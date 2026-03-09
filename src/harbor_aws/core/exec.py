"""Command execution via Kubernetes exec (WebSocket)."""

from __future__ import annotations

import asyncio
import logging
import shlex

from kubernetes import client
from kubernetes.stream import stream

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
    command = command.replace(
        ". /etc/profile.d/testbed-conda.sh",
        "if [ -f /etc/profile.d/testbed-conda.sh ]; then . /etc/profile.d/testbed-conda.sh; fi",
    )
    command = command.replace(
        "sweagent run ",
        "/opt/sweagent-venv/bin/pip install -q boto3 2>/dev/null; sweagent run ",
    )
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
    api: client.CoreV1Api,
    pod_name: str,
    namespace: str,
    command: str,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    timeout_sec: int | None = None,
    container: str = "main",
) -> tuple[str | None, str | None, int]:
    """Execute a command in a pod via Kubernetes exec.

    Returns:
        Tuple of (stdout, stderr, return_code).
    """
    full_command = _build_full_command(command, cwd, env)
    full_command = _apply_sweagent_patches(full_command)

    # Wrap command to capture exit code reliably
    wrapped = f"bash -lc {shlex.quote(full_command)}; echo \":::HARBOR_RC:::$?\""

    logger.debug("Exec in %s: %s", pod_name, command[:200])

    def _exec() -> tuple[str | None, str | None, int]:
        resp = stream(
            api.connect_get_namespaced_pod_exec,
            name=pod_name,
            namespace=namespace,
            container=container,
            command=["bash", "-c", wrapped],
            stderr=True,
            stdout=True,
            stdin=False,
            tty=False,
            _preload_content=False,
        )

        resp.run_forever(timeout=timeout_sec or 300)

        stdout = resp.read_stdout() or ""
        stderr = resp.read_stderr() or ""

        # Parse return code from our sentinel
        return_code = _parse_return_code(stdout)
        stdout = _strip_return_code_sentinel(stdout)

        return (stdout or None, stderr or None, return_code)

    return await asyncio.to_thread(_exec)


def _parse_return_code(stdout: str) -> int:
    """Extract return code from :::HARBOR_RC:::N sentinel."""
    for line in reversed(stdout.splitlines()):
        if line.startswith(":::HARBOR_RC:::"):
            try:
                return int(line.split(":::")[-1])
            except ValueError:
                pass
    return 1  # default to failure if sentinel not found


def _strip_return_code_sentinel(stdout: str) -> str:
    """Remove the :::HARBOR_RC::: sentinel line from stdout."""
    lines = stdout.splitlines()
    filtered = [line for line in lines if not line.startswith(":::HARBOR_RC:::")]
    return "\n".join(filtered)
