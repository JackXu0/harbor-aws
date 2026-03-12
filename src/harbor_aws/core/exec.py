"""Command execution via Kubernetes exec (WebSocket).

IMPORTANT: The kubernetes `stream()` function monkey-patches `api_client.request`
with `websocket_call` during exec. If we share the same CoreV1Api between exec
and REST calls, concurrent REST calls (e.g. read_namespaced_pod_status) get routed
through WebSocket and fail. To avoid this, we create a fresh CoreV1Api for each
stream() call.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
import time

from kubernetes import client, config as k8s_config
from kubernetes.stream import stream

logger = logging.getLogger(__name__)

# Retry config for transient WebSocket handshake failures (e.g. "Handshake status 200 OK")
_EXEC_MAX_RETRIES = 5
_EXEC_RETRY_BASE_DELAY = 2.0


def _make_isolated_api() -> client.CoreV1Api:
    """Create a fresh CoreV1Api with its own ApiClient.

    This prevents stream()'s monkey-patching of api_client.request from
    affecting concurrent REST calls on the shared client.
    Uses the current default configuration (set by load_kube_config).
    """
    return client.CoreV1Api(api_client=client.ApiClient())


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
    """Apply swe-agent command patches that are needed regardless of model."""
    # Make conda sourcing conditional (file may not exist)
    command = command.replace(
        ". /etc/profile.d/testbed-conda.sh",
        "if [ -f /etc/profile.d/testbed-conda.sh ]; then . /etc/profile.d/testbed-conda.sh; fi",
    )

    # Fix repo path: $(pwd) isn't expanded inside single quotes, so switch to preexisting repo mode
    command = command.replace(
        "echo '--env.repo.path=$(pwd)'",
        "echo \"--env.repo.type=preexisting --env.repo.repo_name=$(pwd)\"",
    )

    # Set up git identity and init repo if needed (for preexisting repos like /testbed or /app)
    if "sweagent run" in command and "--env.repo.repo_name=" in command:
        git_setup = (
            "export GIT_AUTHOR_NAME='harbor' GIT_AUTHOR_EMAIL='harbor@local'"
            " GIT_COMMITTER_NAME='harbor' GIT_COMMITTER_EMAIL='harbor@local';"
            " git config --global user.email 'harbor@local' 2>/dev/null || true;"
            " git config --global user.name 'harbor' 2>/dev/null || true;"
            " if ! git rev-parse --git-dir > /dev/null 2>&1; then"
            " git init -q . && touch .gitkeep && git add -A && git commit -q -m init;"
            " fi; "
        )
        command = git_setup + command

    return command


def _apply_bedrock_non_anthropic_patches(command: str) -> str:
    """Apply patches for non-Anthropic models on Bedrock.

    These handle:
    - Installing boto3 (required by litellm for Bedrock auth)
    - Upgrading litellm (older versions don't route to Converse API correctly)
    - Removing prompt caching config (only Anthropic models support cache_control)
    """
    if "sweagent run" not in command:
        return command

    command = command.replace(
        "sweagent run ",
        "(. $HOME/.local/bin/env 2>/dev/null;"
        " uv pip install -q boto3 litellm --upgrade --python /opt/sweagent-venv/bin/python"
        ") 2>/dev/null || true;"
        " sed -i 's/^  history_processors:$/  history_processors: []/; /- type: cache_control/d; /last_n_messages:/d'"
        " /opt/sweagent-configs/default.yaml 2>/dev/null || true;"
        " sweagent run ",
    )

    return command


def _is_anthropic_model(command: str) -> bool:
    """Check if the command uses an Anthropic model."""
    return "anthropic" in command or "claude" in command


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

    if not _is_anthropic_model(full_command):
        full_command = _apply_bedrock_non_anthropic_patches(full_command)

    # Wrap command to capture exit code reliably
    wrapped = f"bash -lc {shlex.quote(full_command)}; echo \":::HARBOR_RC:::$?\""

    logger.debug("Exec in %s: %s", pod_name, command[:200])

    def _exec() -> tuple[str | None, str | None, int]:
        last_exc: Exception | None = None
        for attempt in range(_EXEC_MAX_RETRIES):
            try:
                # Create an isolated CoreV1Api for this stream() call.
                # stream() monkey-patches api_client.request with websocket_call;
                # using a separate instance prevents this from breaking concurrent
                # REST calls on the shared client.
                exec_api = _make_isolated_api()
                resp = stream(
                    exec_api.connect_get_namespaced_pod_exec,
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
            except Exception as e:
                last_exc = e
                err_str = str(e)
                is_transient = "Handshake status" in err_str or "Unauthorized" in err_str
                if is_transient and attempt < _EXEC_MAX_RETRIES - 1:
                    delay = _EXEC_RETRY_BASE_DELAY * (2 ** attempt)
                    logger.warning(
                        "Exec failed for %s (attempt %d/%d, %s), retrying in %.1fs: %s",
                        pod_name, attempt + 1, _EXEC_MAX_RETRIES,
                        type(e).__name__, delay, err_str[:120],
                    )
                    time.sleep(delay)
                    continue
                if "Handshake status" in err_str:
                    logger.error(
                        "WebSocket exec failed after %d attempts for %s. "
                        "This usually means AWS credentials have expired. "
                        "Run 'aws sso login' to refresh.",
                        attempt + 1, pod_name,
                    )
                raise
        raise last_exc  # type: ignore[misc]

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
