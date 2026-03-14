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


@retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential_jitter(initial=2, max=15, jitter=3),
    reraise=True,
)
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
    """Create a Kubernetes pod for a benchmark task.

    The pod runs `sleep infinity` to stay alive for exec calls.

    Returns the pod name.
    """
    pod_name = _make_pod_name(session_id)

    container_env = []
    for k, v in (env_vars or {}).items():
        container_env.append(client.V1EnvVar(name=k, value=v))

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
                    env=container_env or None,
                    resources=client.V1ResourceRequirements(
                        requests={"cpu": str(cpus), "memory": f"{memory_mb}Mi", "ephemeral-storage": "50Gi"},
                        limits={"cpu": str(cpus), "memory": f"{memory_mb}Mi", "ephemeral-storage": "50Gi"},
                    ),
                ),
            ],
            service_account_name="harbor-pod",
            restart_policy="Never",
            image_pull_secrets=[client.V1LocalObjectReference(name=image_pull_secret)] if image_pull_secret else None,
        ),
    )

    try:
        await asyncio.to_thread(
            api.create_namespaced_pod,
            namespace=config.namespace,
            body=pod,
        )
    except client.ApiException as e:
        if e.status == 409:
            logger.debug("Pod %s already exists, reusing", pod_name)
        else:
            raise

    logger.debug("Created pod: %s (image=%s, cpu=%d, memory=%dMi)", pod_name, image_uri, cpus, memory_mb)
    return pod_name


async def wait_for_image_pulled(
    api: client.CoreV1Api,
    config: AWSConfig,
    pod_name: str,
    timeout_sec: int = 600,
) -> None:
    """Wait until the pod is scheduled and image pull is no longer in progress.

    Uses a shared K8s watch stream (O(1) API calls) instead of per-pod polling.
    """
    logger.debug("Waiting for image pull on pod %s...", pod_name)
    watcher = await PodWatcher.get_or_create(config.namespace)
    handle = watcher.register(pod_name)

    try:
        await asyncio.wait_for(handle.image_pulled.wait(), timeout=timeout_sec)
    except asyncio.TimeoutError:
        logger.debug("Pod %s image pull wait timed out after %ds — releasing semaphore", pod_name, timeout_sec)
        return  # release semaphore, let wait_for_pod_running handle errors

    if handle.error:
        raise handle.error

    logger.debug("Pod %s image pull complete", pod_name)


async def wait_for_pod_running(
    api: client.CoreV1Api,
    config: AWSConfig,
    pod_name: str,
    timeout_sec: int = 1800,
) -> None:
    """Wait for pod to reach Running phase and be ready for exec.

    Uses a shared K8s watch stream (O(1) API calls) instead of per-pod polling.
    """
    logger.debug("Waiting for pod %s to be running...", pod_name)
    watcher = await PodWatcher.get_or_create(config.namespace)
    handle = watcher.register(pod_name)

    try:
        await asyncio.wait_for(handle.pod_running.wait(), timeout=timeout_sec)
    except asyncio.TimeoutError:
        raise RuntimeError(f"Pod {pod_name} not running after {timeout_sec}s") from None

    if handle.error:
        raise handle.error

    logger.debug("Pod %s is running and ready", pod_name)


@retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential_jitter(initial=1, max=10, jitter=2),
    reraise=True,
)
async def delete_pod(
    api: client.CoreV1Api,
    config: AWSConfig,
    pod_name: str,
) -> None:
    """Delete a pod. Idempotent — ignores 404."""
    try:
        await asyncio.to_thread(
            api.delete_namespaced_pod,
            name=pod_name,
            namespace=config.namespace,
            grace_period_seconds=0,
        )
        logger.debug("Deleted pod: %s", pod_name)
    except client.ApiException as e:
        if e.status != 404:
            raise

    # Clean up watcher state
    if PodWatcher._instance is not None:
        PodWatcher._instance.unregister(pod_name)


async def list_pods(
    api: client.CoreV1Api,
    config: AWSConfig,
) -> list[str]:
    """List all harbor-aws pods in the namespace."""
    pods = await asyncio.to_thread(
        api.list_namespaced_pod,
        namespace=config.namespace,
        label_selector="managed-by=harbor-aws",
    )
    return [p.metadata.name for p in pods.items]


def _make_pod_name(session_id: str) -> str:
    """Create a valid Kubernetes pod name from a session ID."""
    name = re.sub(r"[^a-z0-9-]", "-", session_id.lower())[:58]
    name = name.strip("-")
    return f"hb-{name}"
