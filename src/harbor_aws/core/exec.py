"""Command execution via Kubernetes WebSocket exec."""

from __future__ import annotations

import asyncio
import logging
import shlex

from kubernetes import client
from kubernetes.stream import stream
from tenacity import retry, retry_if_exception_message, stop_after_attempt, wait_exponential_jitter

logger = logging.getLogger(__name__)

_TRANSIENT_ERRORS = ("Handshake status", "Unauthorized", "nodename nor servname",
                     "Name or service not known", "Connection refused",
                     "Connection reset", "timed out")



def _make_isolated_api() -> client.CoreV1Api:
    """Return a CoreV1Api with its own ApiClient to avoid stream() thread-safety issues."""
    from harbor_aws.core.config import ensure_fresh_kubeconfig

    ensure_fresh_kubeconfig()
    return client.CoreV1Api(api_client=client.ApiClient())


def _build_full_command(
    command: str,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> str:
    """Build the full command string with cwd and env vars from Harbor."""
    parts = []

    if env:
        for key, value in env.items():
            parts.append(f"export {key}={shlex.quote(value)};")

    if cwd:
        parts.append(f"cd {shlex.quote(cwd)} &&")

    parts.append(command)
    return " ".join(parts)


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

    # Wrap command to capture exit code reliably
    wrapped = f"bash -lc {shlex.quote(full_command)}; echo \":::HARBOR_RC:::$?\""

    logger.debug("Exec in %s: %s", pod_name, command[:200])

    @retry(
        stop=stop_after_attempt(10),
        wait=wait_exponential_jitter(initial=1, max=30, jitter=2),
        retry=retry_if_exception_message(match="|".join(_TRANSIENT_ERRORS)),
        reraise=True,
        before_sleep=lambda rs: logger.warning(
            "Exec %s attempt %d, retrying: %s", pod_name, rs.attempt_number, str(rs.outcome.exception())[:120]
        ),
    )
    def _exec() -> tuple[str | None, str | None, int]:
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
