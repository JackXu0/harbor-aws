"""Harbor BaseEnvironment adapter for AWS ECS/Fargate."""

from __future__ import annotations

import logging
from pathlib import Path

from harbor.environments.base import BaseEnvironment, ExecResult
from harbor.models.environment_type import EnvironmentType
from harbor.models.task.config import EnvironmentConfig
from harbor.models.trial.paths import EnvironmentPaths, TrialPaths

from harbor_aws.core import containers, exec, files, images
from harbor_aws.core.clients import AWSClients, ECSClientManager
from harbor_aws.core.config import AWSConfig
from harbor_aws.core.stack import load_config_from_stack


class AWSEnvironment(BaseEnvironment):
    """AWS ECS/Fargate implementation for Harbor sandboxes.

    Runs each sandbox as a Fargate task with ECS Exec for command execution.
    Images are built via CodeBuild and stored in ECR. File transfer uses
    base64-over-exec.

    Configuration can be provided either:
    1. Directly via kwargs (subnets, security_groups, etc.)
    2. Via stack_name to auto-read from CloudFormation stack outputs

    Usage with Harbor CLI:
        harbor trials start -p ./task \\
            --environment-import-path harbor_aws.environment:AWSEnvironment \\
            --ek stack_name=harbor-aws
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
        stack_name: str | None = None,
        cluster_name: str = "harbor-aws",
        subnets: str | list[str] | None = None,
        security_groups: str | list[str] | None = None,
        assign_public_ip: bool = True,
        task_execution_role_arn: str = "",
        task_role_arn: str = "",
        ecr_repository_uri: str = "",
        codebuild_project_name: str = "harbor-aws-builder",
        s3_bucket: str = "",
        s3_prefix: str = "harbor-aws/",
        build_timeout_minutes: int = 30,
        auto_provision: bool = True,
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
            ecr_repository_uri=ecr_repository_uri,
            codebuild_project_name=codebuild_project_name,
            s3_bucket=s3_bucket,
            s3_prefix=s3_prefix,
            build_timeout_minutes=build_timeout_minutes,
            auto_provision=auto_provision if isinstance(auto_provision, bool) else auto_provision != "false",
        )

        self._client_manager: ECSClientManager | None = None
        self._clients: AWSClients | None = None
        self._task_arn: str | None = None
        self._task_definition_arn: str | None = None
        self._config_loaded = bool(not stack_name and subnet_list)

    @staticmethod
    def type() -> EnvironmentType:
        # Return a string value; Harbor's import_path mechanism doesn't
        # require this to be in the EnvironmentType enum
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
        dockerfile = self.environment_dir / "Dockerfile"
        if not dockerfile.exists():
            raise FileNotFoundError(f"{dockerfile} not found. AWSEnvironment requires a Dockerfile.")

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
                auto_provision=self._aws_config.auto_provision,
            )
        else:
            self._aws_config.validate()

        self._config_loaded = True

    async def _ensure_clients(self) -> None:
        """Initialize client manager and get clients."""
        if self._client_manager is None:
            self._client_manager = await ECSClientManager.get_instance()
        if self._clients is None:
            self._clients = await self._client_manager.get_clients(self._aws_config)

    async def start(self, force_build: bool) -> None:
        """Start an ECS Fargate task."""
        # Validate session-manager-plugin is available
        await exec.check_session_manager_plugin()

        await self._ensure_config()
        await self._ensure_clients()
        assert self._clients is not None

        image_name = self.environment_name
        image_tag = "latest"
        ecr_repo_name = self._aws_config.ecr_repository_uri.rsplit("/", 1)[-1]

        # Build image if needed
        if force_build:
            await images.build_and_push_image(
                self._clients.codebuild,
                self._clients.s3,
                self._aws_config,
                self.environment_dir,
                image_name,
                image_tag,
            )
        else:
            if not await images.image_exists(self._clients.ecr, ecr_repo_name, f"{image_name}-{image_tag}"):
                self.logger.debug("Image not found in ECR, building...")
                await images.build_and_push_image(
                    self._clients.codebuild,
                    self._clients.s3,
                    self._aws_config,
                    self.environment_dir,
                    image_name,
                    image_tag,
                )
            else:
                self.logger.debug("Using existing image from ECR")

        image_uri = images.get_image_uri(self._aws_config, image_name, image_tag)

        # Register task definition
        storage_gb = max(21, self.task_env_config.storage_mb // 1024 + 1)
        self._task_definition_arn = await containers.register_task_definition(
            self._clients.ecs,
            self._aws_config,
            image_uri,
            self.environment_name,
            cpus=self.task_env_config.cpus,
            memory_mb=self.task_env_config.memory_mb,
            storage_gb=storage_gb,
        )

        # Run task
        self._task_arn = await containers.run_task(
            self._clients.ecs,
            self._aws_config,
            self._task_definition_arn,
            self.session_id,
        )

        # Wait for task and ECS Exec agent to be ready
        await containers.wait_for_task_running(
            self._clients.ecs,
            self._aws_config,
            self._task_arn,
        )

        # Create required log directories
        mkdir_result = await self.exec(f"mkdir -p {EnvironmentPaths.agent_dir} {EnvironmentPaths.verifier_dir}")
        if mkdir_result.return_code != 0:
            raise RuntimeError(f"Failed to create log directories: {mkdir_result.stderr}")

    async def stop(self, delete: bool) -> None:
        """Stop the Fargate task."""
        try:
            if self._task_arn and self._clients:
                await containers.stop_task(
                    self._clients.ecs,
                    self._aws_config,
                    self._task_arn,
                )
            if delete and self._task_definition_arn and self._clients:
                await containers.deregister_task_definition(
                    self._clients.ecs,
                    self._task_definition_arn,
                )
        except Exception as e:
            self.logger.warning("Error stopping task: %s", e)
        finally:
            self._task_arn = None
            self._task_definition_arn = None

            if self._client_manager:
                try:
                    await self._client_manager.release_clients()
                except Exception as e:
                    self.logger.error("Error releasing AWS clients: %s", e)
                finally:
                    self._client_manager = None
                    self._clients = None

    async def exec(
        self,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: int | None = None,
    ) -> ExecResult:
        """Execute a command in the Fargate task via ECS Exec."""
        if not self._task_arn:
            raise RuntimeError("Task not running. Call start() first.")

        stdout, stderr, return_code = await exec.exec_command(
            config=self._aws_config,
            task_arn=self._task_arn,
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
        """Upload a file to the container."""
        await files.upload_file(self._exec_fn, Path(source_path), target_path)

    async def upload_dir(self, source_dir: Path | str, target_dir: str) -> None:
        """Upload a directory to the container."""
        await files.upload_dir(self._exec_fn, Path(source_dir), target_dir)

    async def download_file(self, source_path: str, target_path: Path | str) -> None:
        """Download a file from the container."""
        await files.download_file(self._exec_fn, source_path, Path(target_path))

    async def download_dir(self, source_dir: str, target_dir: Path | str) -> None:
        """Download a directory from the container."""
        await files.download_dir(self._exec_fn, source_dir, Path(target_dir))

    async def _exec_fn(
        self,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: int | None = None,
    ) -> tuple[str | None, str | None, int]:
        """Bound exec function for file transfer operations."""
        if not self._task_arn:
            raise RuntimeError("Task not running.")
        return await exec.exec_command(
            config=self._aws_config,
            task_arn=self._task_arn,
            command=command,
            cwd=cwd,
            env=env,
            timeout_sec=timeout_sec,
        )


def _parse_list(value: str | list[str]) -> list[str]:
    """Parse a comma-separated string or list into a list of strings."""
    if isinstance(value, list):
        return value
    return [v.strip() for v in value.split(",") if v.strip()]
