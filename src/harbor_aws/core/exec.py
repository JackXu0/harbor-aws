"""Command execution and file transfer in ECS containers via ECS Exec (SSM)."""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import shlex
import tarfile
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path

from tenacity import retry, retry_if_result, stop_after_attempt, wait_exponential_jitter

from harbor_aws.core.config import AWSConfig

logger = logging.getLogger(__name__)


async def check_session_manager_plugin() -> None:
    """Verify session-manager-plugin is installed."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "session-manager-plugin",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
    except FileNotFoundError as err:
        raise RuntimeError(
            "session-manager-plugin not found. Install it from: "
            "https://docs.aws.amazon.com/systems-manager/latest/userguide/"
            "session-manager-working-with-install-plugin.html"
        ) from err


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


def _is_throttled(result: tuple[str | None, str | None, int]) -> bool:
    """Check if the exec result indicates a throttling error."""
    _, stderr, rc = result
    return rc != 0 and stderr is not None and "ThrottlingException" in stderr


def _return_last_result(retry_state):  # type: ignore[no-untyped-def]
    """Return the last result instead of raising RetryError."""
    return retry_state.outcome.result()


@retry(
    stop=stop_after_attempt(15),
    wait=wait_exponential_jitter(initial=2, max=30, jitter=5),
    retry=retry_if_result(_is_throttled),
    retry_error_callback=_return_last_result,
)
async def exec_command(
    config: AWSConfig,
    task_arn: str,
    command: str,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    timeout_sec: int | None = None,
    container_name: str = "main",
) -> tuple[str | None, str | None, int]:
    """Execute a command in a running Fargate task via ECS Exec.

    Uses `aws ecs execute-command --non-interactive` as a subprocess.
    Wraps the command with a sentinel to reliably capture the exit code.

    Returns:
        Tuple of (stdout, stderr, return_code).
    """
    full_command = _build_full_command(command, cwd, env)

    # Fix Harbor swe-agent adapter issues:
    # 1. /etc/profile.d/testbed-conda.sh may not exist — source only if present
    full_command = full_command.replace(
        ". /etc/profile.d/testbed-conda.sh",
        "if [ -f /etc/profile.d/testbed-conda.sh ]; then . /etc/profile.d/testbed-conda.sh; fi",
    )
    # 1b. litellm needs boto3 for Bedrock auth — install into sweagent venv before running
    full_command = full_command.replace(
        "sweagent run ",
        "/opt/sweagent-venv/bin/pip install -q boto3 2>/dev/null; sweagent run ",
    )
    # 2. When no /testbed, swe-agent needs a preexisting git repo at $(pwd).
    #    The adapter uses --env.repo.path=$(pwd) which tries to copytree into an existing dir and fails.
    #    Fix: git init $(pwd) and use --env.repo.type=preexisting --env.repo.repo_name=$(pwd) instead.
    full_command = full_command.replace(
        "echo '--env.repo.path=$(pwd)'",
        "echo \"--env.repo.type=preexisting --env.repo.repo_name=$(pwd)\"",
    )
    if "sweagent run" in full_command and "--env.repo.repo_name=" in full_command:
        git_setup = (
            "if ! git rev-parse --git-dir > /dev/null 2>&1; then"
            " git init -q . && git add -A && git commit -q -m init --allow-empty;"
            " fi; "
        )
        full_command = git_setup + full_command

    # Wrap command with start/end sentinels to extract output from SSM session noise
    sentinel = f"__HARBOR_EXIT_{uuid.uuid4().hex[:8]}__"
    start_sentinel = f"__HARBOR_START_{uuid.uuid4().hex[:8]}__"
    wrapped = f'echo "{start_sentinel}"; {full_command}; echo "{sentinel}$?{sentinel}"'

    cli_args = [
        "aws",
        "ecs",
        "execute-command",
        "--cluster",
        config.cluster_name,
        "--task",
        task_arn,
        "--container",
        container_name,
        "--command",
        f"/bin/bash -c {shlex.quote(wrapped)}",
        "--interactive",
        "--region",
        config.region,
        "--output",
        "text",
    ]

    if config.profile_name:
        cli_args.extend(["--profile", config.profile_name])

    logger.debug("Executing in task %s: %s", task_arn, command[:200])

    # SSM session-manager-plugin requires a PTY — without one the session drops
    # as soon as output stalls. Wrap with `script` to provide a pseudo-TTY.
    import platform

    if platform.system() == "Darwin":
        cli_args = ["script", "-q", "/dev/null"] + cli_args
    else:
        cli_args = ["script", "-qc", shlex.join(cli_args), "/dev/null"]

    process = await asyncio.create_subprocess_exec(
        *cli_args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    effective_timeout = timeout_sec or 300

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            process.communicate(),
            timeout=effective_timeout,
        )
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        return None, f"Command timed out after {effective_timeout}s", 124

    stdout = stdout_bytes.decode(errors="replace") if stdout_bytes else ""
    stderr = stderr_bytes.decode(errors="replace") if stderr_bytes else ""

    # If the CLI itself failed (e.g., auth error, task not found)
    if process.returncode != 0 and sentinel not in stdout:
        return None, stderr or f"aws ecs execute-command failed with code {process.returncode}", 1

    # Detect SSM session drop — session closed before command finished
    if sentinel not in stdout and "Cannot perform start session" in stdout:
        return None, "SSM session dropped (EOF). The command may not have completed.", 1

    # Strip SSM session noise: extract content between start sentinel and exit sentinel
    stdout = _strip_session_noise(stdout, start_sentinel)

    # Parse exit code from sentinel
    return_code = _parse_exit_code(stdout, sentinel)
    stdout = _strip_sentinel(stdout, sentinel)

    return stdout or None, stderr or None, return_code


def _parse_exit_code(stdout: str, sentinel: str) -> int:
    """Extract the exit code from the sentinel pattern in stdout."""
    try:
        # Find the sentinel pattern: __HARBOR_EXIT_xxxx__<code>__HARBOR_EXIT_xxxx__
        start = stdout.rfind(sentinel)
        if start == -1:
            # Sentinel not found — session likely dropped before command finished
            return 1

        after_sentinel = stdout[start + len(sentinel) :]
        end = after_sentinel.find(sentinel)
        if end == -1:
            return 0

        code_str = after_sentinel[:end].strip()
        return int(code_str)
    except (ValueError, IndexError):
        return 0


def _strip_session_noise(stdout: str, start_sentinel: str) -> str:
    """Remove SSM session preamble/postamble from interactive mode output."""
    start = stdout.find(start_sentinel)
    if start == -1:
        return stdout
    # Skip past the start sentinel and its newline
    start += len(start_sentinel)
    if start < len(stdout) and stdout[start] == "\n":
        start += 1
    return stdout[start:]


def _strip_sentinel(stdout: str, sentinel: str) -> str:
    """Remove the sentinel pattern from stdout."""
    start = stdout.rfind(sentinel)
    if start == -1:
        return stdout

    # Remove everything from the last newline before the sentinel to the end
    last_newline = stdout.rfind("\n", 0, start)
    if last_newline == -1:
        return ""
    return stdout[:last_newline]


# ---------------------------------------------------------------------------
# File transfer via base64-over-exec
# ---------------------------------------------------------------------------

# Type for the exec function bound to a specific task
ExecFn = Callable[..., Awaitable[tuple[str | None, str | None, int]]]

# Max size for a single exec command payload (~100KB to stay well under OS arg limit)
_CHUNK_SIZE = 100_000


async def _chunked_upload(exec_fn: ExecFn, encoded: str, remote_path: str) -> None:
    """Upload base64-encoded data to the container in chunks."""
    for i in range(0, len(encoded), _CHUNK_SIZE):
        chunk = encoded[i : i + _CHUNK_SIZE]
        op = ">" if i == 0 else ">>"
        stdout, stderr, rc = await exec_fn(
            command=f"printf '%s' '{chunk}' {op} {remote_path}",
        )
        if rc != 0:
            raise RuntimeError(f"Failed to write chunk to {remote_path}: {stderr}")


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential_jitter(initial=1, max=10, jitter=2),
    reraise=True,
)
async def upload_file(
    exec_fn: ExecFn,
    source_path: Path,
    target_path: str,
) -> None:
    """Upload a local file to the container via base64-over-exec."""
    data = source_path.read_bytes()
    encoded = base64.b64encode(data).decode("ascii")
    target_dir = str(Path(target_path).parent)

    if len(encoded) <= _CHUNK_SIZE:
        stdout, stderr, rc = await exec_fn(
            command=f"mkdir -p {target_dir} && printf '%s' '{encoded}' | base64 -d > {target_path}",
        )
        if rc != 0:
            raise RuntimeError(f"Failed to upload {source_path} to {target_path}: {stderr}")
    else:
        tmp = f"/tmp/_harbor_upload_{id(data)}"
        await exec_fn(command=f"mkdir -p {target_dir}")
        await _chunked_upload(exec_fn, encoded, tmp)
        stdout, stderr, rc = await exec_fn(
            command=f"base64 -d < {tmp} > {target_path} && rm -f {tmp}",
        )
        if rc != 0:
            raise RuntimeError(f"Failed to decode upload {source_path} to {target_path}: {stderr}")

    logger.debug("Uploaded %s -> %s (%d bytes)", source_path, target_path, len(data))


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential_jitter(initial=1, max=10, jitter=2),
    reraise=True,
)
async def upload_dir(
    exec_fn: ExecFn,
    source_dir: Path,
    target_dir: str,
) -> None:
    """Upload a local directory to the container via tar + base64."""
    dir_files = [f for f in source_dir.rglob("*") if f.is_file()]
    if not dir_files:
        logger.warning("No files to upload from %s", source_dir)
        return

    tar_buffer = io.BytesIO()
    with tarfile.open(fileobj=tar_buffer, mode="w:gz") as tar:
        for item in dir_files:
            arcname = str(item.relative_to(source_dir))
            tar.add(str(item), arcname=arcname)
    tar_data = tar_buffer.getvalue()
    encoded = base64.b64encode(tar_data).decode("ascii")

    if len(encoded) <= _CHUNK_SIZE:
        stdout, stderr, rc = await exec_fn(
            command=f"mkdir -p {target_dir} && printf '%s' '{encoded}' | base64 -d | tar xzf - -C {target_dir}",
        )
        if rc != 0:
            raise RuntimeError(f"Failed to upload directory {source_dir} to {target_dir}: {stderr}")
    else:
        tmp = f"/tmp/_harbor_upload_{id(tar_data)}"
        await exec_fn(command=f"mkdir -p {target_dir}")
        await _chunked_upload(exec_fn, encoded, tmp)
        stdout, stderr, rc = await exec_fn(
            command=f"base64 -d < {tmp} | tar xzf - -C {target_dir} && rm -f {tmp}",
        )
        if rc != 0:
            raise RuntimeError(f"Failed to extract upload {source_dir} to {target_dir}: {stderr}")

    logger.debug("Uploaded %d files from %s -> %s (%d bytes)", len(dir_files), source_dir, target_dir, len(tar_data))


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential_jitter(initial=1, max=10, jitter=2),
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

    data = base64.b64decode(stdout.strip())
    target_path.write_bytes(data)

    logger.debug("Downloaded %s -> %s (%d bytes)", source_path, target_path, len(data))


@retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential_jitter(initial=2, max=30, jitter=3),
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

    tar_data = base64.b64decode(stdout.strip())
    tar_buffer = io.BytesIO(tar_data)

    try:
        with tarfile.open(fileobj=tar_buffer, mode="r:gz") as tar:
            tar.extractall(path=str(target_dir))
    except tarfile.TarError as e:
        raise RuntimeError(f"Failed to extract directory {source_dir}: {e}") from e

    logger.debug("Downloaded directory %s -> %s (%d bytes)", source_dir, target_dir, len(tar_data))
