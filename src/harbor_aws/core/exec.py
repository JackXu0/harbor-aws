"""Command execution and file transfer via S3 command channel.

Instead of SSM/ECS Exec, commands are submitted as JSON files to S3.
A daemon running inside the container polls for commands, executes them,
and uploads results back to S3.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import shlex
import tarfile
import uuid
from pathlib import Path

from tenacity import retry, stop_after_attempt, wait_exponential_jitter

logger = logging.getLogger(__name__)


def _build_full_command(
    command: str,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> str:
    """Build the full command string with cwd and env var prefixes."""
    parts = []

    # Pre-set variables that third-party scripts may reference (avoids `set -u` failures)
    parts.append("export CONDA_DEFAULT_ENV=${CONDA_DEFAULT_ENV:-};")

    if env:
        for key, value in env.items():
            parts.append(f"export {key}={shlex.quote(value)};")

    if cwd:
        parts.append(f"cd {shlex.quote(cwd)} &&")

    parts.append(command)
    return " ".join(parts)


def _apply_sweagent_patches(command: str) -> str:
    """Apply swe-agent specific command patches."""
    # 1. /etc/profile.d/testbed-conda.sh may not exist — source only if present
    command = command.replace(
        ". /etc/profile.d/testbed-conda.sh",
        "if [ -f /etc/profile.d/testbed-conda.sh ]; then . /etc/profile.d/testbed-conda.sh; fi",
    )
    # 1b. litellm needs boto3 for Bedrock auth — install into sweagent venv before running
    command = command.replace(
        "sweagent run ",
        "/opt/sweagent-venv/bin/pip install -q boto3 2>/dev/null; sweagent run ",
    )
    # 2. When no /testbed, swe-agent needs a preexisting git repo at $(pwd).
    command = command.replace(
        "echo '--env.repo.path=$(pwd)'",
        "echo \"--env.repo.type=preexisting --env.repo.repo_name=$(pwd)\"",
    )
    if "sweagent run" in command and "--env.repo.repo_name=" in command:
        git_setup = (
            "if ! git rev-parse --git-dir > /dev/null 2>&1; then"
            " git init -q . && git add -A && git commit -q -m init --allow-empty;"
            " fi; "
        )
        command = git_setup + command
    return command


async def exec_command(
    s3_client: object,
    bucket: str,
    s3_prefix: str,
    command: str,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    timeout_sec: int | None = None,
) -> tuple[str | None, str | None, int]:
    """Execute a command in the container via the S3 command channel.

    Submits a command JSON to S3, polls for the result.

    Returns:
        Tuple of (stdout, stderr, return_code).
    """
    full_command = _build_full_command(command, cwd, env)
    full_command = _apply_sweagent_patches(full_command)

    cmd_uuid = uuid.uuid4().hex[:12]
    cmd_key = f"{s3_prefix}commands/{cmd_uuid}.json"
    result_key = f"{s3_prefix}results/{cmd_uuid}.json"

    cmd_payload = {
        "type": "exec",
        "command": full_command,
        "timeout_sec": timeout_sec or 300,
    }

    logger.debug("Submitting command %s: %s", cmd_uuid, command[:200])

    # Submit command to S3
    await asyncio.to_thread(
        s3_client.put_object,  # type: ignore[union-attr]
        Bucket=bucket,
        Key=cmd_key,
        Body=json.dumps(cmd_payload).encode(),
    )

    # Poll for result
    effective_timeout = timeout_sec or 300
    return await _poll_result(s3_client, bucket, result_key, effective_timeout + 30)


async def _poll_result(
    s3_client: object,
    bucket: str,
    result_key: str,
    timeout_sec: int,
) -> tuple[str | None, str | None, int]:
    """Poll S3 for a result file."""
    poll_interval = 0.5
    elapsed = 0.0

    while elapsed < timeout_sec:
        try:
            resp = await asyncio.to_thread(
                s3_client.get_object,  # type: ignore[union-attr]
                Bucket=bucket,
                Key=result_key,
            )
            body = await asyncio.to_thread(resp["Body"].read)
            result = json.loads(body)
            return result.get("stdout"), result.get("stderr"), result.get("return_code", 1)
        except Exception as e:
            if "NoSuchKey" not in str(e):
                logger.warning("Error polling result %s: %s", result_key, e)

        await asyncio.sleep(poll_interval)
        elapsed += poll_interval
        # Increase interval over time: 0.5s → 1s → 2s
        if elapsed > 10:
            poll_interval = min(poll_interval * 1.5, 2.0)

    return None, f"Command timed out waiting for result after {timeout_sec}s", 124


# ---------------------------------------------------------------------------
# File transfer via S3
# ---------------------------------------------------------------------------


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential_jitter(initial=1, max=10, jitter=2),
    reraise=True,
)
async def upload_file(
    s3_client: object,
    bucket: str,
    s3_prefix: str,
    source_path: Path,
    target_path: str,
) -> None:
    """Upload a local file to the container via S3."""
    transfer_uuid = uuid.uuid4().hex[:12]
    s3_key = f"{s3_prefix}uploads/{transfer_uuid}"

    # Upload file to S3
    data = source_path.read_bytes()
    await asyncio.to_thread(
        s3_client.put_object,  # type: ignore[union-attr]
        Bucket=bucket,
        Key=s3_key,
        Body=data,
    )

    # Tell the daemon to download and place it
    cmd_uuid = uuid.uuid4().hex[:12]
    cmd_key = f"{s3_prefix}commands/{cmd_uuid}.json"
    result_key = f"{s3_prefix}results/{cmd_uuid}.json"

    cmd_payload = {
        "type": "upload",
        "s3_key": s3_key,
        "target_path": target_path,
        "is_dir": False,
    }
    await asyncio.to_thread(
        s3_client.put_object,  # type: ignore[union-attr]
        Bucket=bucket,
        Key=cmd_key,
        Body=json.dumps(cmd_payload).encode(),
    )

    stdout, stderr, rc = await _poll_result(s3_client, bucket, result_key, 60)
    if rc != 0:
        raise RuntimeError(f"Failed to upload {source_path} to {target_path}: {stderr}")

    logger.debug("Uploaded %s -> %s (%d bytes)", source_path, target_path, len(data))


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential_jitter(initial=1, max=10, jitter=2),
    reraise=True,
)
async def upload_dir(
    s3_client: object,
    bucket: str,
    s3_prefix: str,
    source_dir: Path,
    target_dir: str,
) -> None:
    """Upload a local directory to the container via S3."""
    dir_files = [f for f in source_dir.rglob("*") if f.is_file()]
    if not dir_files:
        logger.warning("No files to upload from %s", source_dir)
        return

    # Create tar archive
    tar_buffer = io.BytesIO()
    with tarfile.open(fileobj=tar_buffer, mode="w:gz") as tar:
        for item in dir_files:
            arcname = str(item.relative_to(source_dir))
            tar.add(str(item), arcname=arcname)
    tar_data = tar_buffer.getvalue()

    transfer_uuid = uuid.uuid4().hex[:12]
    s3_key = f"{s3_prefix}uploads/{transfer_uuid}.tar.gz"

    # Upload tar to S3
    await asyncio.to_thread(
        s3_client.put_object,  # type: ignore[union-attr]
        Bucket=bucket,
        Key=s3_key,
        Body=tar_data,
    )

    # Tell the daemon to download and extract
    cmd_uuid = uuid.uuid4().hex[:12]
    cmd_key = f"{s3_prefix}commands/{cmd_uuid}.json"
    result_key = f"{s3_prefix}results/{cmd_uuid}.json"

    cmd_payload = {
        "type": "upload",
        "s3_key": s3_key,
        "target_path": target_dir,
        "is_dir": True,
    }
    await asyncio.to_thread(
        s3_client.put_object,  # type: ignore[union-attr]
        Bucket=bucket,
        Key=cmd_key,
        Body=json.dumps(cmd_payload).encode(),
    )

    stdout, stderr, rc = await _poll_result(s3_client, bucket, result_key, 60)
    if rc != 0:
        raise RuntimeError(f"Failed to upload directory {source_dir} to {target_dir}: {stderr}")

    logger.debug("Uploaded %d files from %s -> %s (%d bytes)", len(dir_files), source_dir, target_dir, len(tar_data))


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential_jitter(initial=1, max=10, jitter=2),
    reraise=True,
)
async def download_file(
    s3_client: object,
    bucket: str,
    s3_prefix: str,
    source_path: str,
    target_path: Path,
) -> None:
    """Download a file from the container via S3."""
    target_path.parent.mkdir(parents=True, exist_ok=True)

    transfer_uuid = uuid.uuid4().hex[:12]
    s3_key = f"{s3_prefix}downloads/{transfer_uuid}"

    # Tell the daemon to upload the file to S3
    cmd_uuid = uuid.uuid4().hex[:12]
    cmd_key = f"{s3_prefix}commands/{cmd_uuid}.json"
    result_key = f"{s3_prefix}results/{cmd_uuid}.json"

    cmd_payload = {
        "type": "download",
        "source_path": source_path,
        "s3_key": s3_key,
        "is_dir": False,
    }
    await asyncio.to_thread(
        s3_client.put_object,  # type: ignore[union-attr]
        Bucket=bucket,
        Key=cmd_key,
        Body=json.dumps(cmd_payload).encode(),
    )

    stdout, stderr, rc = await _poll_result(s3_client, bucket, result_key, 60)
    if rc != 0:
        raise RuntimeError(f"Failed to download {source_path}: {stderr}")

    # Download from S3
    resp = await asyncio.to_thread(
        s3_client.get_object,  # type: ignore[union-attr]
        Bucket=bucket,
        Key=s3_key,
    )
    data = await asyncio.to_thread(resp["Body"].read)
    target_path.write_bytes(data)

    logger.debug("Downloaded %s -> %s (%d bytes)", source_path, target_path, len(data))


@retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential_jitter(initial=2, max=30, jitter=3),
    reraise=True,
)
async def download_dir(
    s3_client: object,
    bucket: str,
    s3_prefix: str,
    source_dir: str,
    target_dir: Path,
) -> None:
    """Download a directory from the container via S3."""
    target_dir.mkdir(parents=True, exist_ok=True)

    transfer_uuid = uuid.uuid4().hex[:12]
    s3_key = f"{s3_prefix}downloads/{transfer_uuid}.tar.gz"

    # Tell the daemon to tar and upload to S3
    cmd_uuid = uuid.uuid4().hex[:12]
    cmd_key = f"{s3_prefix}commands/{cmd_uuid}.json"
    result_key = f"{s3_prefix}results/{cmd_uuid}.json"

    cmd_payload = {
        "type": "download",
        "source_path": source_dir,
        "s3_key": s3_key,
        "is_dir": True,
    }
    await asyncio.to_thread(
        s3_client.put_object,  # type: ignore[union-attr]
        Bucket=bucket,
        Key=cmd_key,
        Body=json.dumps(cmd_payload).encode(),
    )

    stdout, stderr, rc = await _poll_result(s3_client, bucket, result_key, 120)
    if rc != 0:
        raise RuntimeError(f"Failed to download directory {source_dir}: {stderr}")

    # Download tar from S3
    resp = await asyncio.to_thread(
        s3_client.get_object,  # type: ignore[union-attr]
        Bucket=bucket,
        Key=s3_key,
    )
    tar_data = await asyncio.to_thread(resp["Body"].read)
    tar_buffer = io.BytesIO(tar_data)

    try:
        with tarfile.open(fileobj=tar_buffer, mode="r:gz") as tar:
            tar.extractall(path=str(target_dir))
    except tarfile.TarError as e:
        raise RuntimeError(f"Failed to extract directory {source_dir}: {e}") from e

    logger.debug("Downloaded directory %s -> %s (%d bytes)", source_dir, target_dir, len(tar_data))
