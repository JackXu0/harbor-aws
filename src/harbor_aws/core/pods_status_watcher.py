"""One K8s watch stream serving every /wait-pod-running request in the control pod."""

from __future__ import annotations

import asyncio
import logging
import random
import threading
import time

from kubernetes import client, watch
from kubernetes import config as k8s_config

logger = logging.getLogger(__name__)

_WATCH_TIMEOUT = 300
_LABEL_SELECTOR = "managed-by=harbor-aws"
_BACKOFF_INITIAL = 1.0
_BACKOFF_MAX = 30.0
_STARTUP_TIMEOUT = 30.0

_PERMANENT_WAITING_REASONS = frozenset({
    "InvalidImageName",
    "CreateContainerConfigError",
    "CreateContainerError",
    "RunContainerError",
})


class PodStatusWatcher:
    """Watches all harbor-aws pods via a single K8s watch stream."""

    def __init__(self, namespace: str) -> None:
        self._namespace = namespace
        self._handles: dict[str, asyncio.Future[None]] = {}
        self._handles_lock = threading.Lock()
        self._unclaimed: dict[str, client.V1Pod] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._watch_thread: threading.Thread | None = None
        self._resource_version: str | None = None
        self._started = threading.Event()
        self._thread_error: Exception | None = None
        self._backoff = _BACKOFF_INITIAL

    # ===== Public API =====

    @classmethod
    async def create(cls, namespace: str) -> PodStatusWatcher:
        watcher = cls(namespace)
        watcher._loop = asyncio.get_running_loop()
        watcher._watch_thread = threading.Thread(
            target=watcher._watch_loop, daemon=True, name="pod-watcher",
        )
        watcher._watch_thread.start()
        ready = await asyncio.to_thread(watcher._started.wait, _STARTUP_TIMEOUT)
        if not ready:
            raise TimeoutError(
                f"PodStatusWatcher startup exceeded {_STARTUP_TIMEOUT:.0f}s (initial K8s list slow or hung)"
            )
        if watcher._thread_error is not None:
            raise watcher._thread_error
        return watcher

    async def wait_pod_running(self, pod_name: str, *, timeout: float) -> None:
        assert self._loop is not None
        with self._handles_lock:
            fut = self._handles.get(pod_name)
            if fut is None:
                fut = self._loop.create_future()
                self._handles[pod_name] = fut
                cached_pod = self._unclaimed.pop(pod_name, None)
                if cached_pod is not None:
                    self._dispatch(cached_pod)
        await asyncio.wait_for(fut, timeout=timeout)

    # ===== Always-on background thread: K8s events watch loop =====

    def _watch_loop(self) -> None:
        try:
            self._reconcile(self._k8s_api())
        except Exception as exc:
            logger.exception("PodStatusWatcher initial list failed")
            self._thread_error = exc
            self._started.set()
            return

        self._started.set()
        logger.info("PodStatusWatcher started (namespace=%s)", self._namespace)

        while True:
            try:
                api = self._k8s_api()
                stream_kwargs: dict = {
                    "namespace": self._namespace,
                    "label_selector": _LABEL_SELECTOR,
                    "timeout_seconds": _WATCH_TIMEOUT,
                }
                if self._resource_version:
                    stream_kwargs["resource_version"] = self._resource_version
                for event in watch.Watch().stream(api.list_namespaced_pod, **stream_kwargs):
                    try:
                        pod: client.V1Pod = event["object"]
                        pod_name: str = pod.metadata.name
                        with self._handles_lock:
                            if event["type"] == "DELETED":
                                fut = self._handles.get(pod_name)
                                if fut is not None:
                                    self._fail(fut, RuntimeError(f"Pod {pod_name} was deleted"))
                                self._unclaimed.pop(pod_name, None)
                            else:
                                self._dispatch(pod)

                        self._resource_version = pod.metadata.resource_version
                    except Exception:
                        logger.exception("PodStatusWatcher: skipping bad event")
                self._backoff = _BACKOFF_INITIAL
            except client.ApiException as e:
                if e.status == 410:
                    logger.debug("Watch 410 Gone, re-listing")
                    try:
                        self._reconcile(self._k8s_api())
                        self._backoff = _BACKOFF_INITIAL
                    except Exception:
                        logger.exception("Re-list after 410 failed")
                        self._retry_sleep()
                else:
                    logger.warning("Watch ApiException %d, reconnecting", e.status)
                    self._retry_sleep()
            except Exception:
                logger.warning("Watch stream error, reconnecting", exc_info=True)
                self._retry_sleep()

    def _retry_sleep(self) -> None:
        time.sleep(self._backoff + random.uniform(0, 0.5))
        self._backoff = min(self._backoff * 2, _BACKOFF_MAX)

    def _reconcile(self, api: client.CoreV1Api) -> None:
        pod_list = api.list_namespaced_pod(namespace=self._namespace, label_selector=_LABEL_SELECTOR)
        self._resource_version = pod_list.metadata.resource_version
        with self._handles_lock:
            for pod in pod_list.items:
                self._dispatch(pod)
        logger.debug("PodStatusWatcher reconciled: %d pods, rv=%s", len(pod_list.items), self._resource_version)

    def _dispatch(self, pod: client.V1Pod) -> None:
        """Route to handle or stash as unclaimed. Caller holds _handles_lock."""
        pod_name = pod.metadata.name
        fut = self._handles.get(pod_name)
        if fut is None:
            self._unclaimed[pod_name] = pod
            return
        if fut.done():
            return
        outcome = _check_outcome(pod)
        if outcome is True:
            self._succeed(fut)
        elif isinstance(outcome, Exception):
            self._fail(fut, outcome)

    # ===== Helpers: thread-safe Future completion =====

    def _succeed(self, fut: asyncio.Future[None]) -> None:
        assert self._loop is not None
        def apply() -> None:
            if not fut.done():
                fut.set_result(None)
        try:
            self._loop.call_soon_threadsafe(apply)
        except RuntimeError:
            logger.debug("event loop closed; skipping fut.set_result")

    def _fail(self, fut: asyncio.Future[None], exc: Exception) -> None:
        assert self._loop is not None
        def apply() -> None:
            if not fut.done():
                fut.set_exception(exc)
        try:
            self._loop.call_soon_threadsafe(apply)
        except RuntimeError:
            logger.debug("event loop closed; skipping fut.set_exception")

    @staticmethod
    def _k8s_api() -> client.CoreV1Api:
        k8s_config.load_incluster_config()
        return client.CoreV1Api(api_client=client.ApiClient())


