"""RemoteShell — interface-compatible wrapper that talks to the control server.

Drop-in replacement for PersistentShell. The adapter creates one RemoteShell
per trial; calls go over plain HTTPS to the harbor-control pod, which forwards
to the trial pod via in-VPC TCP. The apiserver is never in the data path.

Same connect/run/close interface as PersistentShell so the adapter is a
one-line swap.
"""

from __future__ import annotations

from typing import Any

import aiohttp


class RemoteShell:
    """Talks to one trial pod via the harbor-control gateway."""

    def __init__(
        self,
        trial_id: str,
        pod_ip: str,
        token: str,
        control_url: str,
        admin_token: str,
        pod_port: int = 8765,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self._trial_id = trial_id
        self._pod_ip = pod_ip
        self._token = token
        self._control_url = control_url.rstrip("/")
        self._admin_token = admin_token
        self._pod_port = pod_port
        self._session = session
        self._owns_session = session is None
        self._closed = False

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            # Generous connection pool — one trial per RemoteShell, but harbor
            # creates many of these in parallel and they share a host.
            connector = aiohttp.TCPConnector(limit=0, limit_per_host=0)
            self._session = aiohttp.ClientSession(connector=connector)
        return self._session

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._admin_token}"}

    async def connect(self) -> None:
        """Tell the control server to dial the runner pod and authenticate."""
        s = await self._ensure_session()
        async with s.post(
            f"{self._control_url}/register",
            json={
                "trial_id": self._trial_id,
                "pod_ip": self._pod_ip,
                "pod_port": self._pod_port,
                "token": self._token,
            },
            headers=self._headers,
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(f"control server register failed ({resp.status}): {body}")

    async def run(
        self,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: int = 300,
    ) -> tuple[str, str, int]:
        if self._closed:
            raise RuntimeError("RemoteShell is closed")
        s = await self._ensure_session()
        body: dict[str, Any] = {
            "trial_id": self._trial_id,
            "cmd": command,
            "cwd": cwd,
            "env": env,
            "timeout_sec": timeout_sec,
        }
        async with s.post(
            f"{self._control_url}/exec", json=body, headers=self._headers
        ) as resp:
            payload = await resp.json()
            if resp.status != 200:
                raise RuntimeError(
                    f"control server exec failed ({resp.status}): {payload}"
                )
            return (payload.get("stdout", ""), payload.get("stderr", ""), int(payload.get("rc", 1)))

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            s = await self._ensure_session()
            async with s.post(
                f"{self._control_url}/stop",
                json={"trial_id": self._trial_id},
                headers=self._headers,
            ) as resp:
                await resp.read()
        except Exception:  # noqa: BLE001
            pass
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None
