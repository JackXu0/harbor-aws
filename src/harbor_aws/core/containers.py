"""ECS/Fargate container lifecycle management."""

from __future__ import annotations

import asyncio
import base64
import importlib.resources
import logging

from tenacity import retry, stop_after_attempt, wait_exponential_jitter

from harbor_aws.core.config import AWSConfig, map_to_fargate_resources

logger = logging.getLogger(__name__)


@retry(
    stop=stop_after_attempt(10),
    wait=wait_exponential_jitter(initial=2, max=15, jitter=3),
    reraise=True,
)
async def register_task_definition(
    ecs_client: object,
    config: AWSConfig,
    image_uri: str,
    environment_name: str,
    session_id: str,
    cpus: int,
    memory_mb: int,
    env_vars: dict[str, str] | None = None,
) -> str:
    """Register an ECS task definition for Fargate.

    Injects the harbor daemon into the container entrypoint. The daemon
    polls S3 for commands and executes them.

    Returns the task definition ARN.
    """
    fargate_cpu, fargate_memory = map_to_fargate_resources(cpus, memory_mb)

    # Read daemon source and encode for injection into container command
    daemon_src = importlib.resources.files("harbor_aws").joinpath("daemon.py").read_text()
    daemon_b64 = base64.b64encode(daemon_src.encode()).decode()

    # Container entrypoint: decode daemon, run it in background, then sleep.
    # swe-bench images use conda so python3 may not be on the default PATH.
    entrypoint_cmd = (
        f"echo '{daemon_b64}' | base64 -d > /tmp/_harbor_daemon.py && "
        f"PYTHON=$(which python3 2>/dev/null || which python 2>/dev/null "
        f"|| echo /opt/conda/bin/python) && "
        f"$PYTHON /tmp/_harbor_daemon.py &\n"
        f"sleep infinity"
    )

    s3_prefix = f"{config.s3_prefix}{session_id}/"
    container_env = [
        {"name": "HARBOR_S3_BUCKET", "value": config.s3_bucket},
        {"name": "HARBOR_S3_PREFIX", "value": s3_prefix},
        {"name": "AWS_DEFAULT_REGION", "value": config.region},
    ]
    container_env.extend({"name": k, "value": v} for k, v in (env_vars or {}).items())

    import re

    family = re.sub(r"[^a-zA-Z0-9-]", "-", f"harbor-aws-{environment_name}")[:255]

    task_def = {
        "family": family,
        "networkMode": "awsvpc",
        "requiresCompatibilities": ["FARGATE"],
        "cpu": fargate_cpu,
        "memory": fargate_memory,
        "executionRoleArn": config.task_execution_role_arn,
        "taskRoleArn": config.task_role_arn,
        "containerDefinitions": [
            {
                "name": "main",
                "image": image_uri,
                "entryPoint": ["bash", "-c"],
                "command": [entrypoint_cmd],
                "essential": True,
                "environment": container_env,
                "linuxParameters": {
                    "initProcessEnabled": True,
                },
                "logConfiguration": {
                    "logDriver": "awslogs",
                    "options": {
                        "awslogs-group": f"/harbor-aws/{config.cluster_name}",
                        "awslogs-region": config.region,
                        "awslogs-stream-prefix": environment_name,
                        "awslogs-create-group": "true",
                    },
                },
            }
        ],
    }

    response = await asyncio.to_thread(
        ecs_client.register_task_definition,  # type: ignore[union-attr]
        **task_def,
    )

    arn = response["taskDefinition"]["taskDefinitionArn"]
    logger.debug("Registered task definition: %s (cpu=%s, memory=%s)", arn, fargate_cpu, fargate_memory)
    return arn


@retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential_jitter(initial=1, max=10, jitter=2),
    reraise=True,
)
async def run_task(
    ecs_client: object,
    config: AWSConfig,
    task_definition_arn: str,
    session_id: str,
) -> str:
    """Run a Fargate task.

    Returns the task ARN.
    """
    network_config = {
        "awsvpcConfiguration": {
            "subnets": config.subnets,
            "securityGroups": config.security_groups,
            "assignPublicIp": "ENABLED" if config.assign_public_ip else "DISABLED",
        }
    }

    response = await asyncio.to_thread(
        ecs_client.run_task,  # type: ignore[union-attr]
        cluster=config.cluster_name,
        taskDefinition=task_definition_arn,
        launchType="FARGATE",
        networkConfiguration=network_config,
        count=1,
        tags=[
            {"key": "harbor-session", "value": session_id},
            {"key": "managed-by", "value": "harbor-aws"},
        ],
    )

    failures = response.get("failures", [])
    if failures:
        reasons = "; ".join(f"{f.get('arn', 'N/A')}: {f.get('reason', 'unknown')}" for f in failures)
        raise RuntimeError(f"Failed to run task: {reasons}")

    tasks = response.get("tasks", [])
    if not tasks:
        raise RuntimeError("No task returned from RunTask API call")

    task_arn = tasks[0]["taskArn"]
    logger.debug("Started task: %s", task_arn)
    return task_arn


async def wait_for_task_running(
    ecs_client: object,
    config: AWSConfig,
    task_arn: str,
    timeout_sec: int = 300,
) -> None:
    """Wait for task to reach RUNNING state."""
    logger.debug("Waiting for task %s to be running...", task_arn)

    for elapsed in range(0, timeout_sec, 5):
        response = await asyncio.to_thread(
            ecs_client.describe_tasks,  # type: ignore[union-attr]
            cluster=config.cluster_name,
            tasks=[task_arn],
        )

        tasks = response.get("tasks", [])
        if not tasks:
            raise RuntimeError(f"Task {task_arn} not found")

        task = tasks[0]
        status = task.get("lastStatus", "UNKNOWN")

        if status == "RUNNING":
            logger.debug("Task %s is running", task_arn)
            return

        elif status in ("STOPPED", "DEPROVISIONING"):
            stop_reason = task.get("stoppedReason", "unknown")
            task_containers = task.get("containers", [])
            container_reasons = []
            for c in task_containers:
                if c.get("reason"):
                    container_reasons.append(f"{c['name']}: {c['reason']}")
            details = "; ".join(container_reasons) if container_reasons else stop_reason
            raise RuntimeError(f"Task stopped before becoming ready: {details}")

        elif elapsed % 15 == 0:
            logger.debug("Task status: %s (%ds elapsed)", status, elapsed)

        await asyncio.sleep(5)

    raise RuntimeError(f"Task {task_arn} not running after {timeout_sec}s")


@retry(
    stop=stop_after_attempt(10),
    wait=wait_exponential_jitter(initial=1, max=30, jitter=5),
    reraise=True,
)
async def stop_task(
    ecs_client: object,
    config: AWSConfig,
    task_arn: str,
    reason: str = "Stopped by harbor-aws",
) -> None:
    """Stop an ECS task. Idempotent."""
    try:
        await asyncio.to_thread(
            ecs_client.stop_task,  # type: ignore[union-attr]
            cluster=config.cluster_name,
            task=task_arn,
            reason=reason,
        )
        logger.debug("Stopped task: %s", task_arn)
    except Exception as e:
        # Ignore if task is already stopped
        if "InvalidParameterException" not in str(type(e).__name__):
            raise


async def deregister_task_definition(
    ecs_client: object,
    task_definition_arn: str,
) -> None:
    """Deregister a task definition. Best-effort."""
    try:
        await asyncio.to_thread(
            ecs_client.deregister_task_definition,  # type: ignore[union-attr]
            taskDefinition=task_definition_arn,
        )
        logger.debug("Deregistered task definition: %s", task_definition_arn)
    except Exception as e:
        logger.warning("Failed to deregister task definition %s: %s", task_definition_arn, e)
