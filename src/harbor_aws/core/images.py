"""Docker image building via CodeBuild and ECR management."""

from __future__ import annotations

import asyncio
import io
import logging
import tarfile
from pathlib import Path

from tenacity import retry, stop_after_attempt, wait_exponential

from harbor_aws.core.config import AWSConfig

logger = logging.getLogger(__name__)

BUILDSPEC_TEMPLATE = """version: 0.2
phases:
  pre_build:
    commands:
      - aws ecr get-login-password --region {region} | docker login --username AWS --password-stdin {ecr_registry}
  build:
    commands:
      - docker build -t {image_name}:{tag} .
      - docker tag {image_name}:{tag} {full_uri}
  post_build:
    commands:
      - docker push {full_uri}
"""


async def image_exists(ecr_client: object, repository_name: str, image_tag: str) -> bool:
    """Check if an image tag exists in ECR."""
    try:
        response = await asyncio.to_thread(
            ecr_client.describe_images,  # type: ignore[union-attr]
            repositoryName=repository_name,
            imageIds=[{"imageTag": image_tag}],
        )
        return len(response.get("imageDetails", [])) > 0
    except Exception:
        return False


def get_image_uri(config: AWSConfig, image_name: str, tag: str = "latest") -> str:
    """Construct the full ECR image URI."""
    # ecr_repository_uri is like 123456.dkr.ecr.us-east-1.amazonaws.com/harbor-aws
    return f"{config.ecr_repository_uri}:{image_name}-{tag}"


def _get_ecr_registry(config: AWSConfig) -> str:
    """Extract the ECR registry URL from the repository URI."""
    # 123456.dkr.ecr.us-east-1.amazonaws.com/harbor-aws -> 123456.dkr.ecr.us-east-1.amazonaws.com
    return config.ecr_repository_uri.rsplit("/", 1)[0]


def _get_ecr_repository_name(config: AWSConfig) -> str:
    """Extract the repository name from the URI."""
    return config.ecr_repository_uri.rsplit("/", 1)[-1]


async def _upload_build_context(
    s3_client: object,
    config: AWSConfig,
    environment_dir: Path,
    image_name: str,
) -> str:
    """Tar the environment directory and upload to S3 for CodeBuild."""
    tar_buffer = io.BytesIO()
    with tarfile.open(fileobj=tar_buffer, mode="w:gz") as tar:
        for item in environment_dir.rglob("*"):
            if item.is_file():
                arcname = str(item.relative_to(environment_dir))
                tar.add(str(item), arcname=arcname)
    tar_buffer.seek(0)

    s3_key = f"{config.s3_prefix}build-context/{image_name}.tar.gz"

    await asyncio.to_thread(
        s3_client.put_object,  # type: ignore[union-attr]
        Bucket=config.s3_bucket,
        Key=s3_key,
        Body=tar_buffer.getvalue(),
    )

    logger.debug("Uploaded build context to s3://%s/%s", config.s3_bucket, s3_key)
    return s3_key


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=5, max=60),
    reraise=True,
)
async def build_and_push_image(
    codebuild_client: object,
    s3_client: object,
    config: AWSConfig,
    environment_dir: Path,
    image_name: str,
    image_tag: str = "latest",
) -> str:
    """Build Docker image via CodeBuild and push to ECR.

    Returns the full image URI.
    """
    full_uri = get_image_uri(config, image_name, image_tag)
    ecr_registry = _get_ecr_registry(config)

    logger.info("Building image %s via CodeBuild", full_uri)

    # Upload build context to S3
    s3_key = await _upload_build_context(s3_client, config, environment_dir, image_name)

    # Generate inline buildspec
    buildspec = BUILDSPEC_TEMPLATE.format(
        region=config.region,
        ecr_registry=ecr_registry,
        image_name=image_name,
        tag=image_tag,
        full_uri=full_uri,
    )

    # Start build
    response = await asyncio.to_thread(
        codebuild_client.start_build,  # type: ignore[union-attr]
        projectName=config.codebuild_project_name,
        sourceTypeOverride="S3",
        sourceLocationOverride=f"{config.s3_bucket}/{s3_key}",
        buildspecOverride=buildspec,
        timeoutInMinutesOverride=config.build_timeout_minutes,
    )

    build_id = response["build"]["id"]
    logger.debug("CodeBuild started: %s", build_id)

    # Poll until complete
    timeout_seconds = config.build_timeout_minutes * 60
    elapsed = 0
    poll_interval = 10

    while elapsed < timeout_seconds:
        await asyncio.sleep(poll_interval)
        elapsed += poll_interval

        status_response = await asyncio.to_thread(
            codebuild_client.batch_get_builds,  # type: ignore[union-attr]
            ids=[build_id],
        )

        build = status_response["builds"][0]
        build_status = build["buildStatus"]

        if build_status == "SUCCEEDED":
            logger.info("Image built successfully: %s", full_uri)
            return full_uri
        elif build_status in ("FAILED", "FAULT", "TIMED_OUT", "STOPPED"):
            phases = build.get("phases", [])
            failed_phases = [p for p in phases if p.get("phaseStatus") == "FAILED"]
            error_details = ""
            if failed_phases:
                contexts = failed_phases[0].get("contexts", [])
                error_details = "; ".join(c.get("message", "") for c in contexts)
            raise RuntimeError(
                f"CodeBuild failed with status {build_status}. Build ID: {build_id}. Details: {error_details}"
            )

        if elapsed % 30 == 0:
            logger.debug("Build %s in progress (%ds elapsed)", build_id, elapsed)

    raise RuntimeError(f"CodeBuild timed out after {timeout_seconds}s. Build ID: {build_id}")