def _check_outcome(pod: client.V1Pod) -> Exception | bool | None:
    """True if Running and ready; Exception if terminally failed; None if still progressing."""
    status = pod.status
    phase = status.phase if status else None

    if phase in ("Failed", "Succeeded"):
        return RuntimeError(f"Pod {pod.metadata.name} terminated: {_failure_summary(pod)}")

    statuses = status.container_statuses if status else None
    if phase == "Running" and all(cs.ready for cs in (statuses or [])):
        return True

    permanent = _permanent_failure_message(pod)
    if permanent:
        return RuntimeError(permanent)

    return None


def _failure_summary(pod: client.V1Pod) -> str:
    reasons = []
    for cs in pod.status.container_statuses or []:
        if cs.state and cs.state.terminated:
            reasons.append(f"{cs.name}: {cs.state.terminated.reason or 'unknown'}")
        elif cs.state and cs.state.waiting:
            reasons.append(f"{cs.name}: {cs.state.waiting.reason or 'unknown'}")
    return "; ".join(reasons) if reasons else (pod.status.phase or "unknown")


def _permanent_failure_message(pod: client.V1Pod) -> str | None:
    for cs in pod.status.container_statuses or []:
        waiting = cs.state.waiting if cs.state else None
        if not waiting:
            continue
        reason = waiting.reason or ""
        message = waiting.message or ""
        if reason in _PERMANENT_WAITING_REASONS:
            return f"Pod {pod.metadata.name} container {cs.name} failed ({reason}): {message}"
        if "no space left on device" in message:
            return f"Pod {pod.metadata.name} disk full: {message}"
    return None
