"""Kubernetes pod lifecycle management for benchmark containers."""

from __future__ import annotations

import asyncio
import logging
import re
import uuid

from kubernetes import client
from tenacity import retry, stop_after_attempt, wait_exponential_jitter

from harbor_aws.core.config import AWSConfig
from harbor_aws.core.watcher import PodWatcher

logger = logging.getLogger(__name__)


# Bootstrap script for the pod's PID 1. Probes for bash and installs it via
# apk on Alpine if missing (the runner needs bash for /dev/tcp). Then exec's
# the bash runner. Uses POSIX sh so it works even on Alpine where /bin/sh is
# busybox ash.
_RUNNER_BOOTSTRAP_SH = r"""
set -e
if ! command -v bash >/dev/null 2>&1; then
    echo "harbor-runner: bash not found, installing..." >&2
    if command -v apk >/dev/null 2>&1; then
        apk add --no-cache bash
    elif command -v apt-get >/dev/null 2>&1; then
        DEBIAN_FRONTEND=noninteractive apt-get update -qq
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq bash
    elif command -v dnf >/dev/null 2>&1; then
        dnf install -y --quiet bash
    elif command -v yum >/dev/null 2>&1; then
        yum install -y --quiet bash
    else
        echo "harbor-runner: no bash and no known package manager" >&2
        exit 1
    fi
fi
exec bash /harbor-runner/runner.sh
"""


@retry(stop=stop_after_attempt(5), wait=wait_exponential_jitter(initial=2, max=15, jitter=3), reraise=True)
async def create_pod(
    api: client.CoreV1Api,
    config: AWSConfig,
    image_uri: str,
    environment_name: str,
    session_id: str,
    cpus: int,
    memory_mb: int,
    trial_id: str,
    trial_token: str,
    runner_configmap: str,
    control_host: str,
    control_runner_port: int,
    image_pull_secret: str | None = None,
) -> str:
    """Create a Fargate pod that runs the harbor runner.sh as PID 1.

    The runner.sh is shipped via the harbor-runner ConfigMap mounted at
    /harbor-runner/runner.sh. The runner dials out to the harbor-control
    server at control_host:control_runner_port and authenticates with
    HARBOR_TOKEN + HARBOR_TRIAL_ID — no inbound listener on the pod.
    """
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
                "harbor-mode": "layer3",
            },
        ),
        spec=client.V1PodSpec(
            volumes=[
                client.V1Volume(
                    name="harbor-runner",
                    config_map=client.V1ConfigMapVolumeSource(
                        name=runner_configmap,
                        default_mode=0o755,
                    ),
                ),
            ],
            containers=[
                client.V1Container(
                    name="main",
                    image=image_uri,
                    command=["sh", "-c", _RUNNER_BOOTSTRAP_SH],
                    security_context=client.V1SecurityContext(run_as_user=0),
                    env=[
                        client.V1EnvVar(name="HARBOR_TOKEN", value=trial_token),
                        client.V1EnvVar(name="HARBOR_TRIAL_ID", value=trial_id),
                        client.V1EnvVar(name="HARBOR_CONTROL_HOST", value=control_host),
                        client.V1EnvVar(name="HARBOR_CONTROL_PORT", value=str(control_runner_port)),
                    ],
                    volume_mounts=[
                        client.V1VolumeMount(
                            name="harbor-runner",
                            mount_path="/harbor-runner",
                            read_only=True,
                        ),
                    ],
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

    logger.debug("Created layer3 pod: %s (image=%s)", pod_name, image_uri)
    return pod_name


async def wait_for_pod_running(
    config: AWSConfig, pod_name: str, timeout_sec: int = 1800,
) -> None:
    """Wait until the pod's main container is Running.

    The adapter does not need the pod IP — the runner dials the control
    server out, identified by HARBOR_TRIAL_ID — so /register would resolve
    on its own. We still call this to surface image-pull errors early
    instead of waiting for the (longer) /register connect timeout.
    """
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
    """Wait for a pod watcher event (currently only ``pod_running``)."""
    logger.debug("Waiting for %s on pod %s...", event, pod_name)
    watcher = await PodWatcher.get_or_create(namespace)
    handle = watcher.register(pod_name)

    try:
        await asyncio.wait_for(getattr(handle, event).wait(), timeout=timeout_sec)
    except TimeoutError:
        if not swallow_timeout:
            raise RuntimeError(f"Pod {pod_name} {event} timed out after {timeout_sec}s") from None
        logger.debug("Pod %s %s timed out after %ds — continuing", pod_name, event, timeout_sec)
        return

    if handle.error:
        raise handle.error

    logger.debug("Pod %s %s complete", pod_name, event)


def _make_pod_name(session_id: str) -> str:
    """Build a Kubernetes pod name from a session id.

    A 6-char random suffix lets retry attempts of the same trial create
    distinct pods, avoiding races with the async deletion of the previous
    attempt's pod.
    """
    suffix = uuid.uuid4().hex[:6]
    # 3 chars for "hb-" prefix + 7 chars for "-{suffix}" → 53 left for the slug.
    name = re.sub(r"[^a-z0-9-]", "-", session_id.lower())[:53].strip("-")
    return f"hb-{name}-{suffix}"
