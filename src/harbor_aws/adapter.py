"""Harbor BaseEnvironment adapter for AWS EKS/Fargate."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from pathlib import Path

from harbor.environments.base import BaseEnvironment, ExecResult
from harbor.models.environment_type import EnvironmentType
from harbor.models.task.config import EnvironmentConfig
from harbor.models.trial.paths import EnvironmentPaths, TrialPaths
from kubernetes import client

from harbor_aws.core import exec, files, pods
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
            # Cache stack config at class level to avoid repeated CloudFormation calls
            # (which fail if AWS credentials expire mid-run)
            if AWSEnvironment._cached_stack_config is not None:
                self._aws_config = AWSEnvironment._cached_stack_config
            else:
                self.logger.debug("Loading config from stack '%s'", self._aws_config.stack_name)
                self._aws_config = await load_config_from_stack(
                    stack_name=self._aws_config.stack_name,
                    region=self._aws_config.region,
                    profile_name=self._aws_config.profile_name,
                )
                AWSEnvironment._cached_stack_config = self._aws_config
        else:
            self._aws_config.validate()

        # Resolve account_id for ECR pull-through cache if not already set
        if not self._aws_config.account_id:
            try:
                import boto3
                session = boto3.Session(
                    profile_name=self._aws_config.profile_name,
                    region_name=self._aws_config.region,
                )
                self._aws_config.account_id = await asyncio.to_thread(
                    lambda: session.client("sts").get_caller_identity()["Account"]
                )
            except Exception as e:
                self.logger.warning("Could not resolve AWS account ID for ECR cache: %s", e)

        self._config_loaded = True

    _shared_k8s_api = None

    def _ensure_k8s_client(self) -> None:
        """Initialize Kubernetes API client (shared across instances)."""
        if self._k8s_api is None:
            if AWSEnvironment._shared_k8s_api is None:
                AWSEnvironment._shared_k8s_api = create_k8s_client(self._aws_config)
            self._k8s_api = AWSEnvironment._shared_k8s_api

    _cached_stack_config: AWSConfig | None = None
    _docker_secret_checked = False
    _docker_secret_name: str | None = None

    # Limit concurrent image pulls to avoid Docker Hub rate limits.
    # Only this many pods will be in the create+pull phase at a time;
    # once a pod is Running the slot is released for the next one.
    _IMAGE_PULL_CONCURRENCY = 500
    _image_pull_semaphore: asyncio.Semaphore | None = None

    @classmethod
    def _get_pull_semaphore(cls) -> asyncio.Semaphore:
        if cls._image_pull_semaphore is None:
            cls._image_pull_semaphore = asyncio.Semaphore(cls._IMAGE_PULL_CONCURRENCY)
        return cls._image_pull_semaphore

    async def _ensure_docker_pull_secret(self) -> None:
        """Create imagePullSecret from ~/.docker/config.json if not already present."""
        if AWSEnvironment._docker_secret_checked:
            return

        secret_name = "dockerhub-creds"
        docker_cfg = Path.home() / ".docker" / "config.json"
        if not docker_cfg.exists():
            AWSEnvironment._docker_secret_checked = True
            return

        # Check if Docker Hub auth is actually configured
        try:
            cfg_data = json.loads(docker_cfg.read_text())
            has_dockerhub = any(
                "docker.io" in k or "index.docker.io" in k
                for k in cfg_data.get("auths", {})
            )
            if not has_dockerhub:
                AWSEnvironment._docker_secret_checked = True
                return
        except (json.JSONDecodeError, OSError):
            AWSEnvironment._docker_secret_checked = True
            return

        try:
            await asyncio.to_thread(
                self._k8s_api.read_namespaced_secret,
                name=secret_name,
                namespace=self._aws_config.namespace,
            )
        except client.ApiException as e:
            if e.status != 404:
                raise
            secret = client.V1Secret(
                metadata=client.V1ObjectMeta(name=secret_name),
                type="kubernetes.io/dockerconfigjson",
                data={".dockerconfigjson": base64.b64encode(docker_cfg.read_bytes()).decode()},
            )
            await asyncio.to_thread(
                self._k8s_api.create_namespaced_secret,
                namespace=self._aws_config.namespace,
                body=secret,
            )
            self.logger.debug("Created %s secret from ~/.docker/config.json", secret_name)

        AWSEnvironment._docker_secret_name = secret_name
        AWSEnvironment._docker_secret_checked = True

    def _ecr_image_uri(self, image: str) -> str:
        """Rewrite a Docker Hub image URI to use the ECR pull-through cache.

        docker.io/library/ubuntu:22.04  →  …/docker-hub/library/ubuntu:22.04
        swebench/sweb.eval.x86_64:latest →  …/docker-hub/swebench/sweb.eval.x86_64:latest

        Images already pointing at ECR or other registries are returned unchanged.
        """
        # Strip explicit docker.io prefix if present
        stripped = re.sub(r"^(docker\.io|registry-1\.docker\.io)/", "", image)

        # Already an ECR or other registry URI — leave unchanged
        if re.match(r"^[\w.-]+\.amazonaws\.com/", stripped) or re.match(r"^[\w.-]+\.\w{2,}/", stripped):
            return image

        # Docker Hub official images have no namespace — add "library/"
        if "/" not in stripped.split(":")[0]:
            stripped = f"library/{stripped}"

        account = self._aws_config.account_id
        region = self._aws_config.region

        if not account:
            self.logger.debug("No account_id available, skipping ECR rewrite for %s", image)
            return image

        return f"{account}.dkr.ecr.{region}.amazonaws.com/docker-hub/{stripped}"

    def _parse_dockerfile(self) -> tuple[str | None, list[str]]:
        """Parse Dockerfile to extract base image and RUN/WORKDIR commands.

        Returns (image, setup_commands) where setup_commands are shell commands
        to run after pod creation to replicate RUN and WORKDIR instructions.
        """
        dockerfile = self.environment_dir / "Dockerfile"
        if not dockerfile.exists():
            return None, []

        image = None
        commands: list[str] = []

        for line in dockerfile.read_text().splitlines():
            stripped = line.strip()
            # Skip comments and empty lines
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.upper().startswith("FROM ") and image is None:
                image = stripped.split()[1]
            elif stripped.upper().startswith("RUN "):
                commands.append(stripped[4:].strip())
            elif stripped.upper().startswith("WORKDIR "):
                path = stripped[8:].strip()
                commands.append(f"mkdir -p {path} && cd {path}")

        return image, commands

    async def start(self, force_build: bool) -> None:
        """Start a Kubernetes pod for the benchmark task."""
        await self._ensure_config()
        self._ensure_k8s_client()
        await self._ensure_docker_pull_secret()

        image_uri = self.task_env_config.docker_image
        dockerfile_commands: list[str] = []
        if not image_uri:
            image_uri, dockerfile_commands = self._parse_dockerfile()
        if not image_uri:
            raise RuntimeError(
                "No docker_image specified and no Dockerfile found. "
                "harbor-aws only supports prebuilt images."
            )
        # Rewrite Docker Hub images to use ECR pull-through cache
        image_uri = self._ecr_image_uri(image_uri)
        self.logger.debug("Using image: %s", image_uri)

        # Limit concurrent image pulls.  With ECR pull-through cache the
        # limit is high (500) since there are no Docker Hub rate limits.
        # The semaphore is released once the image is pulled (not when the
        # pod is fully running), so Fargate scheduling time doesn't block
        # other pods from starting their image pulls.
        async with self._get_pull_semaphore():
            self.logger.debug("[start] creating pod for %s", self.environment_name)
            self._pod_name = await pods.create_pod(
                self._k8s_api,
                self._aws_config,
                image_uri,
                self.environment_name,
                self.session_id,
                cpus=self.task_env_config.cpus,
                memory_mb=self.task_env_config.memory_mb,
                image_pull_secret=AWSEnvironment._docker_secret_name,
            )
            self.logger.debug("[start] pod created: %s", self._pod_name)

            await pods.wait_for_image_pulled(
                self._k8s_api,
                self._aws_config,
                self._pod_name,
            )

        # Wait for the pod to be fully running (outside the semaphore)
        try:
            await pods.wait_for_pod_running(
                self._k8s_api,
                self._aws_config,
                self._pod_name,
            )
        except Exception as e:
            self.logger.error("[start] wait_for_pod_running FAILED for %s: %s: %s", self._pod_name, type(e).__name__, str(e)[:200])
            raise
        self.logger.debug("[start] pod running: %s", self._pod_name)

        # Run Dockerfile RUN/WORKDIR commands if image was extracted from Dockerfile
        for i, cmd in enumerate(dockerfile_commands):
            self.logger.debug("[start] running Dockerfile command %d/%d: %s", i + 1, len(dockerfile_commands), cmd[:80])
            try:
                result = await self.exec(cmd, timeout_sec=300)
            except Exception as e:
                self.logger.error("[start] Dockerfile cmd %d FAILED for %s: %s: %s", i + 1, self._pod_name, type(e).__name__, str(e)[:200])
                raise
            if result.return_code != 0:
                self.logger.warning("Dockerfile setup command failed (rc=%d): %s", result.return_code, cmd[:100])

        # Create required log directories
        self.logger.debug("[start] creating log dirs in pod %s", self._pod_name)
        try:
            mkdir_result = await self.exec(f"mkdir -p {EnvironmentPaths.agent_dir} {EnvironmentPaths.verifier_dir}")
        except Exception as e:
            self.logger.error("[start] mkdir FAILED for %s: %s: %s", self._pod_name, type(e).__name__, str(e)[:200])
            raise
        if mkdir_result.return_code != 0:
            raise RuntimeError(f"Failed to create log directories: {mkdir_result.stderr}")

        # Install 'script' command if missing — terminus-2 requires it for tmux sessions.
        # Some SWE-bench Pro images (Go, JS repos) don't include bsdutils.
        check = await self.exec("command -v script")
        if check.return_code != 0:
            self.logger.debug("[start] installing bsdutils (script) in pod %s", self._pod_name)
            install = await self.exec(
                "apt-get update -qq && apt-get install -y -qq bsdutils 2>/dev/null"
                " || apk add --no-cache util-linux 2>/dev/null"
                " || yum install -y util-linux 2>/dev/null"
                " || true",
                timeout_sec=120,
            )
            if install.return_code != 0:
                self.logger.warning("[start] bsdutils install may have failed (rc=%d) in %s", install.return_code, self._pod_name)

        self.logger.debug("[start] pod %s fully ready", self._pod_name)

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
        await files.upload_file(
            self._pod_name, self._aws_config.namespace, str(source_path), target_path,
        )

    async def upload_dir(self, source_dir: Path | str, target_dir: str) -> None:
        await files.upload_dir(
            self._pod_name, self._aws_config.namespace, str(source_dir), target_dir,
        )

    async def download_file(self, source_path: str, target_path: Path | str) -> None:
        await files.download_file(
            self._pod_name, self._aws_config.namespace, source_path, str(target_path),
        )

    async def download_dir(self, source_dir: str, target_dir: Path | str) -> None:
        await files.download_dir(
            self._pod_name, self._aws_config.namespace, source_dir, str(target_dir),
        )
