"""Image resolution (3 tiers) and tier-3 docker build + ECR push (orchestrator-side).

  Tier 1 — If docker image pre-built, pull from Docker Hub or ECR.
  Tier 2 — Simple Dockerfile, pull base image, replay RUN/WORKDIR/ENV in-pod.
  Tier 3 — Complex Dockerfile, build locally and cache in ECR.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
import subprocess
from pathlib import Path

import boto3
from harbor.models.task.config import EnvironmentConfig

from harbor_aws.models import ClusterInfo

_ECR_REPO = "harbor-build"
_ECR_SEMAPHORE_LIMIT = 50

_build_locks: dict[str, asyncio.Lock] = {}
_shared_ecr_client = None  # boto3 ECR client (lazy)
_ecr_semaphore: asyncio.Semaphore | None = None
_shared_boto3_session: boto3.Session | None = None


def _get_ecr_semaphore() -> asyncio.Semaphore:
    global _ecr_semaphore
    if _ecr_semaphore is None:
        _ecr_semaphore = asyncio.Semaphore(_ECR_SEMAPHORE_LIMIT)
    return _ecr_semaphore


def _boto3_session(region: str, role_arn: str | None) -> boto3.Session:
    global _shared_boto3_session
    if _shared_boto3_session is not None:
        return _shared_boto3_session

    if not role_arn:
        _shared_boto3_session = boto3.Session(region_name=region)
        return _shared_boto3_session

    sts = boto3.client("sts", region_name=region)
    creds = sts.assume_role(RoleArn=role_arn, RoleSessionName="harbor-aws")["Credentials"]
    _shared_boto3_session = boto3.Session(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
        region_name=region,
    )
    return _shared_boto3_session


def _get_ecr_client(region: str, role_arn: str | None):  # noqa: ANN202 — boto3 client type
    global _shared_ecr_client
    if _shared_ecr_client is None:
        _shared_ecr_client = _boto3_session(region, role_arn).client("ecr", region_name=region)
    return _shared_ecr_client


# ===== ECR pull-through cache rewrite =====


def _ecr_image_uri(image: str, region: str, info: ClusterInfo, logger: logging.Logger) -> str:
    """Rewrite a Docker Hub image to use the ECR pull-through cache.

    ``alexgshaw/foo:tag`` → ``<account>.dkr.ecr.<region>.amazonaws.com/docker-hub/alexgshaw/foo:tag``

    Non-Docker-Hub images returned unchanged.
    """
    stripped = re.sub(r"^(docker\.io|registry-1\.docker\.io)/", "", image)

    # Any host.tld/... prefix means image already has an explicit registry — don't rewrite.
    if re.match(r"^[\w.-]+\.\w{2,}/", stripped):
        return image

    if "/" not in stripped.split(":")[0]:
        stripped = f"library/{stripped}"

    if not info.account_id:
        logger.debug("No account_id, skipping ECR rewrite for %s", image)
        return image

    return f"{info.account_id}.dkr.ecr.{region}.amazonaws.com/docker-hub/{stripped}"


# ===== Image resolution (three tiers) =====


async def resolve_image(
    environment_dir: Path,
    task_env_config: EnvironmentConfig,
    region: str,
    role_arn: str | None,
    info: ClusterInfo,
    logger: logging.Logger,
) -> tuple[str, list[str]]:

    image_uri, setup_cmds = await _resolve_image_uri(
        environment_dir, task_env_config, region, role_arn, info, logger,
    )

    if info.dockerhub_cache_enabled:
        image_uri = _ecr_image_uri(image_uri, region, info, logger)
    return image_uri, setup_cmds


async def _resolve_image_uri(
    environment_dir: Path,
    task_env_config: EnvironmentConfig,
    region: str,
    role_arn: str | None,
    info: ClusterInfo,
    logger: logging.Logger,
) -> tuple[str, list[str]]:
    # Tier 1: explicit image
    if task_env_config.docker_image:
        logger.debug("[image] tier 1 (task.toml): %s", task_env_config.docker_image)
        return task_env_config.docker_image, []

    if not (environment_dir / "Dockerfile").exists():
        raise RuntimeError(
            "No docker_image in task.toml and no Dockerfile found. "
            "harbor-aws can't determine which image to run."
        )

    # Tier 2: simple Dockerfile we can replay in-pod
    if (parsed := _parse_simple_dockerfile(environment_dir)) is not None:
        base_image, setup_cmds = parsed
        logger.debug("[image] tier 2 (pull): %s + %d setup cmds", base_image, len(setup_cmds))
        return base_image, setup_cmds

    # Tier 3: real build
    logger.info("[image] tier 3 (build): falling back to docker build")
    return await _build_image_via_docker(environment_dir, region, role_arn, info, logger), []


def _parse_simple_dockerfile(environment_dir: Path) -> tuple[str, list[str]] | None:
    """Return ``(base_image, setup_commands)`` if the Dockerfile can be replayed
    inside a running pod, or ``None`` if it needs ``docker build``.

    Replay-compatible: single ``FROM`` + only ``RUN`` / ``WORKDIR`` / ``ENV`` /
    ``LABEL`` / ``MAINTAINER``. Anything else (``COPY``, ``ADD``, multi-stage,
    BuildKit ``RUN --flag``, ``ENTRYPOINT``, ``CMD``, ``USER``, ...) → tier 3.
    """
    image: str | None = None
    commands: list[str] = []
    from_count = 0

    for line in _dockerfile_logical_lines(environment_dir):
        instr, _, rest = line.partition(" ")
        instr = instr.upper()
        rest = rest.strip()

        if instr == "FROM":
            from_count += 1
            if from_count > 1:
                return None
            image = rest.split()[0]
        elif instr == "RUN":
            if rest.startswith("--"):
                return None  # BuildKit flag — can't replay in shell
            commands.append(rest)
        elif instr == "WORKDIR":
            commands.append(f"mkdir -p {rest} && cd {rest}")
        elif instr == "ENV":
            if "=" in rest.split(maxsplit=1)[0]:
                commands.append(f"export {rest}")
            else:
                parts = rest.split(maxsplit=1)
                if len(parts) == 2:
                    commands.append(f"export {parts[0]}={parts[1]}")
        elif instr in {"LABEL", "MAINTAINER"}:
            continue
        else:
            return None  # COPY, ADD, ENTRYPOINT, CMD, USER, ARG, VOLUME, ...

    return (image, commands) if image else None


def _dockerfile_logical_lines(environment_dir: Path) -> list[str]:
    """Read the Dockerfile and yield logical lines (joining ``\\`` continuations)."""
    raw_lines = (environment_dir / "Dockerfile").read_text().splitlines()
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


async def _build_image_via_docker(
    environment_dir: Path,
    region: str,
    role_arn: str | None,
    info: ClusterInfo,
    logger: logging.Logger,
) -> str:
    """Build the environment Dockerfile locally and push to ECR.

    Last resort when the Dockerfile has instructions that can't be replayed in
    the pod after start (COPY, ADD, multi-stage, ENTRYPOINT, etc.).
    """
    dockerfile = environment_dir / "Dockerfile"
    tag = hashlib.sha256(dockerfile.read_bytes()).hexdigest()[:16]
    ecr_uri = f"{info.account_id}.dkr.ecr.{region}.amazonaws.com/{_ECR_REPO}:{tag}"

    if tag not in _build_locks:
        _build_locks[tag] = asyncio.Lock()

    async with _build_locks[tag]:
        if await asyncio.to_thread(_ecr_image_exists, tag, region, role_arn):
            logger.debug("Using cached build %s", ecr_uri)
            return ecr_uri

        async with _get_ecr_semaphore():
            logger.info("Building and pushing image %s", ecr_uri)
            await asyncio.to_thread(_docker_build_and_push, ecr_uri, environment_dir, region, role_arn)
            return ecr_uri


def _ecr_image_exists(tag: str, region: str, role_arn: str | None) -> bool:
    ecr = _get_ecr_client(region, role_arn)
    try:
        ecr.describe_images(repositoryName=_ECR_REPO, imageIds=[{"imageTag": tag}])
        return True
    except (ecr.exceptions.ImageNotFoundException, ecr.exceptions.RepositoryNotFoundException):
        return False


def _docker_build_and_push(ecr_uri: str, environment_dir: Path, region: str, role_arn: str | None) -> None:
    ecr = _boto3_session(region, role_arn).client("ecr", region_name=region)

    # Ensure repo exists with 90-day lifecycle policy
    try:
        ecr.describe_repositories(repositoryNames=[_ECR_REPO])
    except ecr.exceptions.RepositoryNotFoundException:
        ecr.create_repository(repositoryName=_ECR_REPO)
        ecr.put_lifecycle_policy(repositoryName=_ECR_REPO, lifecyclePolicyText=json.dumps(
            {"rules": [{"rulePriority": 1, "action": {"type": "expire"},
             "selection": {"tagStatus": "any", "countType": "sinceImagePushed",
                           "countUnit": "days", "countNumber": 90}}]},
        ))

    auth = ecr.get_authorization_token()["authorizationData"][0]
    token = base64.b64decode(auth["authorizationToken"]).decode()
    username, password = token.split(":", 1)
    subprocess.run(
        ["docker", "login", "--username", username, "--password-stdin", auth["proxyEndpoint"]],
        input=password, capture_output=True, text=True, check=True,
    )

    subprocess.run(["docker", "build", "-t", ecr_uri, str(environment_dir)], check=True, capture_output=True, text=True)
    subprocess.run(["docker", "push", ecr_uri], check=True, capture_output=True, text=True)
