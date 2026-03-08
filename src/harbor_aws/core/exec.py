"""Command execution in ECS containers via ECS Exec (SSM)."""

from __future__ import annotations

import asyncio
import logging
import shlex
import uuid

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

    if env:
        for key, value in env.items():
            parts.append(f"export {key}={shlex.quote(value)};")

    if cwd:
        parts.append(f"cd {shlex.quote(cwd)} &&")

    parts.append(command)
    return " ".join(parts)


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

    # Wrap command to capture exit code via sentinel
    sentinel = f"__HARBOR_EXIT_{uuid.uuid4().hex[:8]}__"
    wrapped = f'{full_command}; echo "{sentinel}$?{sentinel}"'

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
        f"/bin/sh -c {shlex.quote(wrapped)}",
        "--non-interactive",
        "--region",
        config.region,
        "--output",
        "text",
    ]

    if config.profile_name:
        cli_args.extend(["--profile", config.profile_name])

    logger.debug("Executing in task %s: %s", task_arn, command[:200])

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
            return 0

        after_sentinel = stdout[start + len(sentinel) :]
        end = after_sentinel.find(sentinel)
        if end == -1:
            return 0

        code_str = after_sentinel[:end].strip()
        return int(code_str)
    except (ValueError, IndexError):
        return 0


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
