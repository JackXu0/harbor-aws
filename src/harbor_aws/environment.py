"""Harbor BaseEnvironment adapter for AWS ECS/Fargate."""

from __future__ import annotations

import logging
from pathlib import Path

from harbor.environments.base import BaseEnvironment, ExecResult
from harbor.models.environment_type import EnvironmentType
from harbor.models.task.config import EnvironmentConfig
from harbor.models.trial.paths import EnvironmentPaths, TrialPaths

from harbor_aws.core import containers, exec
from harbor_aws.core.config import AWSConfig, create_ecs_client, create_s3_client, load_config_from_stack


class AWSEnvironment(BaseEnvironment):
    """AWS ECS/Fargate implementation for Harbor sandboxes.

    Runs each sandbox as a Fargate task. Commands are executed via an S3
    command channel — no SSM/ECS Exec needed. File transfer also goes
    through S3.

    Configuration can be provided either:
    1. Directly via kwargs (subnets, security_groups, etc.)
    2. Via stack_name to auto-read from CloudFormation stack outputs
    """

    def __init__(
        self,
        environment_dir: Path,
        environment_name: str,
        session_id: str,
        trial_paths: TrialPaths,
        task_env_config: EnvironmentConfig,
        # AWS-specific kwargs (passed via --environment-kwarg)
        region: str = "us-east-1",
        profile_name: str | None = None,
        stack_name: str = "harbor-aws",
        cluster_name: str = "harbor-aws",
        subnets: str | list[str] | None = None,
        security_groups: str | list[str] | None = None,
        assign_public_ip: bool = True,
        task_execution_role_arn: str = "",
        task_role_arn: str = "",
        s3_bucket: str = "",
        logger: logging.Logger | None = None,
        **kwargs,
    ):
        """Initialize an AWSEnvironment instance."""
        super().__init__(
            environment_dir=environment_dir,
            environment_name=environment_name,
            session_id=session_id,
            trial_paths=trial_paths,
            task_env_config=task_env_config,
            logger=logger,
            **kwargs,
        )

        # Parse comma-separated strings from CLI kwargs
        subnet_list = _parse_list(subnets) if subnets else []
        sg_list = _parse_list(security_groups) if security_groups else []

        self._aws_config = AWSConfig(
            region=region,
            profile_name=profile_name,
            stack_name=stack_name,
            cluster_name=cluster_name,
            subnets=subnet_list,
            security_groups=sg_list,
            assign_public_ip=assign_public_ip,
            task_execution_role_arn=task_execution_role_arn,
            task_role_arn=task_role_arn,
            s3_bucket=s3_bucket,
        )

        self._ecs_client: object | None = None
        self._s3_client: object | None = None
        self._task_arn: str | None = None
        self._task_definition_arn: str | None = None
        self._s3_prefix: str = ""
        self._config_loaded = bool(subnet_list)

    @staticmethod
    def type() -> EnvironmentType:
        return EnvironmentType("ecs")

    @property
    def is_mounted(self) -> bool:
        return False

    @property
    def supports_gpus(self) -> bool:
        return False

    @property
    def can_disable_internet(self) -> bool:
        return True

    def _validate_definition(self) -> None:
        pass  # prebuilt images only, no Dockerfile needed

    async def _ensure_config(self) -> None:
        """Load config from CloudFormation stack if stack_name was provided."""
        if self._config_loaded:
            return

        if self._aws_config.stack_name:
            self.logger.debug("Loading config from stack '%s'", self._aws_config.stack_name)
            self._aws_config = await load_config_from_stack(
                stack_name=self._aws_config.stack_name,
                region=self._aws_config.region,
                profile_name=self._aws_config.profile_name,
            )
        else:
            self._aws_config.validate()

        self._config_loaded = True

    def _ensure_clients(self) -> None:
        """Initialize boto3 clients."""
        if self._ecs_client is None:
            self._ecs_client = create_ecs_client(self._aws_config)
        if self._s3_client is None:
            self._s3_client = create_s3_client(self._aws_config)

    async def start(self, force_build: bool) -> None:
        """Start an ECS Fargate task with the S3 command daemon."""
        await self._ensure_config()
        self._ensure_clients()

        self._s3_prefix = f"{self._aws_config.s3_prefix}{self.session_id}/"

        # Use prebuilt image from benchmark config
        image_uri = self.task_env_config.docker_image
        if not image_uri:
            raise RuntimeError(
                "No docker_image specified in benchmark config. "
                "harbor-aws only supports prebuilt images."
            )
        self.logger.debug("Using image: %s", image_uri)

        # Register task definition (injects daemon into entrypoint)
        self._task_definition_arn = await containers.register_task_definition(
            self._ecs_client,
            self._aws_config,
            image_uri,
            self.environment_name,
            self.session_id,
            cpus=self.task_env_config.cpus,
            memory_mb=self.task_env_config.memory_mb,
        )

        # Run task
        self._task_arn = await containers.run_task(
            self._ecs_client,
            self._aws_config,
            self._task_definition_arn,
            self.session_id,
        )

        # Wait for task to be running
        await containers.wait_for_task_running(
            self._ecs_client,
            self._aws_config,
            self._task_arn,
        )

        # Wait a few seconds for daemon to start and be ready to accept commands
        import asyncio
        await asyncio.sleep(5)

        # Create required log directories
        mkdir_result = await self.exec(f"mkdir -p {EnvironmentPaths.agent_dir} {EnvironmentPaths.verifier_dir}")
        if mkdir_result.return_code != 0:
            raise RuntimeError(f"Failed to create log directories: {mkdir_result.stderr}")

    async def stop(self, delete: bool) -> None:
        """Stop the Fargate task."""
        try:
            if self._task_arn and self._ecs_client:
                await containers.stop_task(
                    self._ecs_client,
                    self._aws_config,
                    self._task_arn,
                )
            if delete and self._task_definition_arn and self._ecs_client:
                await containers.deregister_task_definition(
                    self._ecs_client,
                    self._task_definition_arn,
                )
        except Exception as e:
            self.logger.warning("Error stopping task: %s", e)
        finally:
            self._task_arn = None
            self._task_definition_arn = None
            self._ecs_client = None
            self._s3_client = None

    async def exec(
        self,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: int | None = None,
    ) -> ExecResult:
        """Execute a command in the Fargate task via S3 command channel."""
        if not self._task_arn:
            raise RuntimeError("Task not running. Call start() first.")

        stdout, stderr, return_code = await exec.exec_command(
            s3_client=self._s3_client,
            bucket=self._aws_config.s3_bucket,
            s3_prefix=self._s3_prefix,
            command=command,
            cwd=cwd,
            env=env,
            timeout_sec=timeout_sec,
        )

        return ExecResult(
            stdout=stdout,
            stderr=stderr,
            return_code=return_code,
        )

    async def upload_file(self, source_path: Path | str, target_path: str) -> None:
        """Upload a file to the container via S3."""
        await exec.upload_file(
            self._s3_client, self._aws_config.s3_bucket, self._s3_prefix,
            Path(source_path), target_path,
        )

    async def upload_dir(self, source_dir: Path | str, target_dir: str) -> None:
        """Upload a directory to the container via S3."""
        await exec.upload_dir(
            self._s3_client, self._aws_config.s3_bucket, self._s3_prefix,
            Path(source_dir), target_dir,
        )

    async def download_file(self, source_path: str, target_path: Path | str) -> None:
        """Download a file from the container via S3."""
        await exec.download_file(
            self._s3_client, self._aws_config.s3_bucket, self._s3_prefix,
            source_path, Path(target_path),
        )

    async def download_dir(self, source_dir: str, target_dir: Path | str) -> None:
        """Download a directory from the container via S3."""
        await exec.download_dir(
            self._s3_client, self._aws_config.s3_bucket, self._s3_prefix,
            source_dir, Path(target_dir),
        )


def _parse_list(value: str | list[str]) -> list[str]:
    """Parse a comma-separated string or list into a list of strings."""
    if isinstance(value, list):
        return value
    return [v.strip() for v in value.split(",") if v.strip()]
