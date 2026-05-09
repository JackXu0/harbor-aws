"""Watch-based pod status monitor — O(1) API calls regardless of pod count."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field

from kubernetes import client, watch
from kubernetes import config as k8s_config

logger = logging.getLogger(__name__)

_WATCH_TIMEOUT = 300
_LABEL_SELECTOR = "managed-by=harbor-aws"


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
        self._stopped = threading.Event()
        self._resource_version: str | None = None
        self._started = threading.Event()
        self._thread_error: Exception | None = None

    # --- public API ---

    @classmethod
    async def get_or_create(cls, namespace: str) -> PodWatcher:
        """Get or create the singleton watcher."""
        with cls._instance_lock:
            inst = cls._instance
            if inst and not inst._stopped.is_set() and inst._watch_thread and inst._watch_thread.is_alive():
                return inst

            if inst:
                logger.warning("PodWatcher thread died, recreating")

            watcher = cls(namespace)
            watcher._loop = asyncio.get_running_loop()
            watcher._watch_thread = threading.Thread(target=watcher._watch_loop, daemon=True, name="pod-watcher")
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
                self._evaluate_pod(handle, pod_name, cached_pod)

            return handle

    def unregister(self, pod_name: str) -> None:
        with self._handles_lock:
            self._handles.pop(pod_name, None)
            self._cached_statuses.pop(pod_name, None)

    @classmethod
    def unregister_if_active(cls, pod_name: str) -> None:
        """Unregister a pod from the singleton watcher if one exists. No-op otherwise."""
        if cls._instance is not None:
            cls._instance.unregister(pod_name)

    def stop(self) -> None:
        self._stopped.set()
        with PodWatcher._instance_lock:
            if PodWatcher._instance is self:
                PodWatcher._instance = None

    # --- background watch thread ---

    def _watch_loop(self) -> None:
        try:
            api = self._make_api()
            self._do_initial_list(api)
        except Exception as exc:
            logger.exception("PodWatcher initial list failed")
            self._thread_error = exc
            self._started.set()
            return

        self._started.set()
        logger.info("PodWatcher started (namespace=%s)", self._namespace)

        while not self._stopped.is_set():
            try:
                api = self._make_api()
                w = watch.Watch()
                kwargs: dict = {"namespace": self._namespace, "label_selector": _LABEL_SELECTOR,
                                "timeout_seconds": _WATCH_TIMEOUT}
                if self._resource_version:
                    kwargs["resource_version"] = self._resource_version

                for event in w.stream(api.list_namespaced_pod, **kwargs):
                    if self._stopped.is_set():
                        w.stop()
                        return
                    self._process_event(event)
            except client.ApiException as e:
                if e.status == 410:
                    logger.debug("Watch 410 Gone, re-listing")
                    try:
                        self._do_initial_list(self._make_api())
                    except Exception:
                        logger.exception("Re-list after 410 failed")
                        time.sleep(2)
                else:
                    logger.warning("Watch ApiException %d, reconnecting in 2s", e.status)
                    time.sleep(2)
            except Exception:
                if not self._stopped.is_set():
                    logger.warning("Watch stream error, reconnecting in 2s", exc_info=True)
                    time.sleep(2)

    def _do_initial_list(self, api: client.CoreV1Api) -> None:
        pod_list = api.list_namespaced_pod(namespace=self._namespace, label_selector=_LABEL_SELECTOR)
        self._resource_version = pod_list.metadata.resource_version

        with self._handles_lock:
            for pod in pod_list.items:
                name = pod.metadata.name
                handle = self._handles.get(name)
                if handle is not None:
                    self._evaluate_pod(handle, name, pod)
                else:
                    self._cached_statuses[name] = pod

        logger.debug("PodWatcher initial list: %d pods, rv=%s", len(pod_list.items), self._resource_version)

    def _process_event(self, event: dict) -> None:
        pod: client.V1Pod = event["object"]
        pod_name: str = pod.metadata.name
        self._resource_version = pod.metadata.resource_version

        with self._handles_lock:
            handle = self._handles.get(pod_name)

            if event["type"] == "DELETED":
                if handle is not None:
                    handle.error = RuntimeError(f"Pod {pod_name} was deleted")
                    self._set_event(handle.pod_running)
                self._cached_statuses.pop(pod_name, None)
                return

            if handle is not None:
                self._evaluate_pod(handle, pod_name, pod)
            else:
                self._cached_statuses[pod_name] = pod

    # --- condition evaluation ---

    def _evaluate_pod(self, handle: _PodWaitHandle, pod_name: str, pod: client.V1Pod) -> None:
        phase = pod.status.phase if pod.status else None
        handle.phase = phase
        statuses = pod.status.container_statuses if pod.status else None

        if handle.pod_running.is_set():
            return

        if phase in ("Failed", "Succeeded"):
            handle.error = RuntimeError(f"Pod {pod_name} terminated: {self._failure_reason(pod)}")
            self._set_event(handle.pod_running)
        elif phase == "Running" and all(cs.ready for cs in (statuses or [])):
            self._set_event(handle.pod_running)
        else:
            for cs in statuses or []:
                if cs.state and cs.state.waiting and "no space left on device" in (cs.state.waiting.message or ""):
                    handle.error = RuntimeError(f"Pod {pod_name} image pull failed: {cs.state.waiting.message}")
                    self._set_event(handle.pod_running)
                    break

    def _set_event(self, event: asyncio.Event) -> None:
        """Set an asyncio.Event from any thread."""
        if self._loop is None:
            return
        try:
            self._loop.call_soon_threadsafe(event.set)
        except RuntimeError:
            event.set()

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
    def _make_api() -> client.CoreV1Api:
        k8s_config.load_kube_config()
        return client.CoreV1Api(api_client=client.ApiClient())
