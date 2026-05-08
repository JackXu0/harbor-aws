"""RemoteShell — talks to one trial pod via the control pod (via NLB).

The adapter holds one RemoteShell per trial. All command execution and file
transfer go through this object, which forwards everything to the in-cluster
control pod over plain HTTPS. The control pod then talks to the
trial pod over direct in-VPC TCP. The K8s apiserver is not in the data path.

Public interface:
    await shell.connect()
    out, err, rc = await shell.run(cmd, cwd=..., env=..., timeout_sec=...)
    await shell.upload_file(local_path, remote_path)
    await shell.upload_dir(local_dir, remote_dir)
    await shell.download_file(remote_path, local_path)
    await shell.download_dir(remote_dir, local_dir)
    await shell.close()
"""

from __future__ import annotations

import base64
import io
import tarfile
from pathlib import Path
from typing import Any

import aiohttp


class RemoteShell:
    """Talks to one trial pod via the control pod (via NLB)."""

    def __init__(
        self,
        trial_id: str,
        token: str,
        nlb_url: str,
        bearer_token: str,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self._trial_id = trial_id
        self._token = token
        self._nlb_url = nlb_url.rstrip("/")
        self._bearer_token = bearer_token
        self._session = session
        self._owns_session = session is None
        self._closed = False

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            connector = aiohttp.TCPConnector(limit=0, limit_per_host=0)
            self._session = aiohttp.ClientSession(connector=connector)
        return self._session

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._bearer_token}"}

    # --- lifecycle ---

    async def connect(self, connect_timeout: float = 600.0) -> None:
        """Pre-register the trial with the control server.

        With the reverse-runner architecture this call returns once the runner
        pod has dialed in to the control server and authenticated. The control
        server will block up to ``connect_timeout`` seconds waiting for that.

        Default 600 s gives heavy ML base images (PyTorch, HuggingFace, etc.)
        room to pull from Docker Hub on cold caches — observed pulls in the
        wild can take 4-7 minutes.
        """
        s = await self._ensure_session()
        async with s.post(
            f"{self._nlb_url}/register",
            json={
                "trial_id": self._trial_id,
                "token": self._token,
                "connect_timeout": connect_timeout,
            },
            headers=self._headers,
            # Give the HTTP request itself a bit more than connect_timeout so
            # we always see the server's structured 504 instead of an aiohttp
            # client-side timeout.
            timeout=aiohttp.ClientTimeout(total=connect_timeout + 30),
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(f"control server register failed ({resp.status}): {body}")

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            s = await self._ensure_session()
            async with s.post(
                f"{self._nlb_url}/stop",
                json={"trial_id": self._trial_id},
                headers=self._headers,
            ) as resp:
                await resp.read()
        except Exception:  # noqa: BLE001
            pass
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None

    # --- command execution ---

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
            f"{self._nlb_url}/exec", json=body, headers=self._headers
        ) as resp:
            payload = await resp.json()
            if resp.status != 200:
                raise RuntimeError(
                    f"control server exec failed ({resp.status}): {payload}"
                )
            return (
                payload.get("stdout", ""),
                payload.get("stderr", ""),
                int(payload.get("rc", 1)),
            )

    # --- file transfer ---
    #
    # Both directions use tar+base64 over a single run() call. The runner's
    # message frame is capped at 64 MB; base64 inflates 4/3, so we can transfer
    # roughly 48 MB of payload per call. For larger transfers we'd need to
    # chunk, which we don't currently need for any harbor workload.

    @staticmethod
    def _tar_b64(entries: list[tuple[Path, str]]) -> str:
        """Build a gzip+base64 tar containing ``entries`` (path, arcname pairs)."""
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for path, arcname in entries:
                tar.add(str(path), arcname=arcname)
        return base64.b64encode(buf.getvalue()).decode("ascii")

    async def upload_file(self, local_path: str | Path, remote_path: str) -> None:
        local = Path(local_path)
        if not local.exists():
            raise FileNotFoundError(local)
        b64 = self._tar_b64([(local, local.name)])
        # Extract into the parent dir of remote_path then mv to the final name,
        # since the tar contains a single entry named local.name.
        remote = Path(remote_path)
        cmd = (
            f"mkdir -p {_q(str(remote.parent))} && "
            f"echo {_q(b64)} | base64 -d | tar xzf - -C {_q(str(remote.parent))} && "
            f"mv {_q(str(remote.parent / local.name))} {_q(remote_path)}"
        )
        _, err, rc = await self.run(cmd, timeout_sec=300)
        if rc != 0:
            raise RuntimeError(f"upload_file({local} -> {remote_path}) failed: rc={rc} err={err}")

    async def upload_dir(self, local_dir: str | Path, remote_dir: str) -> None:
        local = Path(local_dir)
        if not local.is_dir():
            raise NotADirectoryError(local)
        b64 = self._tar_b64([(entry, entry.name) for entry in local.iterdir()])
        cmd = (
            f"mkdir -p {_q(remote_dir)} && "
            f"echo {_q(b64)} | base64 -d | tar xzf - -C {_q(remote_dir)}"
        )
        _, err, rc = await self.run(cmd, timeout_sec=300)
        if rc != 0:
            raise RuntimeError(f"upload_dir({local} -> {remote_dir}) failed: rc={rc} err={err}")

    async def download_file(self, remote_path: str, local_path: str | Path) -> None:
        local = Path(local_path)
        local.parent.mkdir(parents=True, exist_ok=True)
        src = Path(remote_path)
        cmd = f"tar czf - -C {_q(str(src.parent))} {_q(src.name)} | base64 -w0 2>/dev/null || tar czf - -C {_q(str(src.parent))} {_q(src.name)} | base64"
        out, err, rc = await self.run(cmd, timeout_sec=300)
        if rc != 0 or not out.strip():
            raise RuntimeError(f"download_file({remote_path}) failed: rc={rc} err={err[:200]}")
        data = base64.b64decode(out)
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tar:
            members = tar.getmembers()
            if not members:
                raise RuntimeError(f"download_file({remote_path}): empty tar")
            extracted = tar.extractfile(members[0])
            if extracted is None:
                raise RuntimeError(f"download_file({remote_path}): not a regular file")
            local.write_bytes(extracted.read())

    async def download_dir(self, remote_dir: str, local_dir: str | Path) -> None:
        local = Path(local_dir)
        local.mkdir(parents=True, exist_ok=True)
        cmd = f"tar czf - -C {_q(remote_dir)} . | base64 -w0 2>/dev/null || tar czf - -C {_q(remote_dir)} . | base64"
        out, err, rc = await self.run(cmd, timeout_sec=300)
        if rc != 0 or not out.strip():
            raise RuntimeError(f"download_dir({remote_dir}) failed: rc={rc} err={err[:200]}")
        data = base64.b64decode(out)
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tar:
            tar.extractall(path=str(local), filter="data")


def _q(s: str) -> str:
    """Single-quote shell-escape."""
    return "'" + s.replace("'", "'\\''") + "'"
