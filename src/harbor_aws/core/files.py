"""File transfer via tar-over-exec (same mechanism as kubectl cp)."""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import shlex
import tarfile
from pathlib import Path

from kubernetes import client
from kubernetes.stream import stream

from harbor_aws.core.exec import _make_isolated_api

logger = logging.getLogger(__name__)

_TRANSFER_CONCURRENCY = 100
_transfer_semaphore: asyncio.Semaphore | None = None


def _get_transfer_semaphore() -> asyncio.Semaphore:
    global _transfer_semaphore
    if _transfer_semaphore is None:
        _transfer_semaphore = asyncio.Semaphore(_TRANSFER_CONCURRENCY)
    return _transfer_semaphore


# --- public API ---


async def upload_file(
    pod_name: str, namespace: str, source_path: str, target_path: str, container: str = "main",
) -> None:
    """Upload a local file to a pod."""
    src = Path(source_path)
    if not src.exists():
        raise FileNotFoundError(f"Source file not found: {source_path}")

    target = Path(target_path)
    tar_data = _create_tar({target.name: str(src)})

    async with _get_transfer_semaphore():
        await asyncio.to_thread(_exec_tar_upload, pod_name, namespace, container, str(target.parent), tar_data)


async def upload_dir(
    pod_name: str, namespace: str, source_dir: str, target_dir: str, container: str = "main",
) -> None:
    """Upload a local directory to a pod."""
    src = Path(source_dir)
    if not src.exists():
        raise FileNotFoundError(f"Source directory not found: {source_dir}")

    tar_data = _create_tar({item.name: str(item) for item in src.iterdir()})

    async with _get_transfer_semaphore():
        await asyncio.to_thread(_exec_tar_upload, pod_name, namespace, container, target_dir, tar_data)


async def download_file(
    pod_name: str, namespace: str, source_path: str, target_path: str, container: str = "main",
) -> None:
    """Download a file from a pod."""
    Path(target_path).parent.mkdir(parents=True, exist_ok=True)

    src = Path(source_path)
    tar_cmd = f"tar czf - -C {shlex.quote(str(src.parent))} {shlex.quote(src.name)} | base64"

    async with _get_transfer_semaphore():
        tar_data = await asyncio.to_thread(_exec_tar_download, pod_name, namespace, container, tar_cmd)

    with tarfile.open(fileobj=io.BytesIO(tar_data), mode="r:*") as tar:
        members = tar.getmembers()
        if not members:
            raise RuntimeError(f"No file found at {source_path} in pod {pod_name}")
        extracted = tar.extractfile(members[0])
        if extracted is None:
            raise RuntimeError(f"Could not extract {source_path} from pod {pod_name}")
        Path(target_path).write_bytes(extracted.read())


async def download_dir(
    pod_name: str, namespace: str, source_dir: str, target_dir: str, container: str = "main",
) -> None:
    """Download a directory from a pod."""
    Path(target_dir).mkdir(parents=True, exist_ok=True)

    tar_cmd = f"tar czf - -C {shlex.quote(source_dir)} . | base64"

    async with _get_transfer_semaphore():
        tar_data = await asyncio.to_thread(_exec_tar_download, pod_name, namespace, container, tar_cmd)

    with tarfile.open(fileobj=io.BytesIO(tar_data), mode="r:*") as tar:
        tar.extractall(path=target_dir, filter="data")


# --- helpers ---


def _create_tar(entries: dict[str, str]) -> bytes:
    """Create a gzipped tar archive. entries maps arcname -> local path."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for arcname, path in entries.items():
            tar.add(path, arcname=arcname)
    return buf.getvalue()


def _exec_tar_upload(
    pod_name: str, namespace: str, container: str, target_dir: str, tar_data: bytes,
) -> None:
    """Pipe tar data into a pod."""
    exec_api = _make_isolated_api()
    resp = stream(
        exec_api.connect_get_namespaced_pod_exec,
        name=pod_name, namespace=namespace, container=container,
        command=["sh", "-c", f"mkdir -p {shlex.quote(target_dir)} && tar xzf - -C {shlex.quote(target_dir)}"],
        stderr=True, stdout=True, stdin=True, tty=False, _preload_content=False,
    )

    for i in range(0, len(tar_data), 64 * 1024):
        resp.write_stdin(tar_data[i : i + 64 * 1024])
    resp.close()

    stderr = resp.read_stderr() or ""
    if stderr.strip():
        logger.debug("tar upload stderr for %s: %s", pod_name, stderr[:200])


def _exec_tar_download(pod_name: str, namespace: str, container: str, tar_cmd: str) -> bytes:
    """Run a tar|base64 command in a pod and return decoded bytes.

    K8s WebSocket corrupts raw binary (decodes as UTF-8), so we base64-encode on the pod.
    """
    exec_api = _make_isolated_api()
    resp = stream(
        exec_api.connect_get_namespaced_pod_exec,
        name=pod_name, namespace=namespace, container=container,
        command=["sh", "-c", tar_cmd],
        stderr=True, stdout=True, stdin=False, tty=False, _preload_content=False,
    )

    resp.run_forever(timeout=120)

    stdout_data = resp.read_stdout(timeout=5) or ""
    stderr = resp.read_stderr() or ""

    if stderr.strip():
        logger.debug("tar download stderr for %s: %s", pod_name, stderr[:200])
    if not stdout_data:
        raise RuntimeError(f"No data from tar download in pod {pod_name}")

    return base64.b64decode(stdout_data)
