"""Harbor BaseEnvironment adapter for AWS EKS/Fargate.

Layer 3 architecture:
  - kubectl create_pod / delete_pod via the K8s API server (control plane only).
  - Pod runs ``harbor_aws/runner.sh`` as PID 1 (mounted via ConfigMap), which
    dials out to the in-cluster harbor-control server over in-VPC TCP.
  - The Mac talks to harbor-control over HTTPS via an NLB, using one
    process-wide aiohttp ClientSession (HTTP keepalive across all trials).
  - All exec / file transfer flows Mac → NLB → control server → trial pod.
    The K8s apiserver is **not** in the data path, and ``kubectl exec`` is
    never used.
"""

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

from harbor_aws.core import pods
from harbor_aws.core.config import AWSConfig, create_k8s_client, load_config_from_stack
from harbor_aws.core.remote_shell import RemoteShell

# Layer 3 configuration. Auto-discovered from the CloudFormation stack
# and K8s API unless explicitly overridden via environment variables.
_RUNNER_CONFIGMAP = os.environ.get("HARBOR_RUNNER_CONFIGMAP", "harbor-runner")
_CONTROL_RUNNER_HOST = os.environ.get("HARBOR_CONTROL_RUNNER_HOST", "harbor-control.harbor.svc.cluster.local")
_CONTROL_RUNNER_PORT = int(os.environ.get("HARBOR_CONTROL_RUNNER_PORT", "8444"))

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

    Each sandbox is a Kubernetes pod on EKS Fargate. The pod's PID 1 is a
    bash runner that dials the harbor-control server (in-VPC); all command
    execution and file transfer flow through that connection. See module
    docstring for the full data path.
    """

    # -- Class-level shared state (avoid repeated API calls across instances) --
    _cached_stack_config: AWSConfig | None = None
    _config_lock: asyncio.Lock | None = None
    _shared_k8s_api = None
    _docker_secret_checked = False
    _docker_secret_name: str | None = None
    _build_locks: dict[str, asyncio.Lock] = {}
    # One process-wide aiohttp session shared across all RemoteShells, so we
    # use a single bounded connection pool to the NLB instead of one pool
    # per trial (which would exhaust local FDs and NLB capacity at scale).
    _shared_aiohttp_session: object | None = None  # aiohttp.ClientSession when set
    _shared_aiohttp_lock: asyncio.Lock | None = None
    _control_url: str | None = None
    _admin_token: str | None = None

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
        self._shell: RemoteShell | None = None

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

    async def _ensure_control_plane(self) -> None:
        """Resolve the harbor-control NLB URL and admin token.

        Priority: env var override > stack output / K8s Service lookup.
        """
        cls = AWSEnvironment
        if cls._control_url is not None:
            return

        # Env var overrides (backwards compat / manual setups)
        env_url = os.environ.get("HARBOR_CONTROL_URL")
        env_token = os.environ.get("HARBOR_ADMIN_TOKEN")
        if env_url and env_token:
            cls._control_url = env_url
            cls._admin_token = env_token
            return

        # Admin token from stack output
        cls._admin_token = env_token or self._aws_config.admin_token or ""

        # NLB hostname from K8s Service
        if env_url:
            cls._control_url = env_url
        else:
            nlb_host = await asyncio.to_thread(
                self._read_nlb_hostname, self._k8s_api, self._aws_config.namespace,
            )
            cls._control_url = f"http://{nlb_host}:8443"
            self.logger.info("Auto-discovered control URL: %s", cls._control_url)

    @staticmethod
    def _read_nlb_hostname(api: client.CoreV1Api, namespace: str) -> str:
        svc = api.read_namespaced_service("harbor-control-nlb", namespace)
        ingress = svc.status.load_balancer.ingress
        if ingress and ingress[0].hostname:
            return ingress[0].hostname
        raise RuntimeError(
            "harbor-control-nlb Service has no external hostname. "
            "Is the AWS Load Balancer Controller running?"
        )

    _ECR_SEMAPHORE_LIMIT = 50
    _CREATE_SEMAPHORE_LIMIT = 100
    _ecr_semaphore: asyncio.Semaphore | None = None
    _create_semaphore: asyncio.Semaphore | None = None

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
    async def _get_shared_aiohttp_session(cls):  # noqa: ANN206 — aiohttp imported lazily
        """Lazily create one process-wide aiohttp ClientSession.

        All RemoteShells share this session so we use a single connection
        pool to the harbor-control NLB instead of one pool per trial. The
        pool is **unbounded** (limit=0) so workloads with thousands of
        concurrent trials don't queue waiting for a free slot. The
        underlying TCP cost scales with concurrent in-flight requests, not
        with peak trial count.
        """
        import aiohttp
        if cls._shared_aiohttp_lock is None:
            cls._shared_aiohttp_lock = asyncio.Lock()
        async with cls._shared_aiohttp_lock:
            if cls._shared_aiohttp_session is None:
                connector = aiohttp.TCPConnector(limit=0, limit_per_host=0)
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

        # Any `host.tld/...` prefix means the image already has an explicit
        # registry — don't rewrite it. (This covers ECR, GHCR, etc. as well.)
        if re.match(r"^[\w.-]+\.\w{2,}/", stripped):
            return image

        if "/" not in stripped.split(":")[0]:
            stripped = f"library/{stripped}"

        account = self._aws_config.account_id
        region = self._aws_config.region

        if not account:
            self.logger.debug("No account_id, skipping ECR rewrite for %s", image)
            return image

        return f"{account}.dkr.ecr.{region}.amazonaws.com/docker-hub/{stripped}"

    # -- Image resolution --------------------------------------------------
    #
    # Three tiers, tried in order:
    #   Tier 1 — task.toml `docker_image`: use the image directly, no setup.
    #   Tier 2 — simple Dockerfile: pull FROM image, replay RUN/WORKDIR/ENV
    #            inside the pod as setup. Avoids `docker build` entirely.
    #   Tier 3 — complex Dockerfile: real `docker build` + push to ECR.

    _ECR_REPO = "harbor-build"

    async def _resolve_image(self) -> tuple[str, list[str]]:
        """Return ``(image_uri, in_pod_setup_cmds)`` for this trial."""
        # Tier 1: explicit image
        if self.task_env_config.docker_image:
            self.logger.debug("[image] tier 1 (task.toml): %s", self.task_env_config.docker_image)
            return self.task_env_config.docker_image, []

        if not (self.environment_dir / "Dockerfile").exists():
            raise RuntimeError(
                "No docker_image in task.toml and no Dockerfile found. "
                "harbor-aws can't determine which image to run."
            )

        # Tier 2: simple Dockerfile we can replay in-pod
        if (parsed := self._parse_simple_dockerfile()) is not None:
            base_image, setup_cmds = parsed
            self.logger.debug("[image] tier 2 (pull): %s + %d setup cmds", base_image, len(setup_cmds))
            return base_image, setup_cmds

        # Tier 3: real build
        self.logger.info("[image] tier 3 (build): falling back to docker build")
        return await self._build_image_via_docker(), []

    def _parse_simple_dockerfile(self) -> tuple[str, list[str]] | None:
        """Return ``(base_image, setup_commands)`` if the Dockerfile can be
        replayed inside a running pod, or ``None`` if it needs ``docker build``.

        Replay-compatible means: a single ``FROM`` plus only ``RUN`` /
        ``WORKDIR`` / ``ENV`` / ``LABEL`` / ``MAINTAINER`` instructions.
        Anything else (``COPY``, ``ADD``, multi-stage, BuildKit ``RUN --flag``,
        ``ENTRYPOINT``, ``CMD``, ``USER``, ...) bails out to tier 3.
        """
        image: str | None = None
        commands: list[str] = []
        from_count = 0

        for line in self._dockerfile_logical_lines():
            instr, _, rest = line.partition(" ")
            instr = instr.upper()
            rest = rest.strip()

            if instr == "FROM":
                from_count += 1
                if from_count > 1:
                    return None  # multi-stage
                image = rest.split()[0]
            elif instr == "RUN":
                if rest.startswith("--"):
                    return None  # BuildKit flag — can't replay in shell
                commands.append(rest)
            elif instr == "WORKDIR":
                commands.append(f"mkdir -p {rest} && cd {rest}")
            elif instr == "ENV":
                # `ENV K=V` and `ENV K V` both supported
                if "=" in rest.split(maxsplit=1)[0]:
                    commands.append(f"export {rest}")
                else:
                    parts = rest.split(maxsplit=1)
                    if len(parts) == 2:
                        commands.append(f"export {parts[0]}={parts[1]}")
            elif instr in {"LABEL", "MAINTAINER"}:
                continue  # no runtime effect
            else:
                return None  # COPY, ADD, ENTRYPOINT, CMD, USER, ARG, VOLUME, ...

        return (image, commands) if image else None

    def _dockerfile_logical_lines(self) -> list[str]:
        """Read the Dockerfile and yield logical lines (joining ``\\``-continuations)."""
        raw_lines = (self.environment_dir / "Dockerfile").read_text().splitlines()
        logical: list[str] = []
        current = ""
        for raw in raw_lines:
            if raw.rstrip().endswith("\\"):
                current += raw.rstrip()[:-1]
            else:
                current += raw
                stripped = current.strip()
                if stripped and not stripped.startswith("#"):
                    logical.append(stripped)
                current = ""
        if current.strip() and not current.strip().startswith("#"):
            logical.append(current.strip())
        return logical

    async def _build_image_via_docker(self) -> str:
        """Build the environment Dockerfile locally and push it to ECR.

        Used only as a last resort when the Dockerfile has instructions that
        can't be replayed inside the pod after start (COPY, ADD, multi-stage,
        ENTRYPOINT, etc.).
        """
        dockerfile = self.environment_dir / "Dockerfile"
        tag = hashlib.sha256(dockerfile.read_bytes()).hexdigest()[:16]
        account = self._aws_config.account_id
        region = self._aws_config.region
        ecr_uri = f"{account}.dkr.ecr.{region}.amazonaws.com/{self._ECR_REPO}:{tag}"

        # Per-tag lock to avoid duplicate builds when many trials share a Dockerfile
        if tag not in AWSEnvironment._build_locks:
            AWSEnvironment._build_locks[tag] = asyncio.Lock()

        async with AWSEnvironment._build_locks[tag]:
            async with self._get_ecr_semaphore():
                if await asyncio.to_thread(self._ecr_image_exists, tag):
                    self.logger.debug("Using cached build %s", ecr_uri)
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
        await self._ensure_control_plane()
        await self._ensure_docker_pull_secret()
        _t_init = _time.monotonic()

        image_uri, dockerfile_commands = await self._resolve_image()
        if self._aws_config.ecr_cache:
            image_uri = self._ecr_image_uri(image_uri)
        _t_image = _time.monotonic()

        profile = _match_resource_profile(image_uri)
        pod_cpus = self._cpus_override or (profile[0] if profile else None) or self.task_env_config.cpus
        pod_memory = self._memory_mb_override or (profile[1] if profile else None) or self.task_env_config.memory_mb

        # Per-trial token used by the runner to authenticate to the control server.
        trial_token = secrets.token_urlsafe(16)

        shared_session = await AWSEnvironment._get_shared_aiohttp_session()
        self._shell = RemoteShell(
            trial_id=self.session_id,
            token=trial_token,
            control_url=AWSEnvironment._control_url,
            admin_token=AWSEnvironment._admin_token,
            session=shared_session,
        )

        # Pre-register the trial with the control server BEFORE creating the
        # pod, so the runner has somewhere to connect to as soon as it starts.
        # /register blocks until the runner dials in — we run it concurrently
        # with pod creation and only await it after the pod is Running.
        register_task = asyncio.create_task(self._shell.connect())

        try:
            # Throttle pod creation to avoid K8s API contention.
            async with self._get_create_semaphore():
                _t_pull_enter = _time.monotonic()
                self._pod_name = await pods.create_pod(
                    self._k8s_api,
                    self._aws_config,
                    image_uri,
                    self.environment_name,
                    self.session_id,
                    cpus=pod_cpus,
                    memory_mb=pod_memory,
                    trial_id=self.session_id,
                    trial_token=trial_token,
                    runner_configmap=_RUNNER_CONFIGMAP,
                    control_host=_CONTROL_RUNNER_HOST,
                    control_runner_port=_CONTROL_RUNNER_PORT,
                    image_pull_secret=AWSEnvironment._docker_secret_name,
                )
            _t_created = _time.monotonic()

            # Wait until the pod's main container is Running. This surfaces
            # image-pull errors quickly instead of letting /register hit its
            # full timeout.
            await pods.wait_for_pod_running(self._aws_config, self._pod_name)
            _t_running = _time.monotonic()

            # Now block until the runner has actually dialed in and the
            # control server has registered the connection.
            await register_task
            _t_shell = _time.monotonic()
        except Exception:
            register_task.cancel()
            raise

        # Run any inline Dockerfile setup commands plus mkdir for the harbor log dirs.
        for i, cmd in enumerate(dockerfile_commands):
            self.logger.debug("[start] Dockerfile cmd %d/%d: %s", i + 1, len(dockerfile_commands), cmd[:80])
            try:
                result = await self.exec(cmd, timeout_sec=300)
            except Exception as e:
                self.logger.error("[start] Dockerfile cmd %d FAILED for %s: %s: %s", i + 1, self._pod_name, type(e).__name__, str(e)[:200])
                raise
            if result.return_code != 0:
                self.logger.warning("Dockerfile setup command failed (rc=%d): %s", result.return_code, cmd[:100])

        mkdir_result = await self.exec(
            f"mkdir -p {EnvironmentPaths.agent_dir} {EnvironmentPaths.verifier_dir}"
        )
        if mkdir_result.return_code != 0:
            raise RuntimeError(f"Failed to create log directories: {mkdir_result.stderr}")
        _t_done = _time.monotonic()

        self.logger.info(
            "[timing] %s init=%.1f image=%.1f create=%.1f running=%.1f shell=%.1f setup=%.1f TOTAL=%.1f",
            self.environment_name,
            _t_init - _t0, _t_image - _t_init,
            _t_created - _t_pull_enter, _t_running - _t_created,
            _t_shell - _t_running, _t_done - _t_shell, _t_done - _t0,
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
        """Execute a command in the pod via the harbor-control gateway.

        ``user`` is part of the ``BaseEnvironment`` interface but not
        supported here — trial pods run as root, which is equivalent to any
        user for our sandbox purposes. Raise if a caller actually asks for a
        non-root user so we fail loudly instead of silently ignoring it.
        """
        if self._shell is None:
            raise RuntimeError("Pod not running. Call start() first.")
        if user is not None:
            raise NotImplementedError("AWSEnvironment.exec(user=...) is not supported")
        stdout, stderr, return_code = await self._shell.run(
            command, cwd=cwd, env=env, timeout_sec=timeout_sec or 900,
        )
        return ExecResult(stdout=stdout, stderr=stderr, return_code=return_code)

    async def upload_file(self, source_path: Path | str, target_path: str) -> None:
        if self._shell is None:
            raise RuntimeError("Pod not running. Call start() first.")
        await self._shell.upload_file(source_path, target_path)

    async def upload_dir(self, source_dir: Path | str, target_dir: str) -> None:
        if self._shell is None:
            raise RuntimeError("Pod not running. Call start() first.")
        await self._shell.upload_dir(source_dir, target_dir)

    async def download_file(self, source_path: str, target_path: Path | str) -> None:
        if self._shell is None:
            raise RuntimeError("Pod not running. Call start() first.")
        await self._shell.download_file(source_path, target_path)

    async def download_dir(self, source_dir: str, target_dir: Path | str) -> None:
        if self._shell is None:
            raise RuntimeError("Pod not running. Call start() first.")
        await self._shell.download_dir(source_dir, target_dir)
