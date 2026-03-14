"""Watch-based pod status monitor.

Replaces per-pod polling with a single K8s watch stream that pushes status
updates to asyncio.Event waiters.  Reduces API server load from O(n) calls
per interval to O(1) regardless of pod count.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field

from kubernetes import client, watch
from kubernetes import config as k8s_config

logger = logging.getLogger(__name__)

# Diagnostic file logger — writes to /tmp/watcher_diag.log regardless of
# Harbor's log configuration.  Remove once the watcher is validated at scale.
_diag = logging.getLogger("watcher_diag")
_diag.setLevel(logging.DEBUG)
if not _diag.handlers:
    _fh = logging.FileHandler("/tmp/watcher_diag.log", mode="w")
    _fh.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    _diag.addHandler(_fh)

# Server-side watch timeout — the API server closes the stream after this
# many seconds, and we reconnect with the last resource_version.
_WATCH_TIMEOUT = 300


@dataclass
class _PodWaitHandle:
    """Per-pod state for waiters."""

    image_pulled: asyncio.Event = field(default_factory=asyncio.Event)
    pod_running: asyncio.Event = field(default_factory=asyncio.Event)
    error: Exception | None = None
    phase: str | None = None


class PodWatcher:
    """Singleton watch-based pod status monitor.

    A single background thread runs a K8s watch on all pods with
    label ``managed-by=harbor-aws``.  Callers register interest in a
    pod via ``register()`` and await the returned handle's events.

    Uses the list-then-watch pattern: an initial ``list_namespaced_pod``
    captures current state and provides a ``resource_version``, then the
    watch stream picks up from there — no events are missed even if a pod
    reaches Running before ``register()`` is called.
    """

    _instance: PodWatcher | None = None
    _instance_lock = threading.Lock()

    def __init__(self, namespace: str) -> None:
        self._namespace = namespace
        self._handles: dict[str, _PodWaitHandle] = {}
        self._handles_lock = threading.Lock()
        # Cache statuses for pods that send events before register() is called
        self._cached_statuses: dict[str, client.V1Pod] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._watch_thread: threading.Thread | None = None
        self._stopped = threading.Event()
        self._resource_version: str | None = None
        self._started = threading.Event()
        self._thread_error: Exception | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @classmethod
    async def get_or_create(cls, namespace: str) -> PodWatcher:
        """Get or create the singleton watcher for *namespace*."""
        with cls._instance_lock:
            inst = cls._instance
            if inst is not None and not inst._stopped.is_set():
                # Verify the watch thread is still alive
                if inst._watch_thread is not None and inst._watch_thread.is_alive():
                    return inst
                # Thread died — recreate
                logger.warning("PodWatcher thread died, recreating")

            watcher = cls(namespace)
            watcher._loop = asyncio.get_running_loop()
            watcher._watch_thread = threading.Thread(
                target=watcher._watch_loop, daemon=True, name="pod-watcher",
            )
            watcher._watch_thread.start()
            cls._instance = watcher

        # Wait until the initial list is done so register() can check cached state
        await asyncio.to_thread(watcher._started.wait, 30)
        if watcher._thread_error is not None:
            raise watcher._thread_error
        return watcher

    def register(self, pod_name: str) -> _PodWaitHandle:
        """Register interest in a pod.  Thread-safe.

        If the watcher already saw this pod (via the initial list or an
        event before registration), the handle's events are evaluated
        immediately so the caller doesn't miss a transition.
        """
        with self._handles_lock:
            if pod_name in self._handles:
                h = self._handles[pod_name]
                _diag.debug("REGISTER %s (existing) img=%s run=%s", pod_name, h.image_pulled.is_set(), h.pod_running.is_set())
                return h

            handle = _PodWaitHandle()
            self._handles[pod_name] = handle

            # Check if we already have a cached status from the initial list
            # or from events received before this registration.
            cached_pod = self._cached_statuses.pop(pod_name, None)
            if cached_pod is not None:
                self._evaluate_pod(handle, pod_name, cached_pod)
                _diag.debug("REGISTER %s (new, from cache) img=%s run=%s", pod_name, handle.image_pulled.is_set(), handle.pod_running.is_set())
            else:
                _diag.debug("REGISTER %s (new, no cache)", pod_name)

            return handle

    def unregister(self, pod_name: str) -> None:
        """Remove a pod from the watch list."""
        with self._handles_lock:
            self._handles.pop(pod_name, None)
            self._cached_statuses.pop(pod_name, None)

    def stop(self) -> None:
        """Stop the watcher thread."""
        self._stopped.set()
        with PodWatcher._instance_lock:
            if PodWatcher._instance is self:
                PodWatcher._instance = None

    # ------------------------------------------------------------------
    # Background watch thread
    # ------------------------------------------------------------------

    def _make_watch_api(self) -> client.CoreV1Api:
        """Create a fresh CoreV1Api for the watch stream."""
        k8s_config.load_kube_config()
        return client.CoreV1Api(api_client=client.ApiClient())

    def _watch_loop(self) -> None:
        """Thread target: list-then-watch loop with reconnect."""
        try:
            api = self._make_watch_api()
            self._do_initial_list(api)
        except Exception as exc:
            logger.exception("PodWatcher initial list failed")
            self._thread_error = exc
            self._started.set()
            return

        self._started.set()
        _diag.info("STARTED namespace=%s rv=%s", self._namespace, self._resource_version)
        logger.info("PodWatcher started (namespace=%s)", self._namespace)

        while not self._stopped.is_set():
            try:
                api = self._make_watch_api()
                w = watch.Watch()
                kwargs: dict = {
                    "namespace": self._namespace,
                    "label_selector": "managed-by=harbor-aws",
                    "timeout_seconds": _WATCH_TIMEOUT,
                }
                if self._resource_version:
                    kwargs["resource_version"] = self._resource_version

                _diag.info("WATCH connecting rv=%s", self._resource_version)
                event_count = 0
                for event in w.stream(api.list_namespaced_pod, **kwargs):
                    if self._stopped.is_set():
                        w.stop()
                        return
                    event_count += 1
                    self._process_event(event)
                _diag.info("WATCH stream ended after %d events", event_count)
            except client.ApiException as e:
                _diag.warning("WATCH ApiException %d", e.status)
                if e.status == 410:
                    # resource_version too old — re-list
                    logger.debug("Watch 410 Gone, re-listing")
                    try:
                        api = self._make_watch_api()
                        self._do_initial_list(api)
                    except Exception:
                        logger.exception("Re-list after 410 failed")
                        time.sleep(2)
                else:
                    logger.warning("Watch ApiException %d, reconnecting in 2s", e.status)
                    time.sleep(2)
            except Exception as exc:
                _diag.warning("WATCH exception: %s", exc)
                if not self._stopped.is_set():
                    logger.warning("Watch stream error, reconnecting in 2s", exc_info=True)
                    time.sleep(2)

    def _do_initial_list(self, api: client.CoreV1Api) -> None:
        """List all pods to seed cached state and get resource_version."""
        pod_list = api.list_namespaced_pod(
            namespace=self._namespace,
            label_selector="managed-by=harbor-aws",
        )
        self._resource_version = pod_list.metadata.resource_version

        with self._handles_lock:
            for pod in pod_list.items:
                name = pod.metadata.name
                handle = self._handles.get(name)
                if handle is not None:
                    self._evaluate_pod(handle, name, pod)
                else:
                    self._cached_statuses[name] = pod

        logger.debug(
            "PodWatcher initial list: %d pods, resource_version=%s",
            len(pod_list.items), self._resource_version,
        )

    def _process_event(self, event: dict) -> None:
        """Handle a single watch event."""
        event_type: str = event["type"]
        pod: client.V1Pod = event["object"]
        pod_name: str = pod.metadata.name
        phase = pod.status.phase if pod.status else None
        self._resource_version = pod.metadata.resource_version

        with self._handles_lock:
            handle = self._handles.get(pod_name)
            has_handle = handle is not None

            if event_type == "DELETED":
                if handle is not None:
                    handle.error = RuntimeError(f"Pod {pod_name} was deleted")
                    self._set_event(handle.image_pulled)
                    self._set_event(handle.pod_running)
                self._cached_statuses.pop(pod_name, None)
                _diag.debug("EVENT %s %s phase=%s handle=%s", event_type, pod_name, phase, has_handle)
                return

            # ADDED or MODIFIED
            if handle is not None:
                self._evaluate_pod(handle, pod_name, pod)
                _diag.debug(
                    "EVENT %s %s phase=%s img_set=%s run_set=%s",
                    event_type, pod_name, phase,
                    handle.image_pulled.is_set(), handle.pod_running.is_set(),
                )
            else:
                self._cached_statuses[pod_name] = pod
                _diag.debug("EVENT %s %s phase=%s -> cached (no handle)", event_type, pod_name, phase)

    # ------------------------------------------------------------------
    # Condition evaluation
    # ------------------------------------------------------------------

    def _evaluate_pod(
        self, handle: _PodWaitHandle, pod_name: str, pod: client.V1Pod,
    ) -> None:
        """Evaluate image-pulled and pod-running conditions for a pod.

        Called from both the watch thread and the asyncio thread (via
        ``register()``).  Uses ``call_soon_threadsafe`` to set asyncio
        Events safely from any thread.
        """
        phase = pod.status.phase if pod.status else None
        handle.phase = phase
        container_statuses = (
            pod.status.container_statuses if pod.status else None
        )

        # --- Image pulled condition ---
        if not handle.image_pulled.is_set():
            if phase in ("Running", "Failed", "Succeeded"):
                self._set_event(handle.image_pulled)
            elif container_statuses:
                has_pull_error = any(
                    cs.state and cs.state.waiting
                    and (cs.state.waiting.reason or "")
                    in ("ErrImagePull", "ImagePullBackOff")
                    for cs in container_statuses
                )
                if not has_pull_error:
                    self._set_event(handle.image_pulled)

        # --- Pod running condition ---
        if not handle.pod_running.is_set():
            if phase in ("Failed", "Succeeded"):
                handle.error = RuntimeError(
                    f"Pod {pod_name} terminated before becoming ready: "
                    f"{self._failure_reason(pod)}"
                )
                self._set_event(handle.pod_running)
            elif phase == "Running":
                all_ready = all(
                    cs.ready for cs in (container_statuses or [])
                )
                if all_ready:
                    self._set_event(handle.pod_running)
            else:
                # Detect unrecoverable errors during pending phase
                for cs in container_statuses or []:
                    if cs.state and cs.state.waiting:
                        msg = cs.state.waiting.message or ""
                        if "no space left on device" in msg:
                            handle.error = RuntimeError(
                                f"Pod {pod_name} image pull failed: {msg}"
                            )
                            self._set_event(handle.pod_running)
                            break

    def _set_event(self, event: asyncio.Event) -> None:
        """Set an asyncio.Event from any thread.

        Uses ``call_soon_threadsafe`` so it works from both the watch
        thread and the asyncio thread (register path).
        """
        loop = self._loop
        if loop is None:
            return
        try:
            loop.call_soon_threadsafe(event.set)
        except RuntimeError:
            # Loop is closed — best-effort direct set
            event.set()

    @staticmethod
    def _failure_reason(pod: client.V1Pod) -> str:
        """Extract failure reason from pod status."""
        reasons = []
        for cs in pod.status.container_statuses or []:
            if cs.state and cs.state.terminated:
                reasons.append(
                    f"{cs.name}: {cs.state.terminated.reason or 'unknown'}"
                )
            elif cs.state and cs.state.waiting:
                reasons.append(
                    f"{cs.name}: {cs.state.waiting.reason or 'unknown'}"
                )
        return (
            "; ".join(reasons) if reasons else (pod.status.phase or "unknown")
        )
