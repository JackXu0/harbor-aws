"""File transfer to/from ECS containers via base64-over-exec."""

from __future__ import annotations

import base64
import io
import logging
import tarfile
from collections.abc import Awaitable, Callable
from pathlib import Path

from tenacity import retry, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)

# Type for the exec function bound to a specific task
ExecFn = Callable[..., Awaitable[tuple[str | None, str | None, int]]]

# Max size for base64-over-exec (5 MB before encoding)
MAX_EXEC_TRANSFER_BYTES = 5 * 1024 * 1024


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    reraise=True,
)
async def upload_file(
    exec_fn: ExecFn,
    source_path: Path,
    target_path: str,
) -> None:
    """Upload a local file to the container via base64-over-exec."""
    data = source_path.read_bytes()

    if len(data) > MAX_EXEC_TRANSFER_BYTES:
        logger.warning(
            "File %s is %d bytes, may be slow via exec transfer. Consider using S3 for large files.",
            source_path,
            len(data),
        )

    encoded = base64.b64encode(data).decode("ascii")

    # Create parent directory and write file
    target_dir = str(Path(target_path).parent)
    stdout, stderr, rc = await exec_fn(
        command=f"mkdir -p {target_dir} && echo '{encoded}' | base64 -d > {target_path}",
    )
    if rc != 0:
        raise RuntimeError(f"Failed to upload {source_path} to {target_path}: {stderr}")

    logger.debug("Uploaded %s -> %s (%d bytes)", source_path, target_path, len(data))


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    reraise=True,
)
async def upload_dir(
    exec_fn: ExecFn,
    source_dir: Path,
    target_dir: str,
) -> None:
    """Upload a local directory to the container via tar + base64."""
    files = [f for f in source_dir.rglob("*") if f.is_file()]
    if not files:
        logger.warning("No files to upload from %s", source_dir)
        return

    # Create tar archive in memory
    tar_buffer = io.BytesIO()
    with tarfile.open(fileobj=tar_buffer, mode="w:gz") as tar:
        for item in files:
            arcname = str(item.relative_to(source_dir))
            tar.add(str(item), arcname=arcname)
    tar_data = tar_buffer.getvalue()

    if len(tar_data) > MAX_EXEC_TRANSFER_BYTES:
        logger.warning(
            "Directory %s tar is %d bytes, may be slow via exec transfer.",
            source_dir,
            len(tar_data),
        )

    encoded = base64.b64encode(tar_data).decode("ascii")

    # Create target directory and extract
    stdout, stderr, rc = await exec_fn(
        command=f"mkdir -p {target_dir} && echo '{encoded}' | base64 -d | tar xzf - -C {target_dir}",
    )
    if rc != 0:
        raise RuntimeError(f"Failed to upload directory {source_dir} to {target_dir}: {stderr}")

    logger.debug("Uploaded %d files from %s -> %s (%d bytes)", len(files), source_dir, target_dir, len(tar_data))


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    reraise=True,
)
async def download_file(
    exec_fn: ExecFn,
    source_path: str,
    target_path: Path,
) -> None:
    """Download a file from the container via base64-over-exec."""
    target_path.parent.mkdir(parents=True, exist_ok=True)

    stdout, stderr, rc = await exec_fn(
        command=f"base64 {source_path}",
    )
    if rc != 0:
        raise RuntimeError(f"Failed to download {source_path}: {stderr}")
    if not stdout:
        raise RuntimeError(f"No data received when downloading {source_path}")

    # Decode and write
    data = base64.b64decode(stdout.strip())
    target_path.write_bytes(data)

    logger.debug("Downloaded %s -> %s (%d bytes)", source_path, target_path, len(data))


@retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    reraise=True,
)
async def download_dir(
    exec_fn: ExecFn,
    source_dir: str,
    target_dir: Path,
) -> None:
    """Download a directory from the container via tar + base64."""
    target_dir.mkdir(parents=True, exist_ok=True)

    stdout, stderr, rc = await exec_fn(
        command=f"cd {source_dir} && tar czf - . | base64",
    )
    if rc != 0:
        raise RuntimeError(f"Failed to download directory {source_dir}: {stderr}")
    if not stdout:
        raise RuntimeError(f"No data received when downloading directory {source_dir}")

    # Decode and extract
    tar_data = base64.b64decode(stdout.strip())
    tar_buffer = io.BytesIO(tar_data)

    try:
        with tarfile.open(fileobj=tar_buffer, mode="r:gz") as tar:
            tar.extractall(path=str(target_dir))
    except tarfile.TarError as e:
        raise RuntimeError(f"Failed to extract directory {source_dir}: {e}") from e

    logger.debug("Downloaded directory %s -> %s (%d bytes)", source_dir, target_dir, len(tar_data))
