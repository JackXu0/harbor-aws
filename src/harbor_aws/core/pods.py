"""Kubernetes pod lifecycle management for benchmark containers."""

from __future__ import annotations

import asyncio
import logging
import re
import uuid

from kubernetes import client
from tenacity import retry, stop_after_attempt, wait_exponential_jitter

from harbor_aws.core import images
from harbor_aws.core.watcher import PodWatcher

logger = logging.getLogger(__name__)

RUNNER_CONFIGMAP = "harbor-runner"
NLB_SERVICE = "harbor-control-nlb"
API_PORT = 8443

EXECUTABLE_MODE = 0o755


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
    namespace: str,
    image_uri: str,
    environment_name: str,
    cpus: int,
    memory_mb: int,
    trial_id: str,
    trial_token: str,
    pod_timeout_sec: int,
    service_account: str | None = None,
) -> str:
    """Create a Fargate pod that runs the harbor runner.sh as PID 1."""
    pod_name = _make_pod_name(trial_id)
    resources = {"cpu": str(cpus), "memory": f"{memory_mb}Mi", "ephemeral-storage": "50Gi"}
    pull_secret = await images.ensure_docker_pull_secret(api, namespace)

    pod = client.V1Pod(
        metadata=client.V1ObjectMeta(
            name=pod_name,
            namespace=namespace,
            labels={
                "app": "harbor-aws",
                "harbor-trial": trial_id[:63],
                "harbor-env": environment_name[:63],
                "managed-by": "harbor-aws",
            },
        ),
        spec=client.V1PodSpec(
            volumes=[
                client.V1Volume(
                    name="harbor-runner",
                    config_map=client.V1ConfigMapVolumeSource(
                        name=RUNNER_CONFIGMAP,
                        default_mode=EXECUTABLE_MODE,
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
                        client.V1EnvVar(name="HARBOR_TRIAL_TOKEN", value=trial_token),
                        client.V1EnvVar(name="HARBOR_TRIAL_ID", value=trial_id),
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
            active_deadline_seconds=pod_timeout_sec,
            service_account_name=service_account or None,
            restart_policy="Never",
            image_pull_secrets=[client.V1LocalObjectReference(name=pull_secret)] if pull_secret else None,
        ),
    )

    await asyncio.to_thread(api.create_namespaced_pod, namespace=namespace, body=pod)
    return pod_name


async def wait_for_pod_running(
    namespace: str, pod_name: str, timeout_sec: int = 1800,
) -> None:
    logger.debug("Waiting for pod %s to be running...", pod_name)
    watcher = await PodWatcher.get_or_create(namespace)
    handle = watcher.register(pod_name)
    try:
        await asyncio.wait_for(handle.pod_running.wait(), timeout=timeout_sec)
    except TimeoutError:
        raise RuntimeError(f"Pod {pod_name} did not become Running in {timeout_sec}s") from None
    if handle.error:
        raise handle.error
    logger.debug("Pod %s is running", pod_name)


@retry(stop=stop_after_attempt(5), wait=wait_exponential_jitter(initial=1, max=10, jitter=2), reraise=True)
async def delete_pod(api: client.CoreV1Api, namespace: str, pod_name: str) -> None:
    try:
        await asyncio.to_thread(api.delete_namespaced_pod, name=pod_name, namespace=namespace, grace_period_seconds=0)
        logger.debug("Deleted pod: %s", pod_name)
    except client.ApiException as e:
        if e.status != 404:
            raise

    PodWatcher.unregister(pod_name)


async def list_pods(api: client.CoreV1Api, namespace: str) -> list[str]:
    """List all harbor-aws pods in the namespace."""
    pods = await asyncio.to_thread(
        api.list_namespaced_pod, namespace=namespace, label_selector="managed-by=harbor-aws",
    )
    return [p.metadata.name for p in pods.items]


@retry(stop=stop_after_attempt(10), wait=wait_exponential_jitter(initial=2, max=10, jitter=2), reraise=True)
def discover_nlb_url(api: client.CoreV1Api, namespace: str) -> str:
    """Read the NLB hostname from the harbor-control-nlb Service status."""
    svc = api.read_namespaced_service(name=NLB_SERVICE, namespace=namespace)
    lb_status = getattr(svc.status, "load_balancer", None) if svc.status else None
    ingress = getattr(lb_status, "ingress", None) if lb_status else None
    if not ingress or not ingress[0].hostname:
        raise RuntimeError(
            f"Service '{NLB_SERVICE}' in namespace '{namespace}' has no LB hostname yet "
            f"(AWS Load Balancer Controller may still be provisioning)."
        )
    return f"https://{ingress[0].hostname}:{API_PORT}"


def validate_runner_configmap(api: client.CoreV1Api, namespace: str) -> None:
    try:
        api.read_namespaced_config_map(name=RUNNER_CONFIGMAP, namespace=namespace)
    except client.ApiException as e:
        if e.status == 404:
            raise RuntimeError(
                f"ConfigMap '{RUNNER_CONFIGMAP}' not found in namespace '{namespace}'. "
                f"Redeploy with: harbor-aws deploy"
            ) from e
        raise


# --- helpers ---


def _make_pod_name(trial_id: str) -> str:
    """Build a Kubernetes pod name from a trial id + retry uuid"""
    suffix = uuid.uuid4().hex[:6]
    # 3 chars for "hb-" prefix + 7 chars for "-{suffix}" → 53 left for the slug.
    name = re.sub(r"[^a-z0-9-]", "-", trial_id.lower())[:53].strip("-")
    return f"hb-{name}-{suffix}"
