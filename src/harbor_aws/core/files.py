"""File transfer between the orchestrator and pods via tar-over-exec.

Harbor's BaseEnvironment interface requires upload_file/download_file methods for
moving test suites, agent scripts, logs, and artifacts between the orchestrator and
pods. This implements that contract by piping tar archives through Kubernetes
WebSocket exec — same mechanism as `kubectl cp`.
"""

from __future__ import annotations

import asyncio
import io
import logging
import shlex
import tarfile
from pathlib import Path

from kubernetes import client
from kubernetes.stream import stream

from harbor_aws.core.exec import _make_isolated_api

logger = logging.getLogger(__name__)

# Limit concurrent file transfers to avoid overwhelming the K8s API server
_TRANSFER_CONCURRENCY = 100
_transfer_semaphore: asyncio.Semaphore | None = None


def _get_transfer_semaphore() -> asyncio.Semaphore:
    global _transfer_semaphore
    if _transfer_semaphore is None:
        _transfer_semaphore = asyncio.Semaphore(_TRANSFER_CONCURRENCY)
    return _transfer_semaphore


async def upload_file(
    pod_name: str,
    namespace: str,
    source_path: str,
    target_path: str,
    container: str = "main",
) -> None:
    """Upload a local file to a pod via tar-over-exec."""
    src = Path(source_path)
    if not src.exists():
        raise FileNotFoundError(f"Source file not found: {source_path}")

    target = Path(target_path)
    target_dir = str(target.parent) if target.parent != target else "/"
    target_name = target.name

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(str(src), arcname=target_name)
    tar_data = buf.getvalue()

    async with _get_transfer_semaphore():
        await asyncio.to_thread(
            _exec_tar_upload, pod_name, namespace, container, target_dir, tar_data,
        )


async def upload_dir(
    pod_name: str,
    namespace: str,
    source_dir: str,
    target_dir: str,
    container: str = "main",
) -> None:
    """Upload a local directory to a pod via tar-over-exec."""
    src = Path(source_dir)
    if not src.exists():
        raise FileNotFoundError(f"Source directory not found: {source_dir}")

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for item in src.iterdir():
            tar.add(str(item), arcname=item.name)
    tar_data = buf.getvalue()

    async with _get_transfer_semaphore():
        await asyncio.to_thread(
            _exec_tar_upload, pod_name, namespace, container, target_dir, tar_data,
        )


async def download_file(
    pod_name: str,
    namespace: str,
    source_path: str,
    target_path: str,
    container: str = "main",
) -> None:
    """Download a file from a pod via tar-over-exec."""
    Path(target_path).parent.mkdir(parents=True, exist_ok=True)

    async with _get_transfer_semaphore():
        tar_data = await asyncio.to_thread(
            _exec_tar_download, pod_name, namespace, container, source_path,
        )

    buf = io.BytesIO(tar_data)
    with tarfile.open(fileobj=buf, mode="r:*") as tar:
        # Extract the single file
        members = tar.getmembers()
        if not members:
            raise RuntimeError(f"No file found at {source_path} in pod {pod_name}")
        member = members[0]
        extracted = tar.extractfile(member)
        if extracted is None:
            raise RuntimeError(f"Could not extract {source_path} from pod {pod_name}")
        Path(target_path).write_bytes(extracted.read())


async def download_dir(
    pod_name: str,
    namespace: str,
    source_dir: str,
    target_dir: str,
    container: str = "main",
) -> None:
    """Download a directory from a pod via tar-over-exec."""
    Path(target_dir).mkdir(parents=True, exist_ok=True)

    async with _get_transfer_semaphore():
        tar_data = await asyncio.to_thread(
            _exec_tar_download_dir, pod_name, namespace, container, source_dir,
        )

    buf = io.BytesIO(tar_data)
    with tarfile.open(fileobj=buf, mode="r:*") as tar:
        tar.extractall(path=target_dir, filter="data")


def _exec_tar_upload(
    pod_name: str,
    namespace: str,
    container: str,
    target_dir: str,
    tar_data: bytes,
) -> None:
    """Upload tar data to a pod by piping it to 'tar xzf -'."""
    exec_api = _make_isolated_api()
    resp = stream(
        exec_api.connect_get_namespaced_pod_exec,
        name=pod_name,
        namespace=namespace,
        container=container,
        command=["sh", "-c", f"mkdir -p {shlex.quote(target_dir)} && tar xzf - -C {shlex.quote(target_dir)}"],
        stderr=True,
        stdout=True,
        stdin=True,
        tty=False,
        _preload_content=False,
    )

    # Send tar data in chunks
    chunk_size = 64 * 1024
    for i in range(0, len(tar_data), chunk_size):
        resp.write_stdin(tar_data[i : i + chunk_size])

    resp.close()

    stderr = resp.read_stderr() or ""
    if stderr.strip():
        logger.debug("tar upload stderr for %s: %s", pod_name, stderr[:200])


def _exec_tar_download(
    pod_name: str,
    namespace: str,
    container: str,
    source_path: str,
) -> bytes:
    """Download tar data from a pod by piping 'tar czf -' and base64-encoding.

    The K8s WebSocket stream corrupts raw binary data (decodes as UTF-8).
    We base64-encode on the pod side and decode locally to preserve bytes.
    """
    import base64 as b64

    exec_api = _make_isolated_api()
    resp = stream(
        exec_api.connect_get_namespaced_pod_exec,
        name=pod_name,
        namespace=namespace,
        container=container,
        command=[
            "sh", "-c",
            f"tar czf - -C {shlex.quote(str(Path(source_path).parent))} {shlex.quote(Path(source_path).name)} | base64",
        ],
        stderr=True,
        stdout=True,
        stdin=False,
        tty=False,
        _preload_content=False,
    )

    resp.run_forever(timeout=120)

    stdout_data = resp.read_stdout(timeout=5) or ""
    stderr = resp.read_stderr() or ""

    if stderr.strip():
        logger.debug("tar download stderr for %s: %s", pod_name, stderr[:200])

    if not stdout_data:
        raise RuntimeError(f"No data returned from tar download of {source_path} in pod {pod_name}")

    # Decode base64 text back to binary tar data
    return b64.b64decode(stdout_data)


def _exec_tar_download_dir(
    pod_name: str,
    namespace: str,
    container: str,
    source_dir: str,
) -> bytes:
    """Download directory contents (not the directory itself) from a pod.

    Uses 'tar czf - -C <dir> .' to tar the contents, then base64-encodes.
    """
    import base64 as b64

    exec_api = _make_isolated_api()
    resp = stream(
        exec_api.connect_get_namespaced_pod_exec,
        name=pod_name,
        namespace=namespace,
        container=container,
        command=[
            "sh", "-c",
            f"tar czf - -C {shlex.quote(source_dir)} . | base64",
        ],
        stderr=True,
        stdout=True,
        stdin=False,
        tty=False,
        _preload_content=False,
    )

    resp.run_forever(timeout=120)

    stdout_data = resp.read_stdout(timeout=5) or ""
    stderr = resp.read_stderr() or ""

    if stderr.strip():
        logger.debug("tar download_dir stderr for %s: %s", pod_name, stderr[:200])

    if not stdout_data:
        raise RuntimeError(f"No data returned from tar download of {source_dir} in pod {pod_name}")

    return b64.b64decode(stdout_data)
