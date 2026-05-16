"""Harbor BaseEnvironment adapter for AWS EKS/Fargate.

AWSEnvironment implements Harbor's sandbox interface (start, exec,
upload/download, stop) by running each trial in a Fargate pod.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
from pathlib import Path

from harbor.environments.base import BaseEnvironment, ExecResult
from harbor.models.environment_type import EnvironmentType
from harbor.models.task.config import EnvironmentConfig
from harbor.models.trial.paths import EnvironmentPaths, TrialPaths

from harbor_aws.core import images, pods
from harbor_aws.core.config import ClusterConfig, TrialOptions
from harbor_aws.core.remote_shell import RemoteShell
from harbor_aws.runtime import runtime

RUNNER_AUTH_TIMEOUT_SEC = 60.0


class AWSEnvironment(BaseEnvironment):

    def __init__(
        self,
        environment_dir: Path,
        environment_name: str,
        session_id: str,
        trial_paths: TrialPaths,
        task_env_config: EnvironmentConfig,
        *,
        region: str = "us-east-1",
        role_arn: str | None = None,
        stack_name: str = "harbor-aws",
        bedrock: bool = False,
        cpus: int | None = None,
        memory_mb: int | None = None,
        pod_timeout_sec: int = 14400,
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

        # Stack lookup parameters — only used until ClusterConfig is loaded.
        self.region = region
        self.role_arn = role_arn
        self.stack_name = stack_name

        self.trial_options = TrialOptions(
            pod_timeout_sec=int(pod_timeout_sec),
            use_bedrock=bedrock,
        )

        self.cpus_override = int(cpus) if cpus is not None else None
        self.memory_mb_override = int(memory_mb) if memory_mb is not None else None

        # Populated by start() once runtime.get_cluster_config() resolves.
        self.cluster_config: ClusterConfig | None = None

        self.pod_name: str | None = None
        self.remote_shell: RemoteShell | None = None

    # -- Lifecycle ---------------------------------------------------------

    async def start(self, force_build: bool) -> None:
        """Start a Kubernetes pod for the benchmark task."""
        if self.remote_shell is not None or self.pod_name is not None:
            self.logger.warning("start() retried for session %s; cleaning up previous attempt", self.session_id)
            await self.stop(delete=True)

        self.cluster_config = await runtime.get_cluster_config(self.stack_name, self.region, self.role_arn)

        image_uri, dockerfile_commands = await images.resolve_image(
            self.environment_dir, self.task_env_config, self.cluster_config, self.logger,
        )

        pod_cpus = self.cpus_override or self.task_env_config.cpus
        pod_memory = self.memory_mb_override or self.task_env_config.memory_mb

        # Per-trial token used by the trial pod to authenticate to the control pod.
        trial_token = secrets.token_urlsafe(16)

        self.remote_shell = RemoteShell(
            trial_id=self.session_id,
            trial_token=trial_token,
            nlb_url=runtime.get_nlb_url(),
            bearer_token=self.cluster_config.bearer_token,
            session=runtime.get_session(),
        )

        register_task = asyncio.create_task(self.remote_shell.connect())

        try:
            service_account = self.cluster_config.k8s_service_account if self.trial_options.use_bedrock else None

            self.pod_name = await self.remote_shell.create_pod(
                image_uri=image_uri,
                environment_name=self.environment_name,
                cpus=pod_cpus,
                memory_mb=pod_memory,
                pod_timeout_sec=self.trial_options.pod_timeout_sec,
                service_account=service_account,
            )

            await pods.wait_for_pod_running(self.cluster_config.namespace, self.pod_name)
            try:
                await asyncio.wait_for(register_task, timeout=RUNNER_AUTH_TIMEOUT_SEC)
            except TimeoutError:
                raise RuntimeError(
                    f"Pod {self.pod_name} is Running but runner did not authenticate within "
                    f"{RUNNER_AUTH_TIMEOUT_SEC:.0f}s — check runner.sh and the harbor-runner ConfigMap"
                ) from None
        except Exception:
            register_task.cancel()
            raise

        # Replay the Dockerfile's RUN/WORKDIR/ENV inside the running pod for tier 2 image resolve path
        for i, cmd in enumerate(dockerfile_commands):
            self.logger.debug("[start] Dockerfile cmd %d/%d: %s", i + 1, len(dockerfile_commands), cmd[:80])
            try:
                result = await self.exec(cmd, timeout_sec=300)
            except Exception as e:
                raise RuntimeError(f"Dockerfile cmd {i+1}/{len(dockerfile_commands)} failed on pod {self.pod_name}: {cmd[:80]}") from e
            if result.return_code != 0:
                self.logger.warning("Dockerfile setup command failed (rc=%d): %s", result.return_code, cmd[:100])

        # Create the agent + verifier log dirs inside the pod (Harbor requirement)
        mkdir_result = await self.exec(f"mkdir -p {EnvironmentPaths.agent_dir} {EnvironmentPaths.verifier_dir}")

        if mkdir_result.return_code != 0:
            raise RuntimeError(f"Failed to create log directories: {mkdir_result.stderr}")

    async def stop(self, delete: bool) -> None:
        try:
            if self.pod_name:
                await runtime.delete_pod(self.pod_name)
            if self.remote_shell:
                await self.remote_shell.close()
                self.remote_shell = None
        except Exception as e:
            self.logger.warning("Error deleting pod: %s", e)
        finally:
            self.pod_name = None

    async def exec(
        self,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: int | None = None,
        user: str | int | None = None,
    ) -> ExecResult:
        if self.remote_shell is None:
            raise RuntimeError("Pod not running. Call start() first.")
            
        if user is not None and user not in ("root", 0, "0"):
            raise NotImplementedError(f"AWSEnvironment.exec(user={user!r}) is not supported")
        stdout, stderr, return_code = await self.remote_shell.run(
            command, cwd=cwd, env=env, timeout_sec=timeout_sec or 900,
        )
        return ExecResult(stdout=stdout, stderr=stderr, return_code=return_code)

    async def upload_file(self, source_path: Path | str, target_path: str) -> None:
        if self.remote_shell is None:
            raise RuntimeError("Pod not running. Call start() first.")
        await self.remote_shell.upload_file(source_path, target_path)

    async def upload_dir(self, source_dir: Path | str, target_dir: str) -> None:
        if self.remote_shell is None:
            raise RuntimeError("Pod not running. Call start() first.")
        await self.remote_shell.upload_dir(source_dir, target_dir)

    async def download_file(self, source_path: str, target_path: Path | str) -> None:
        if self.remote_shell is None:
            raise RuntimeError("Pod not running. Call start() first.")
        await self.remote_shell.download_file(source_path, target_path)

    async def download_dir(self, source_dir: str, target_dir: Path | str) -> None:
        if self.remote_shell is None:
            raise RuntimeError("Pod not running. Call start() first.")
        await self.remote_shell.download_dir(source_dir, target_dir)

    async def attach(self) -> None:
        if not self.pod_name or not self.cluster_config:
            raise RuntimeError("attach: pod not running")
        os.execvp("kubectl", [
            "kubectl", "exec", "-it",
            "-n", self.cluster_config.namespace,
            self.pod_name, "--", "/bin/bash",
        ])

    # -- BaseEnvironment capability declarations ---------------------------

    @staticmethod
    def type() -> EnvironmentType:
        # TODO: switch to EnvironmentType.EKS once https://github.com/harbor-framework/harbor/pull/1634 lands.
        return EnvironmentType.DOCKER

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
        pass
