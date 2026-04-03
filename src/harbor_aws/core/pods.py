"""Kubernetes pod lifecycle management for benchmark containers."""

from __future__ import annotations

import asyncio
import logging
import re

from kubernetes import client
from tenacity import retry, stop_after_attempt, wait_exponential_jitter

from harbor_aws.core.config import AWSConfig
from harbor_aws.core.watcher import PodWatcher

logger = logging.getLogger(__name__)


@retry(stop=stop_after_attempt(5), wait=wait_exponential_jitter(initial=2, max=15, jitter=3), reraise=True)
async def create_pod(
    api: client.CoreV1Api,
    config: AWSConfig,
    image_uri: str,
    environment_name: str,
    session_id: str,
    cpus: int,
    memory_mb: int,
    env_vars: dict[str, str] | None = None,
    image_pull_secret: str | None = None,
) -> str:
    """Create a pod that runs `sleep infinity` for exec calls. Returns the pod name."""
    pod_name = _make_pod_name(session_id)
    resources = {"cpu": str(cpus), "memory": f"{memory_mb}Mi", "ephemeral-storage": "50Gi"}

    pod = client.V1Pod(
        metadata=client.V1ObjectMeta(
            name=pod_name,
            namespace=config.namespace,
            labels={
                "app": "harbor-aws",
                "harbor-session": session_id[:63],
                "harbor-env": environment_name[:63],
                "managed-by": "harbor-aws",
            },
        ),
        spec=client.V1PodSpec(
            containers=[
                client.V1Container(
                    name="main",
                    image=image_uri,
                    command=["sleep", "infinity"],
                    security_context=client.V1SecurityContext(run_as_user=0),
                    env=[client.V1EnvVar(name=k, value=v) for k, v in (env_vars or {}).items()] or None,
                    resources=client.V1ResourceRequirements(requests=resources, limits=resources),
                ),
            ],
            active_deadline_seconds=config.pod_timeout_sec,
            service_account_name=config.k8s_service_account or None,
            restart_policy="Never",
            image_pull_secrets=[client.V1LocalObjectReference(name=image_pull_secret)] if image_pull_secret else None,
        ),
    )

    try:
        await asyncio.to_thread(api.create_namespaced_pod, namespace=config.namespace, body=pod)
    except client.ApiException as e:
        if e.status == 409:
            logger.debug("Pod %s already exists, reusing", pod_name)
        else:
            raise

    logger.debug("Created pod: %s (image=%s, cpu=%d, memory=%dMi)", pod_name, image_uri, cpus, memory_mb)
    return pod_name


async def wait_for_image_pulled(
    api: client.CoreV1Api, config: AWSConfig, pod_name: str, timeout_sec: int = 600,
) -> None:
    """Wait until the pod's image pull completes (O(1) API calls via shared watch)."""
    await _wait_for_pod_event(config.namespace, pod_name, "image_pulled", timeout_sec, swallow_timeout=True)


async def wait_for_pod_running(
    api: client.CoreV1Api, config: AWSConfig, pod_name: str, timeout_sec: int = 1800,
) -> None:
    """Wait for pod to reach Running phase and be ready for exec."""
    await _wait_for_pod_event(config.namespace, pod_name, "pod_running", timeout_sec, swallow_timeout=False)


@retry(stop=stop_after_attempt(5), wait=wait_exponential_jitter(initial=1, max=10, jitter=2), reraise=True)
async def delete_pod(api: client.CoreV1Api, config: AWSConfig, pod_name: str) -> None:
    """Delete a pod. Idempotent — ignores 404."""
    try:
        await asyncio.to_thread(api.delete_namespaced_pod, name=pod_name, namespace=config.namespace, grace_period_seconds=0)
        logger.debug("Deleted pod: %s", pod_name)
    except client.ApiException as e:
        if e.status != 404:
            raise

    if PodWatcher._instance is not None:
        PodWatcher._instance.unregister(pod_name)


async def list_pods(api: client.CoreV1Api, config: AWSConfig) -> list[str]:
    """List all harbor-aws pods in the namespace."""
    pods = await asyncio.to_thread(
        api.list_namespaced_pod, namespace=config.namespace, label_selector="managed-by=harbor-aws",
    )
    return [p.metadata.name for p in pods.items]


# --- helpers ---


async def _wait_for_pod_event(
    namespace: str, pod_name: str, event: str, timeout_sec: int, *, swallow_timeout: bool,
) -> None:
    """Wait for a pod watcher event (image_pulled or pod_running)."""
    logger.debug("Waiting for %s on pod %s...", event, pod_name)
    watcher = await PodWatcher.get_or_create(namespace)
    handle = watcher.register(pod_name)

    try:
        await asyncio.wait_for(getattr(handle, event).wait(), timeout=timeout_sec)
    except asyncio.TimeoutError:
        if not swallow_timeout:
            raise RuntimeError(f"Pod {pod_name} {event} timed out after {timeout_sec}s") from None
        logger.debug("Pod %s %s timed out after %ds — continuing", pod_name, event, timeout_sec)
        return

    if handle.error:
        raise handle.error

    logger.debug("Pod %s %s complete", pod_name, event)


def _make_pod_name(session_id: str) -> str:
    """Create a valid Kubernetes pod name from a session ID."""
    name = re.sub(r"[^a-z0-9-]", "-", session_id.lower())[:58]
    return f"hb-{name.strip('-')}"
