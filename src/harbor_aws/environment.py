"""Harbor BaseEnvironment adapter for AWS EKS/Fargate."""

from __future__ import annotations

import asyncio
import logging
import subprocess
from pathlib import Path

from kubernetes import client

from harbor.environments.base import BaseEnvironment, ExecResult
from harbor.models.environment_type import EnvironmentType
from harbor.models.task.config import EnvironmentConfig
from harbor.models.trial.paths import EnvironmentPaths, TrialPaths

from harbor_aws.core import exec, pods
from harbor_aws.core.config import AWSConfig, create_k8s_client, load_config_from_stack


class AWSEnvironment(BaseEnvironment):
    """AWS EKS/Fargate implementation for Harbor sandboxes.

    Runs each sandbox as a Kubernetes pod on EKS Fargate. Commands are executed
    via `kubectl exec` (WebSocket). File transfer uses `kubectl cp`.

    Configuration can be provided either:
    1. Directly via kwargs (eks_cluster_name, namespace, etc.)
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
        eks_cluster_name: str = "harbor-aws",
        namespace: str = "harbor",
        logger: logging.Logger | None = None,
        **kwargs,
    ):
        super().__init__(
            environment_dir=environment_dir,
            environment_name=environment_name,
            session_id=session_id,
            trial_paths=trial_paths,
            task_env_config=task_env_config,
            logger=logger,
            **kwargs,
        )

        self._aws_config = AWSConfig(
            region=region,
            profile_name=profile_name,
            stack_name=stack_name,
            eks_cluster_name=eks_cluster_name,
            namespace=namespace,
        )

        self._k8s_api: client.CoreV1Api | None = None
        self._pod_name: str | None = None
        self._config_loaded = bool(eks_cluster_name and eks_cluster_name != "harbor-aws" and not stack_name)

    @staticmethod
    def type() -> EnvironmentType:
        return EnvironmentType("eks")

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
        pass  # prebuilt images only

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

    def _ensure_k8s_client(self) -> None:
        """Initialize Kubernetes API client."""
        if self._k8s_api is None:
            self._k8s_api = create_k8s_client(self._aws_config)

    async def start(self, force_build: bool) -> None:
        """Start a Kubernetes pod for the benchmark task."""
        await self._ensure_config()
        self._ensure_k8s_client()

        image_uri = self.task_env_config.docker_image
        if not image_uri:
            raise RuntimeError(
                "No docker_image specified in benchmark config. "
                "harbor-aws only supports prebuilt images."
            )
        self.logger.debug("Using image: %s", image_uri)

        self._pod_name = await pods.create_pod(
            self._k8s_api,
            self._aws_config,
            image_uri,
            self.environment_name,
            self.session_id,
            cpus=self.task_env_config.cpus,
            memory_mb=self.task_env_config.memory_mb,
        )

        await pods.wait_for_pod_running(
            self._k8s_api,
            self._aws_config,
            self._pod_name,
        )

        # Create required log directories
        mkdir_result = await self.exec(f"mkdir -p {EnvironmentPaths.agent_dir} {EnvironmentPaths.verifier_dir}")
        if mkdir_result.return_code != 0:
            raise RuntimeError(f"Failed to create log directories: {mkdir_result.stderr}")

    async def stop(self, delete: bool) -> None:
        """Delete the pod."""
        try:
            if self._pod_name and self._k8s_api:
                await pods.delete_pod(
                    self._k8s_api,
                    self._aws_config,
                    self._pod_name,
                )
        except Exception as e:
            self.logger.warning("Error deleting pod: %s", e)
        finally:
            self._pod_name = None
            self._k8s_api = None

    async def exec(
        self,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: int | None = None,
    ) -> ExecResult:
        """Execute a command in the pod via Kubernetes exec."""
        if not self._pod_name:
            raise RuntimeError("Pod not running. Call start() first.")

        stdout, stderr, return_code = await exec.exec_command(
            api=self._k8s_api,
            pod_name=self._pod_name,
            namespace=self._aws_config.namespace,
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
        await self._kubectl_cp(str(source_path), f"{self._pod_ref}:{target_path}")

    async def upload_dir(self, source_dir: Path | str, target_dir: str) -> None:
        await self._kubectl_cp(str(source_dir), f"{self._pod_ref}:{target_dir}")

    async def download_file(self, source_path: str, target_path: Path | str) -> None:
        Path(target_path).parent.mkdir(parents=True, exist_ok=True)
        await self._kubectl_cp(f"{self._pod_ref}:{source_path}", str(target_path))

    async def download_dir(self, source_dir: str, target_dir: Path | str) -> None:
        Path(target_dir).mkdir(parents=True, exist_ok=True)
        await self._kubectl_cp(f"{self._pod_ref}:{source_dir}", str(target_dir))

    @property
    def _pod_ref(self) -> str:
        """Kubernetes pod reference for kubectl: namespace/pod-name."""
        return f"{self._aws_config.namespace}/{self._pod_name}"

    async def _kubectl_cp(self, src: str, dst: str) -> None:
        cmd = ["kubectl", "cp", src, dst, "-c", "main"]
        result = await asyncio.to_thread(
            subprocess.run, cmd, capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            raise RuntimeError(f"kubectl cp failed: {result.stderr.strip()}")
