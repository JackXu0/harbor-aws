"""Watch-based pod status monitor — O(1) API calls regardless of pod count."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from dataclasses import dataclass, field

from kubernetes import client, watch
from kubernetes import config as k8s_config

logger = logging.getLogger(__name__)

_WATCH_TIMEOUT = 300
_LABEL_SELECTOR = "managed-by=harbor-aws"

_PERMANENT_WAITING_REASONS = frozenset({
    "InvalidImageName",
    "CreateContainerConfigError",
    "CreateContainerError",
    "RunContainerError",
})


@dataclass
class _PodWaitHandle:
    """Per-pod wait state."""

    pod_running: asyncio.Event = field(default_factory=asyncio.Event)
    error: Exception | None = None
    phase: str | None = None


class PodWatcher:
    """Singleton that watches all harbor-aws pods via a single K8s watch stream.

    Callers register interest via register() and await the returned handle's events.
    """

    _instance: PodWatcher | None = None
    _instance_lock = threading.Lock()

    def __init__(self, namespace: str) -> None:
        self._namespace = namespace
        self._handles: dict[str, _PodWaitHandle] = {}
        self._handles_lock = threading.Lock()
        self._cached_statuses: dict[str, client.V1Pod] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._watch_thread: threading.Thread | None = None
        self._resource_version: str | None = None
        self._started = threading.Event()
        self._thread_error: Exception | None = None

    # --- public API ---

    @classmethod
    async def get_or_create(cls, namespace: str) -> PodWatcher:
        """Get or create the singleton watcher."""
        with cls._instance_lock:
            inst = cls._instance
            if inst and inst._watch_thread and inst._watch_thread.is_alive():
                return inst

            if inst:
                logger.warning("PodWatcher thread died, recreating")

            watcher = cls(namespace)
            watcher._loop = asyncio.get_running_loop()
            watcher._watch_thread = threading.Thread(target=watcher._watch_thread_main, daemon=True, name="pod-watcher")
            watcher._watch_thread.start()
            cls._instance = watcher

        await asyncio.to_thread(watcher._started.wait, 30)
        if watcher._thread_error is not None:
            raise watcher._thread_error
        return watcher

    def register(self, pod_name: str) -> _PodWaitHandle:
        """Register interest in a pod. Replays cached status if available."""
        with self._handles_lock:
            if pod_name in self._handles:
                return self._handles[pod_name]

            handle = _PodWaitHandle()
            self._handles[pod_name] = handle

            cached_pod = self._cached_statuses.pop(pod_name, None)
            if cached_pod is not None:
                self._evaluate_pod(handle, cached_pod)

            return handle

    def _watch_thread_main(self) -> None:
        try:
            api = self._make_core_v1_api()
            self._reconcile_watcher_state(api)
        except Exception as exc:
            logger.exception("PodWatcher initial list failed")
            self._thread_error = exc
            self._started.set()
            return

        self._started.set()
        logger.info("PodWatcher started (namespace=%s)", self._namespace)

        while True:
            try:
                api = self._make_core_v1_api()
                w = watch.Watch()
                kwargs: dict = {"namespace": self._namespace, "label_selector": _LABEL_SELECTOR,
                                "timeout_seconds": _WATCH_TIMEOUT}
                if self._resource_version:
                    kwargs["resource_version"] = self._resource_version

                for event in w.stream(api.list_namespaced_pod, **kwargs):
                    self._process_event(event)
            except client.ApiException as e:
                if e.status == 410:
                    logger.debug("Watch 410 Gone, re-listing")
                    try:
                        self._reconcile_watcher_state(self._make_core_v1_api())
                    except Exception:
                        logger.exception("Re-list after 410 failed")
                        time.sleep(2)
                else:
                    logger.warning("Watch ApiException %d, reconnecting in 2s", e.status)
                    time.sleep(2)
            except Exception:
                logger.warning("Watch stream error, reconnecting in 2s", exc_info=True)
                time.sleep(2)

    def _reconcile_watcher_state(self, api: client.CoreV1Api) -> None:
        pod_list = api.list_namespaced_pod(namespace=self._namespace, label_selector=_LABEL_SELECTOR)
        self._resource_version = pod_list.metadata.resource_version

        with self._handles_lock:
            for pod in pod_list.items:
                self._ingest_pod(pod)

        logger.debug("PodWatcher reconciled: %d pods, rv=%s", len(pod_list.items), self._resource_version)

    def _process_event(self, event: dict) -> None:
        pod: client.V1Pod = event["object"]
        pod_name: str = pod.metadata.name
        self._resource_version = pod.metadata.resource_version

        with self._handles_lock:
            if event["type"] == "DELETED":
                handle = self._handles.get(pod_name)
                if handle is not None and not handle.pod_running.is_set():
                    handle.error = RuntimeError(f"Pod {pod_name} was deleted")
                    self._set_event(handle.pod_running)
                self._cached_statuses.pop(pod_name, None)
                return
            self._ingest_pod(pod)

    def _ingest_pod(self, pod: client.V1Pod) -> None:
        """Evaluate if a handle is registered, else cache for later. Caller must hold _handles_lock."""
        pod_name = pod.metadata.name
        handle = self._handles.get(pod_name)
        if handle is not None:
            self._evaluate_pod(handle, pod)
        else:
            self._cached_statuses[pod_name] = pod


    def _evaluate_pod(self, handle: _PodWaitHandle, pod: client.V1Pod) -> None:
        """Decides whether the pod has reached a terminal state (success, failure, or permanent infra error) and signals the awaiter via handle"""
        status = pod.status
        phase = status.phase if status else None
        handle.phase = phase

        if handle.pod_running.is_set():
            return

        # 1. Terminal phase
        if phase in ("Failed", "Succeeded"):
            handle.error = RuntimeError(
                f"Pod {pod.metadata.name} terminated: {self._failure_reason(pod)}"
            )
            self._set_event(handle.pod_running)
            return

        # 2. Happy path
        statuses = status.container_statuses if status else None
        if phase == "Running" and all(cs.ready for cs in (statuses or [])):
            self._set_event(handle.pod_running)
            return

        # 3. Permanent failure detection (still starting, but stuck for good)
        permanent = self._detect_permanent_failure(pod)
        if permanent:
            handle.error = RuntimeError(permanent)
            self._set_event(handle.pod_running)

    @staticmethod
    def _detect_permanent_failure(pod: client.V1Pod) -> str | None:
        """Return a failure message if the pod is in a permanently-failed state, else None."""
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

    def _set_event(self, event: asyncio.Event) -> None:
        """Schedule event.set() on the asyncio loop. Safe to call from any thread."""
        assert self._loop is not None
        with contextlib.suppress(RuntimeError):
            self._loop.call_soon_threadsafe(event.set)

    @staticmethod
    def _failure_reason(pod: client.V1Pod) -> str:
        reasons = []
        for cs in pod.status.container_statuses or []:
            if cs.state and cs.state.terminated:
                reasons.append(f"{cs.name}: {cs.state.terminated.reason or 'unknown'}")
            elif cs.state and cs.state.waiting:
                reasons.append(f"{cs.name}: {cs.state.waiting.reason or 'unknown'}")
        return "; ".join(reasons) if reasons else (pod.status.phase or "unknown")

    @staticmethod
    def _make_core_v1_api() -> client.CoreV1Api:
        k8s_config.load_kube_config()
        return client.CoreV1Api(api_client=client.ApiClient())
