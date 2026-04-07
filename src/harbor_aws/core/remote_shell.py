"""RemoteShell — interface-compatible wrapper around a control-server SessionHandle.

The point of this wrapper is to keep the adapter agnostic about which transport
it uses. Today the adapter (on the persistent-shell-v4 branch) holds a
PersistentShell. With Layer 3, it instead holds a RemoteShell, but the call
sites — connect(), run(), close() — are identical.

So the adapter integration is a one-line swap:

    # before (Layer 2 / v4):
    self._shell = PersistentShell(self._pod_name, self._aws_config.namespace)
    await self._shell.connect()

    # after (Layer 3):
    self._shell = RemoteShell(handle)
    await self._shell.connect()  # waits for the pod to register

Everything else (exec, download_dir, stop) keeps working unchanged because the
shapes match.
"""

from __future__ import annotations

import asyncio

from harbor_aws.core.control_server import SessionHandle


class RemoteShell:
    """Drop-in replacement for PersistentShell that flows commands through the
    pod-initiated outbound channel instead of a kubectl-exec WebSocket.
    """

    def __init__(self, handle: SessionHandle, register_timeout_sec: float = 600.0):
        self._handle = handle
        self._register_timeout_sec = register_timeout_sec
        self._closed = False

    async def connect(self) -> None:
        """Wait for the pod runner to call POST /register on the control server.

        This replaces opening a WebSocket exec — the "connection" is established
        when the pod itself dials the control server. The slow apiserver -> kubelet
        TLS dial is bypassed entirely.
        """
        await self._handle.wait_for_register(timeout=self._register_timeout_sec)

    async def run(
        self,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: int = 300,
    ) -> tuple[str, str, int]:
        """Run a command in the pod's bash subprocess. Returns (stdout, stderr, return_code)."""
        if self._closed:
            raise RuntimeError("RemoteShell is closed")
        return await self._handle.exec(
            command, cwd=cwd, env=env, timeout_sec=timeout_sec
        )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._handle.close()
