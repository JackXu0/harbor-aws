"""Persistent shell session via Kubernetes WebSocket exec.

One WebSocket connection per pod, reused for all commands and file transfers.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import shlex
import threading
import time
import uuid

from kubernetes import client
from kubernetes.stream import stream
from tenacity import retry, retry_if_exception_message, stop_after_attempt, wait_exponential_jitter

logger = logging.getLogger(__name__)

_TRANSIENT_ERRORS = ("Handshake status", "Unauthorized", "nodename nor servname",
                     "Name or service not known", "Connection refused",
                     "Connection reset", "timed out")

# Dedicated thread pool for WebSocket exec calls. asyncio's default executor caps
# at 32 workers, which wedges everything when 2000+ trials each have a long-running
# WebSocket round-trip (the round-trip itself is fine; the cap is the issue). Sized
# generously since each thread blocks on socket I/O and consumes negligible memory.
_SHELL_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=4096, thread_name_prefix="harbor-shell"
)


class PersistentShell:
    """A persistent bash shell in a Kubernetes pod."""

    def __init__(self, pod_name: str, namespace: str, container: str = "main"):
        self._pod_name = pod_name
        self._namespace = namespace
        self._container = container
        self._resp = None
        self._lock = asyncio.Lock()
        self._thread_lock = threading.Lock()

    @retry(
        stop=stop_after_attempt(10),
        wait=wait_exponential_jitter(initial=2, max=60, jitter=5),
        retry=retry_if_exception_message(match="|".join(_TRANSIENT_ERRORS)),
        reraise=True,
    )
    def _connect(self) -> None:
        from harbor_aws.core.config import ensure_fresh_kubeconfig
        ensure_fresh_kubeconfig()
        cfg = client.Configuration.get_default_copy()
        api = client.CoreV1Api(api_client=client.ApiClient(configuration=cfg))
        self._resp = stream(
            api.connect_get_namespaced_pod_exec,
            name=self._pod_name, namespace=self._namespace, container=self._container,
            command=["bash"], stderr=True, stdout=True, stdin=True, tty=False,
            _preload_content=False,
        )

        # Wait until bash is actually reading stdin and writing stdout. Under high
        # concurrent connect load on Fargate, stream() can return before the kubelet
        # has finished wiring up bash's pipes — so the very first write_stdin() is
        # silently dropped. Send a sentinel and wait for it to come back, so callers
        # are guaranteed a live, responsive shell.
        sentinel = f"__READY_{uuid.uuid4().hex[:12]}__"
        self._resp.write_stdin(f"echo {sentinel}\n")
        deadline = time.monotonic() + 60
        out = ""
        while sentinel not in out:
            if time.monotonic() > deadline:
                try:
                    self._resp.close()
                except Exception:
                    pass
                self._resp = None
                raise RuntimeError(
                    f"PersistentShell handshake timed out after 60s for {self._pod_name} "
                    f"(bash never responded to sentinel)"
                )
            self._resp.update(timeout=0.5)
            if self._resp.peek_stdout():
                out += self._resp.read_stdout()

    async def connect(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(_SHELL_EXECUTOR, self._connect)

    def _run_sync(
        self, command: str, cwd: str | None, env: dict[str, str] | None, timeout_sec: int,
    ) -> tuple[str, str, int]:
        """Send a command, read stdout until end marker. Stderr collected separately."""
        with self._thread_lock:
            if self._resp is None or not self._resp.is_open():
                raise RuntimeError(f"Shell not connected for {self._pod_name}")

            parts = []
            if env:
                for key, value in env.items():
                    parts.append(f"export {key}={shlex.quote(value)};")
            if cwd:
                parts.append(f"cd {shlex.quote(cwd)} &&")
            parts.append(command)
            full_cmd = " ".join(parts)

            # End marker uses underscores — cannot appear in base64 (A-Za-z0-9+/=)
            marker = uuid.uuid4().hex[:16]
            end_token = f"__HEND_{marker}__"

            # No 2>&1 — stderr stays in its own WebSocket channel
            self._resp.write_stdin(f"{full_cmd} ; echo \"{end_token} $?\"\n")

            stdout_buf = ""
            stderr_buf = ""
            deadline = time.monotonic() + timeout_sec

            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return ("", f"Command timed out after {timeout_sec}s", 124)

                self._resp.update(timeout=min(remaining, 1))

                if self._resp.peek_stdout():
                    stdout_buf += self._resp.read_stdout()
                if self._resp.peek_stderr():
                    stderr_buf += self._resp.read_stderr()

                if end_token in stdout_buf:
                    lines = stdout_buf.split("\n")
                    output = []
                    rc = 1
                    for line in lines:
                        if line.startswith(end_token):
                            try:
                                rc = int(line.split()[-1])
                            except (ValueError, IndexError):
                                rc = 1
                            break
                        output.append(line)
                    return ("\n".join(output), stderr_buf, rc)

    async def run(
        self, command: str, cwd: str | None = None,
        env: dict[str, str] | None = None, timeout_sec: int = 300,
    ) -> tuple[str, str, int]:
        """Run a command. Returns (stdout, stderr, return_code)."""
        async with self._lock:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                _SHELL_EXECUTOR, self._run_sync, command, cwd, env, timeout_sec,
            )

    async def close(self) -> None:
        if self._resp:
            try:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(_SHELL_EXECUTOR, self._resp.close)
            except Exception:
                pass
            self._resp = None
