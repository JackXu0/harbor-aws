"""Harbor BaseEnvironment adapter for AWS EKS/Fargate."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
import subprocess
from dataclasses import replace
from pathlib import Path

from harbor.environments.base import BaseEnvironment, ExecResult
from harbor.models.environment_type import EnvironmentType
from harbor.models.task.config import EnvironmentConfig
from harbor.models.trial.paths import EnvironmentPaths, TrialPaths
from kubernetes import client

from harbor_aws.core import exec, files, pods
from harbor_aws.core.config import AWSConfig, create_k8s_client, load_config_from_stack

# Fargate resource profiles for known benchmark images.
# Matched top-to-bottom by regex against the docker image URI.
_RESOURCE_PROFILES: list[tuple[str, int, int]] = [
    # (image_pattern,  cpus,  memory_mb)
    (r"swebench/sweb\.eval", 2, 8192),
]


def _match_resource_profile(image: str) -> tuple[int, int] | None:
    """Return (cpus, memory_mb) if *image* matches a known profile."""
    for pattern, cpus, mem in _RESOURCE_PROFILES:
        if re.search(pattern, image):
            return cpus, mem
    return None


class AWSEnvironment(BaseEnvironment):
    """AWS EKS/Fargate sandbox for Harbor benchmarks.

    Each sandbox runs as a Kubernetes pod on EKS Fargate. Commands execute via
    WebSocket exec; files transfer via tar-over-exec.
    """

    # -- Class-level shared state (avoid repeated API calls across instances) --
    _cached_stack_config: AWSConfig | None = None
    _config_lock: asyncio.Lock | None = None
    _shared_k8s_api = None
    _docker_secret_checked = False
    _docker_secret_name: str | None = None
    _image_pull_semaphore: asyncio.Semaphore | None = None
    _image_pull_semaphore_size: int = 0
    _setup_semaphore: asyncio.Semaphore | None = None
    _build_locks: dict[str, asyncio.Lock] = {}

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
        ecr_cache: bool = False,
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

        self._aws_config = AWSConfig(
            region=region,
            role_arn=role_arn,
            stack_name=stack_name,
            ecr_cache=ecr_cache,
        )
        self._bedrock = bedrock

        self._cpus_override = int(cpus) if cpus is not None else None
        self._memory_mb_override = int(memory_mb) if memory_mb is not None else None
        self._pod_timeout_sec = int(pod_timeout_sec)

        self._k8s_api: client.CoreV1Api | None = None
        self._pod_name: str | None = None

    # -- Properties --------------------------------------------------------

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
        pass

    # -- Initialization helpers --------------------------------------------

    async def _ensure_config(self) -> None:
        """Load cluster config from CloudFormation (once) and apply per-instance overrides."""
        if AWSEnvironment._cached_stack_config is None:
            if AWSEnvironment._config_lock is None:
                AWSEnvironment._config_lock = asyncio.Lock()
            async with AWSEnvironment._config_lock:
                if AWSEnvironment._cached_stack_config is None:
                    self.logger.debug("Loading config from stack '%s'", self._aws_config.stack_name)
                    AWSEnvironment._cached_stack_config = await load_config_from_stack(
                        stack_name=self._aws_config.stack_name,
                        region=self._aws_config.region,
                        role_arn=self._aws_config.role_arn,
                    )

        self._aws_config = replace(
            AWSEnvironment._cached_stack_config,
            ecr_cache=self._aws_config.ecr_cache,
            pod_timeout_sec=self._pod_timeout_sec,
            k8s_service_account=AWSEnvironment._cached_stack_config.k8s_service_account if self._bedrock else None,
        )

    def _ensure_k8s_client(self) -> None:
        """Initialize Kubernetes API client (shared across instances)."""
        if self._k8s_api is None:
            if AWSEnvironment._shared_k8s_api is None:
                AWSEnvironment._shared_k8s_api = create_k8s_client(self._aws_config)
            self._k8s_api = AWSEnvironment._shared_k8s_api

    @classmethod
    def _get_setup_semaphore(cls) -> asyncio.Semaphore:
        if cls._setup_semaphore is None:
            cls._setup_semaphore = asyncio.Semaphore(500)
        return cls._setup_semaphore

    @classmethod
    def _get_pull_semaphore(cls, ecr_cache: bool = False) -> asyncio.Semaphore:
        limit = 500 if ecr_cache else 50
        if cls._image_pull_semaphore is None or cls._image_pull_semaphore_size != limit:
            cls._image_pull_semaphore = asyncio.Semaphore(limit)
            cls._image_pull_semaphore_size = limit
        return cls._image_pull_semaphore

    # -- Docker Hub credentials --------------------------------------------

    async def _ensure_docker_pull_secret(self) -> None:
        """Create imagePullSecret from ~/.docker/config.json if not already present."""
        if AWSEnvironment._docker_secret_checked:
            return
        AWSEnvironment._docker_secret_checked = True

        docker_cfg = Path.home() / ".docker" / "config.json"
        if not docker_cfg.exists():
            return
        try:
            auths = json.loads(docker_cfg.read_text()).get("auths", {})
        except (json.JSONDecodeError, OSError):
            return
        if not any("docker.io" in k for k in auths):
            return

        secret_name = "dockerhub-creds"
        secret = client.V1Secret(
            metadata=client.V1ObjectMeta(name=secret_name),
            type="kubernetes.io/dockerconfigjson",
            data={".dockerconfigjson": base64.b64encode(docker_cfg.read_bytes()).decode()},
        )
        try:
            await asyncio.to_thread(
                self._k8s_api.create_namespaced_secret,
                namespace=self._aws_config.namespace, body=secret,
            )
        except client.ApiException as e:
            if e.status != 409:
                raise
        AWSEnvironment._docker_secret_name = secret_name

    # -- Image helpers -----------------------------------------------------

    def _ecr_image_uri(self, image: str) -> str:
        """Rewrite a Docker Hub image to use the ECR pull-through cache.

        ``alexgshaw/foo:tag`` → ``<account>.dkr.ecr.<region>.amazonaws.com/docker-hub/alexgshaw/foo:tag``

        Non-Docker-Hub images are returned unchanged.
        """
        stripped = re.sub(r"^(docker\.io|registry-1\.docker\.io)/", "", image)

        if re.match(r"^[\w.-]+\.amazonaws\.com/", stripped) or re.match(r"^[\w.-]+\.\w{2,}/", stripped):
            return image

        if "/" not in stripped.split(":")[0]:
            stripped = f"library/{stripped}"

        account = self._aws_config.account_id
        region = self._aws_config.region

        if not account:
            self.logger.debug("No account_id, skipping ECR rewrite for %s", image)
            return image

        return f"{account}.dkr.ecr.{region}.amazonaws.com/docker-hub/{stripped}"

    def _parse_dockerfile(self) -> tuple[str | None, list[str]]:
        """Extract base image and RUN/WORKDIR commands from Dockerfile."""
        dockerfile = self.environment_dir / "Dockerfile"
        if not dockerfile.exists():
            return None, []

        image = None
        commands: list[str] = []

        # Join backslash-continued lines before parsing
        raw_lines = dockerfile.read_text().splitlines()
        logical_lines: list[str] = []
        current = ""
        for raw in raw_lines:
            if raw.rstrip().endswith("\\"):
                current += raw.rstrip()[:-1]
            else:
                current += raw
                logical_lines.append(current)
                current = ""
        if current:
            logical_lines.append(current)

        for line in logical_lines:
            stripped = line.strip()
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

    # -- Image build & cache -----------------------------------------------

    _ECR_REPO = "harbor-build"

    async def _get_or_build_image(self) -> str | None:
        """Build a Docker image from the environment Dockerfile and cache it in ECR.

        Returns the ECR image URI if successful, None if no Dockerfile or Docker unavailable.
        """
        dockerfile = self.environment_dir / "Dockerfile"
        if not dockerfile.exists():
            return None

        tag = hashlib.sha256(dockerfile.read_bytes()).hexdigest()[:16]
        account = self._aws_config.account_id
        region = self._aws_config.region
        ecr_uri = f"{account}.dkr.ecr.{region}.amazonaws.com/{self._ECR_REPO}:{tag}"

        # Per-tag lock to avoid duplicate builds
        if tag not in AWSEnvironment._build_locks:
            AWSEnvironment._build_locks[tag] = asyncio.Lock()

        async with AWSEnvironment._build_locks[tag]:
            if await asyncio.to_thread(self._ecr_image_exists, tag):
                self.logger.debug("Using cached image %s", ecr_uri)
                return ecr_uri

            self.logger.info("Building and pushing image %s", ecr_uri)
            await asyncio.to_thread(self._docker_build_and_push, ecr_uri)
            return ecr_uri

    def _ecr_image_exists(self, tag: str) -> bool:
        session = self._aws_config.create_boto3_session()
        ecr = session.client("ecr", region_name=self._aws_config.region)
        try:
            ecr.describe_images(repositoryName=self._ECR_REPO, imageIds=[{"imageTag": tag}])
            return True
        except (ecr.exceptions.ImageNotFoundException, ecr.exceptions.RepositoryNotFoundException):
            return False

    def _docker_build_and_push(self, ecr_uri: str) -> None:
        session = self._aws_config.create_boto3_session()
        ecr = session.client("ecr", region_name=self._aws_config.region)

        # Ensure repo exists with 90-day lifecycle policy
        try:
            ecr.describe_repositories(repositoryNames=[self._ECR_REPO])
        except ecr.exceptions.RepositoryNotFoundException:
            ecr.create_repository(repositoryName=self._ECR_REPO)
            ecr.put_lifecycle_policy(repositoryName=self._ECR_REPO, lifecyclePolicyText=json.dumps(
                {"rules": [{"rulePriority": 1, "action": {"type": "expire"},
                 "selection": {"tagStatus": "any", "countType": "sinceImagePushed",
                               "countUnit": "days", "countNumber": 90}}]},
            ))

        # Login to ECR
        auth = ecr.get_authorization_token()["authorizationData"][0]
        token = base64.b64decode(auth["authorizationToken"]).decode()
        username, password = token.split(":", 1)
        subprocess.run(
            ["docker", "login", "--username", username, "--password-stdin", auth["proxyEndpoint"]],
            input=password, capture_output=True, text=True, check=True,
        )

        # Build and push
        subprocess.run(["docker", "build", "-t", ecr_uri, str(self.environment_dir)], check=True, capture_output=True, text=True)
        subprocess.run(["docker", "push", ecr_uri], check=True, capture_output=True, text=True)

    # -- Lifecycle ---------------------------------------------------------

    async def start(self, force_build: bool) -> None:
        """Start a Kubernetes pod for the benchmark task."""
        await self._ensure_config()
        self._ensure_k8s_client()
        await self._ensure_docker_pull_secret()

        image_uri = self.task_env_config.docker_image
        dockerfile_commands: list[str] = []
        if not image_uri:
            # Try build-and-cache when ECR cache is enabled
            if self._aws_config.ecr_cache:
                image_uri = await self._get_or_build_image()
            # Fall back to exec-based Dockerfile setup
            if not image_uri:
                image_uri, dockerfile_commands = self._parse_dockerfile()
        if not image_uri:
            raise RuntimeError(
                "No docker_image specified and no Dockerfile found. "
                "harbor-aws only supports prebuilt images."
            )

        if self._aws_config.ecr_cache:
            image_uri = self._ecr_image_uri(image_uri)
        self.logger.debug("Using image: %s", image_uri)

        profile = _match_resource_profile(image_uri)
        pod_cpus = self._cpus_override or (profile[0] if profile else None) or self.task_env_config.cpus
        pod_memory = self._memory_mb_override or (profile[1] if profile else None) or self.task_env_config.memory_mb

        # Limit concurrent image pulls (ECR: 500, Docker Hub: 50).
        async with self._get_pull_semaphore(self._aws_config.ecr_cache):
            self.logger.debug("[start] creating pod for %s", self.environment_name)
            self._pod_name = await pods.create_pod(
                self._k8s_api,
                self._aws_config,
                image_uri,
                self.environment_name,
                self.session_id,
                cpus=pod_cpus,
                memory_mb=pod_memory,
                image_pull_secret=AWSEnvironment._docker_secret_name,
            )
            self.logger.debug("[start] pod created: %s", self._pod_name)

            await pods.wait_for_image_pulled(
                self._k8s_api,
                self._aws_config,
                self._pod_name,
            )

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

        # Throttle setup exec calls to avoid overwhelming the API server.
        async with self._get_setup_semaphore():
            for i, cmd in enumerate(dockerfile_commands):
                self.logger.debug("[start] Dockerfile cmd %d/%d: %s", i + 1, len(dockerfile_commands), cmd[:80])
                try:
                    result = await self.exec(cmd, timeout_sec=300)
                except Exception as e:
                    self.logger.error("[start] Dockerfile cmd %d FAILED for %s: %s: %s", i + 1, self._pod_name, type(e).__name__, str(e)[:200])
                    raise
                if result.return_code != 0:
                    self.logger.warning("Dockerfile setup command failed (rc=%d): %s", result.return_code, cmd[:100])

            self.logger.debug("[start] creating log dirs in pod %s", self._pod_name)
            mkdir_result = await self.exec(f"mkdir -p {EnvironmentPaths.agent_dir} {EnvironmentPaths.verifier_dir}")
            if mkdir_result.return_code != 0:
                raise RuntimeError(f"Failed to create log directories: {mkdir_result.stderr}")

        self.logger.debug("[start] pod %s fully ready", self._pod_name)

    async def stop(self, delete: bool) -> None:
        """Delete the pod."""
        try:
            if self._pod_name and self._k8s_api:
                await pods.delete_pod(self._k8s_api, self._aws_config, self._pod_name)
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
        user: str | int | None = None,
    ) -> ExecResult:
        """Execute a command in the pod via Kubernetes exec."""
        if not self._pod_name:
            raise RuntimeError("Pod not running. Call start() first.")

        if user is not None:
            command = f"su - {user} -c {command!r}" if isinstance(user, str) else f"su - $(id -un {user}) -c {command!r}"

        stdout, stderr, return_code = await exec.exec_command(
            api=self._k8s_api,
            pod_name=self._pod_name,
            namespace=self._aws_config.namespace,
            command=command,
            cwd=cwd,
            env=env,
            timeout_sec=timeout_sec,
        )

        return ExecResult(stdout=stdout, stderr=stderr, return_code=return_code)

    async def upload_file(self, source_path: Path | str, target_path: str) -> None:
        await files.upload_file(self._pod_name, self._aws_config.namespace, str(source_path), target_path)

    async def upload_dir(self, source_dir: Path | str, target_dir: str) -> None:
        await files.upload_dir(self._pod_name, self._aws_config.namespace, str(source_dir), target_dir)

    async def download_file(self, source_path: str, target_path: Path | str) -> None:
        await files.download_file(self._pod_name, self._aws_config.namespace, source_path, str(target_path))

    async def download_dir(self, source_dir: str, target_dir: Path | str) -> None:
        await files.download_dir(self._pod_name, self._aws_config.namespace, source_dir, str(target_dir))
