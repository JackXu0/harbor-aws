"""Configuration dataclasses for AWS ECS/Fargate backend."""

from __future__ import annotations

from dataclasses import dataclass, field

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

    # ECR
    ecr_repository_uri: str = ""

    # CodeBuild
    codebuild_project_name: str = "harbor-aws-builder"
    build_timeout_minutes: int = 30

    # S3 (for build context and file transfer)
    s3_bucket: str = ""
    s3_prefix: str = "harbor-aws/"

    # Stack-based configuration (alternative to individual fields)
    stack_name: str | None = None

    # Auto-provision: deploy infrastructure if stack doesn't exist
    auto_provision: bool = True

    def validate(self) -> None:
        """Validate that required fields are set."""
        required = {
            "subnets": self.subnets,
            "security_groups": self.security_groups,
            "task_execution_role_arn": self.task_execution_role_arn,
            "task_role_arn": self.task_role_arn,
            "ecr_repository_uri": self.ecr_repository_uri,
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
