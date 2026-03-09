"""Configuration dataclasses for AWS ECS/Fargate backend."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

import boto3

logger = logging.getLogger(__name__)

# Valid Fargate CPU/memory combinations
# https://docs.aws.amazon.com/AmazonECS/latest/developerguide/task-cpu-memory-error.html
FARGATE_CPU_MEMORY_MAP: dict[int, list[int]] = {
    256: [512, 1024, 2048],
    512: [1024, 2048, 3072, 4096],
    1024: [2048, 3072, 4096, 5120, 6144, 7168, 8192],
    2048: list(range(4096, 16385, 1024)),
    4096: list(range(8192, 30721, 1024)),
    8192: list(range(16384, 61441, 4096)),
    16384: list(range(32768, 122881, 8192)),
}


@dataclass
class AWSConfig:
    """AWS-specific configuration for the ECS/Fargate backend."""

    # AWS credentials
    region: str = "us-east-1"
    profile_name: str | None = None

    # ECS
    cluster_name: str = "harbor-aws"
    subnets: list[str] = field(default_factory=list)
    security_groups: list[str] = field(default_factory=list)
    assign_public_ip: bool = True
    task_execution_role_arn: str = ""
    task_role_arn: str = ""

    # S3 for command channel and file transfer
    s3_bucket: str = ""
    s3_prefix: str = "harbor-aws/sessions/"

    # Stack-based configuration (alternative to individual fields)
    stack_name: str | None = None

    def validate(self) -> None:
        """Validate that required fields are set."""
        required = {
            "subnets": self.subnets,
            "security_groups": self.security_groups,
            "task_execution_role_arn": self.task_execution_role_arn,
            "task_role_arn": self.task_role_arn,
            "s3_bucket": self.s3_bucket,
        }
        missing = [k for k, v in required.items() if not v]
        if missing:
            raise ValueError(
                f"Missing required AWS config fields: {', '.join(missing)}. "
                f"Set them directly or use stack_name to read from CloudFormation outputs."
            )


def map_to_fargate_resources(cpus: int, memory_mb: int) -> tuple[str, str]:
    """Map arbitrary CPU/memory values to valid Fargate combinations.

    Args:
        cpus: Number of CPU cores requested.
        memory_mb: Memory in MB requested.

    Returns:
        Tuple of (cpu_str, memory_str) in Fargate units (e.g., "1024", "2048").
    """
    # Convert cores to Fargate CPU units (1 core = 1024 units)
    cpu_units = cpus * 1024
    if cpu_units < 256:
        cpu_units = 256

    # Find the smallest valid CPU that's >= requested
    valid_cpus = sorted(FARGATE_CPU_MEMORY_MAP.keys())
    fargate_cpu = valid_cpus[-1]
    for vc in valid_cpus:
        if vc >= cpu_units:
            fargate_cpu = vc
            break

    # Find the smallest valid memory for this CPU that's >= requested
    valid_memories = FARGATE_CPU_MEMORY_MAP[fargate_cpu]
    fargate_memory = valid_memories[-1]
    for vm in valid_memories:
        if vm >= memory_mb:
            fargate_memory = vm
            break

    return str(fargate_cpu), str(fargate_memory)


def create_ecs_client(config: AWSConfig) -> object:
    """Create a boto3 ECS client."""
    session = boto3.Session(
        region_name=config.region,
        profile_name=config.profile_name or None,
    )
    return session.client("ecs")


def create_s3_client(config: AWSConfig) -> object:
    """Create a boto3 S3 client."""
    session = boto3.Session(
        region_name=config.region,
        profile_name=config.profile_name or None,
    )
    return session.client("s3")


async def load_config_from_stack(
    stack_name: str,
    region: str = "us-east-1",
    profile_name: str | None = None,
) -> AWSConfig:
    """Load AWSConfig from CloudFormation stack outputs."""

    def _read_outputs() -> dict[str, str]:
        session = boto3.Session(profile_name=profile_name, region_name=region)
        cfn = session.client("cloudformation")
        response = cfn.describe_stacks(StackName=stack_name)

        stacks = response.get("Stacks", [])
        if not stacks:
            raise RuntimeError(
                f"Stack '{stack_name}' not found. "
                f"Deploy with: python -m harbor_aws deploy --stack-name {stack_name} --region {region}"
            )

        stack = stacks[0]
        if stack["StackStatus"] not in ("CREATE_COMPLETE", "UPDATE_COMPLETE", "UPDATE_ROLLBACK_COMPLETE"):
            raise RuntimeError(f"Stack '{stack_name}' is in status {stack['StackStatus']}")

        return {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}

    outputs = await asyncio.to_thread(_read_outputs)
    logger.debug("Loaded %d outputs from stack '%s'", len(outputs), stack_name)

    config = AWSConfig(
        region=region,
        profile_name=profile_name,
        stack_name=stack_name,
        cluster_name=outputs.get("ClusterName", "harbor-aws"),
        subnets=outputs.get("SubnetIds", "").split(","),
        security_groups=[outputs["SecurityGroupId"]] if "SecurityGroupId" in outputs else [],
        task_execution_role_arn=outputs.get("TaskExecutionRoleArn", ""),
        task_role_arn=outputs.get("TaskRoleArn", ""),
        s3_bucket=outputs.get("S3Bucket", ""),
    )

    config.validate()
    return config
