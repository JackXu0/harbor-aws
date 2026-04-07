"""Harbor BaseEnvironment adapter for AWS EKS/Fargate."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import secrets
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

# Layer 3 mode: trial pods run a TCP server (runner.py) and the harbor-control
# in-cluster gateway dials in. Mac talks to control server via HTTP. The
# kubectl-exec WebSocket path is bypassed entirely.
_LAYER3_ENABLED = os.environ.get("HARBOR_LAYER3", "").lower() in ("1", "true", "yes")
_LAYER3_CONTROL_URL = os.environ.get("HARBOR_CONTROL_URL", "http://localhost:8443")
_LAYER3_ADMIN_TOKEN = os.environ.get("HARBOR_ADMIN_TOKEN", "")
_LAYER3_RUNNER_CONFIGMAP = os.environ.get("HARBOR_RUNNER_CONFIGMAP", "harbor-runner")
_LAYER3_RUNNER_PORT = int(os.environ.get("HARBOR_RUNNER_PORT", "8765"))

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
    _setup_semaphore: asyncio.Semaphore | None = None
    _build_locks: dict[str, asyncio.Lock] = {}
    # Layer 3: ONE shared aiohttp session across all RemoteShells in this process,
    # so we have a single bounded connection pool to the harbor-control NLB instead
    # of 2500 separate per-trial pools (which exhaust local FDs / NLB capacity).
    _shared_aiohttp_session: object | None = None  # aiohttp.ClientSession when set
    _shared_aiohttp_lock: asyncio.Lock | None = None

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
        skip_image_check: bool = False,
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
        self._skip_image_check = skip_image_check if isinstance(skip_image_check, bool) else str(skip_image_check).lower() == "true"

        self._k8s_api: client.CoreV1Api | None = None
        self._pod_name: str | None = None
        self._shell = None  # PersistentShell instance

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

    _ECR_SEMAPHORE_LIMIT = 50
    _CREATE_SEMAPHORE_LIMIT = 100
    _CONNECT_SEMAPHORE_LIMIT = 100
    _ecr_semaphore: asyncio.Semaphore | None = None
    _create_semaphore: asyncio.Semaphore | None = None
    _connect_semaphore: asyncio.Semaphore | None = None

    @classmethod
    def _get_setup_semaphore(cls) -> asyncio.Semaphore:
        if cls._setup_semaphore is None:
            cls._setup_semaphore = asyncio.Semaphore(500)
        return cls._setup_semaphore

    @classmethod
    def _get_ecr_semaphore(cls) -> asyncio.Semaphore:
        if cls._ecr_semaphore is None:
            cls._ecr_semaphore = asyncio.Semaphore(cls._ECR_SEMAPHORE_LIMIT)
        return cls._ecr_semaphore

    @classmethod
    def _get_create_semaphore(cls) -> asyncio.Semaphore:
        if cls._create_semaphore is None:
            cls._create_semaphore = asyncio.Semaphore(cls._CREATE_SEMAPHORE_LIMIT)
        return cls._create_semaphore

    @classmethod
    def _get_connect_semaphore(cls) -> asyncio.Semaphore:
        if cls._connect_semaphore is None:
            cls._connect_semaphore = asyncio.Semaphore(cls._CONNECT_SEMAPHORE_LIMIT)
        return cls._connect_semaphore

    @classmethod
    async def _get_shared_aiohttp_session(cls):  # noqa: ANN206 — aiohttp imported lazily
        """Lazily create one process-wide aiohttp ClientSession for talking to the
        harbor-control server. All RemoteShells share it so we have one bounded
        connection pool instead of 2500 separate per-trial pools.
        """
        import aiohttp
        if cls._shared_aiohttp_lock is None:
            cls._shared_aiohttp_lock = asyncio.Lock()
        async with cls._shared_aiohttp_lock:
            if cls._shared_aiohttp_session is None:
                # Generous-but-bounded pool. limit caps total simultaneous
                # connections; limit_per_host caps connections to a single host
                # (the NLB DNS, which is what we're talking to).
                connector = aiohttp.TCPConnector(limit=512, limit_per_host=512)
                cls._shared_aiohttp_session = aiohttp.ClientSession(connector=connector)
        return cls._shared_aiohttp_session

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

        # Skip ECR check if images are known to exist (e.g., pre-cached run).
        if self._skip_image_check:
            self.logger.debug("Using image (skip check) %s", ecr_uri)
            return ecr_uri

        # Per-tag lock to avoid duplicate builds
        if tag not in AWSEnvironment._build_locks:
            AWSEnvironment._build_locks[tag] = asyncio.Lock()

        async with AWSEnvironment._build_locks[tag]:
            async with self._get_ecr_semaphore():
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
        import time as _time
        _t0 = _time.monotonic()

        await self._ensure_config()
        self._ensure_k8s_client()
        await self._ensure_docker_pull_secret()
        _t_init = _time.monotonic()

        image_uri = self.task_env_config.docker_image
        dockerfile_commands: list[str] = []
        if not image_uri:
            # Try to use a pre-built image from ECR cache
            image_uri = await self._get_or_build_image()
            # Fall back to exec-based Dockerfile setup (no Docker available)
            if not image_uri:
                image_uri, dockerfile_commands = self._parse_dockerfile()
        if not image_uri:
            raise RuntimeError(
                "No docker_image specified and no Dockerfile found. "
                "harbor-aws only supports prebuilt images."
            )

        if self._aws_config.ecr_cache:
            image_uri = self._ecr_image_uri(image_uri)
        _t_image = _time.monotonic()

        profile = _match_resource_profile(image_uri)
        pod_cpus = self._cpus_override or (profile[0] if profile else None) or self.task_env_config.cpus
        pod_memory = self._memory_mb_override or (profile[1] if profile else None) or self.task_env_config.memory_mb

        # Per-trial token for Layer 3 (no-op in Layer 2 path)
        trial_token = secrets.token_urlsafe(16) if _LAYER3_ENABLED else ""

        # Throttle pod creation to avoid K8s API contention.
        async with self._get_create_semaphore():
            _t_pull_enter = _time.monotonic()
            if _LAYER3_ENABLED:
                self._pod_name = await pods.create_layer3_pod(
                    self._k8s_api,
                    self._aws_config,
                    image_uri,
                    self.environment_name,
                    self.session_id,
                    cpus=pod_cpus,
                    memory_mb=pod_memory,
                    trial_token=trial_token,
                    runner_configmap=_LAYER3_RUNNER_CONFIGMAP,
                    runner_port=_LAYER3_RUNNER_PORT,
                    image_pull_secret=AWSEnvironment._docker_secret_name,
                )
            else:
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
        _t_created = _time.monotonic()

        # Wait for Fargate to pull image — no semaphore, all concurrent.
        await pods.wait_for_image_pulled(
            self._k8s_api,
            self._aws_config,
            self._pod_name,
        )
        _t_pulled = _time.monotonic()

        try:
            await pods.wait_for_pod_running(
                self._k8s_api,
                self._aws_config,
                self._pod_name,
            )
        except Exception as e:
            self.logger.error("[start] wait_for_pod_running FAILED for %s: %s: %s", self._pod_name, type(e).__name__, str(e)[:200])
            raise
        _t_running = _time.monotonic()

        if _LAYER3_ENABLED:
            # Layer 3: read pod IP, then ask the in-cluster control server to
            # dial the runner over direct in-VPC TCP. The apiserver is NOT in
            # the data path. We share ONE aiohttp ClientSession across every
            # RemoteShell so we have a single bounded connection pool to the NLB
            # instead of 2500 separate per-trial pools.
            from harbor_aws.core.remote_shell import RemoteShell
            pod_ip = await pods.get_pod_ip(self._k8s_api, self._aws_config.namespace, self._pod_name)
            shared_session = await AWSEnvironment._get_shared_aiohttp_session()
            self._shell = RemoteShell(
                trial_id=self._pod_name,
                pod_ip=pod_ip,
                pod_port=_LAYER3_RUNNER_PORT,
                token=trial_token,
                control_url=_LAYER3_CONTROL_URL,
                admin_token=_LAYER3_ADMIN_TOKEN,
                session=shared_session,
            )
            await self._shell.connect()
        else:
            # Layer 2 (v4): persistent kubectl-exec WebSocket.
            from harbor_aws.core.shell import PersistentShell
            self._shell = PersistentShell(self._pod_name, self._aws_config.namespace)
            async with self._get_connect_semaphore():
                await self._shell.connect()
        _t_shell = _time.monotonic()

        self.logger.info(
            "[timing] %s init=%.1f image=%.1f pull_wait=%.1f create=%.1f pulled=%.1f running=%.1f shell=%.1f",
            self.environment_name,
            _t_init - _t0, _t_image - _t_init, _t_pull_enter - _t_image,
            _t_created - _t_pull_enter, _t_pulled - _t_created, _t_running - _t_pulled,
            _t_shell - _t_running,
        )

        # Setup — all commands go through the persistent shell, no new WebSocket connections.
        _t_setup_wait = _time.monotonic()
        async with self._get_setup_semaphore():
            _t_setup_enter = _time.monotonic()
            for i, cmd in enumerate(dockerfile_commands):
                self.logger.debug("[start] Dockerfile cmd %d/%d: %s", i + 1, len(dockerfile_commands), cmd[:80])
                try:
                    result = await self.exec(cmd, timeout_sec=300)
                except Exception as e:
                    self.logger.error("[start] Dockerfile cmd %d FAILED for %s: %s: %s", i + 1, self._pod_name, type(e).__name__, str(e)[:200])
                    raise
                if result.return_code != 0:
                    self.logger.warning("Dockerfile setup command failed (rc=%d): %s", result.return_code, cmd[:100])

            mkdir_result = await self.exec(f"mkdir -p {EnvironmentPaths.agent_dir} {EnvironmentPaths.verifier_dir} {EnvironmentPaths.artifacts_dir}")
            if mkdir_result.return_code != 0:
                raise RuntimeError(f"Failed to create log directories: {mkdir_result.stderr}")
        _t_done = _time.monotonic()

        self.logger.info(
            "[timing] %s setup_wait=%.1f setup_exec=%.1f TOTAL=%.1f",
            self.environment_name,
            _t_setup_enter - _t_setup_wait, _t_done - _t_setup_enter, _t_done - _t0,
        )

    async def stop(self, delete: bool) -> None:
        """Close shell and delete the pod."""
        import time as _time
        _t0 = _time.monotonic()
        try:
            if self._shell:
                await self._shell.close()
                self._shell = None
            if self._pod_name and self._k8s_api:
                await pods.delete_pod(self._k8s_api, self._aws_config, self._pod_name)
        except Exception as e:
            self.logger.warning("Error deleting pod: %s", e)
        finally:
            self.logger.info("[timing] %s stop=%.1f", self.environment_name, _time.monotonic() - _t0)
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
        """Execute a command in the pod via persistent shell."""
        if not self._pod_name:
            raise RuntimeError("Pod not running. Call start() first.")

        if user is not None:
            command = f"su - {user} -c {command!r}" if isinstance(user, str) else f"su - $(id -un {user}) -c {command!r}"

        if self._shell:
            stdout, stderr, return_code = await self._shell.run(
                command, cwd=cwd, env=env, timeout_sec=timeout_sec or 300,
            )
        else:
            # Fallback to per-call WebSocket if shell not available
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
        if self._shell:
            import base64, io, tarfile
            target = Path(target_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            src = Path(source_path)
            stdout, _, rc = await self._shell.run(
                f"tar czf - -C {src.parent} {src.name} | base64", timeout_sec=120,
            )
            if rc != 0 or not stdout.strip():
                raise RuntimeError(f"download_file failed (rc={rc})")
            data = base64.b64decode(stdout)
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tar:
                target.write_bytes(tar.extractfile(tar.getmembers()[0]).read())
        else:
            await files.download_file(self._pod_name, self._aws_config.namespace, source_path, str(target_path))

    async def download_dir(self, source_dir: str, target_dir: Path | str) -> None:
        if self._shell:
            import base64, io, tarfile
            target = Path(target_dir)
            target.mkdir(parents=True, exist_ok=True)
            stdout, _, rc = await self._shell.run(
                f"tar czf - -C {source_dir} . | base64", timeout_sec=120,
            )
            if rc != 0 or not stdout.strip():
                raise RuntimeError(f"download_dir failed (rc={rc})")
            data = base64.b64decode(stdout)
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tar:
                tar.extractall(path=str(target), filter="data")
        else:
            await files.download_dir(self._pod_name, self._aws_config.namespace, source_dir, str(target_dir))
