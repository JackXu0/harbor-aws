"""Per-trial handle over the runtime's HTTPS client.

Bundles ``trial_id`` + ``trial_token`` so callers don't pass them on every
``run`` / ``upload`` / ``download``. All HTTPS work delegates to the control pod client.
"""

from __future__ import annotations

import base64
import io
import logging
import tarfile
from pathlib import Path
from shlex import quote as _q
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from harbor_aws.control_pod_client import ControlPodClient

logger = logging.getLogger(__name__)


class TrialSession:

    def __init__(self, trial_id: str, trial_token: str, control_pod: ControlPodClient) -> None:
        self._trial_id = trial_id
        self._trial_token = trial_token
        self._control_pod = control_pod
        self._closed = False

    # ===== Lifecycle =====

    async def connect(self, connect_timeout: float = 1800.0) -> None:
        """Pre-register the trial; blocks until the runner dials in."""
        await self._control_pod.register_trial(
            self._trial_id, self._trial_token, connect_timeout=connect_timeout,
        )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._control_pod.stop_trial(self._trial_id)
        except Exception:
            logger.warning("TrialSession.close() failed for trial %s", self._trial_id, exc_info=True)

    # ===== Command execution =====

    async def run(
        self,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: int = 300,
    ) -> tuple[str, str, int]:
        if self._closed:
            raise RuntimeError("TrialSession is closed")
        return await self._control_pod.exec(
            self._trial_id, command, cwd=cwd, env=env, timeout_sec=timeout_sec,
        )

    # ===== File transfer =====
    # tar+base64 over a single run() call. The control pod caps request
    # bodies at MAX_PAYLOAD_BYTES (see server.py); base64 inflates 4/3,
    # so usable payload per call is ~3/4 of that.

    @staticmethod
    def _tar_b64(entries: list[tuple[Path, str]]) -> str:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for path, arcname in entries:
                tar.add(str(path), arcname=arcname)
        return base64.b64encode(buf.getvalue()).decode("ascii")

    async def upload_file(self, local_path: str | Path, remote_path: str) -> None:
        local = Path(local_path)
        if not local.exists():
            raise FileNotFoundError(local)
        remote = Path(remote_path)
        b64 = self._tar_b64([(local, remote.name)])
        cmd = (
            f"mkdir -p {_q(str(remote.parent))} && "
            f"echo {_q(b64)} | base64 -d | tar xzf - -C {_q(str(remote.parent))}"
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
        # Prefer GNU base64 -w0 (no wrapping); BSD base64 doesn't support it.
        cmd = (
            f"tar czf - -C {_q(str(src.parent))} {_q(src.name)} | base64 -w0 2>/dev/null"
            f" || tar czf - -C {_q(str(src.parent))} {_q(src.name)} | base64"
        )
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
        cmd = (
            f"tar czf - -C {_q(remote_dir)} . | base64 -w0 2>/dev/null"
            f" || tar czf - -C {_q(remote_dir)} . | base64"
        )
        out, err, rc = await self.run(cmd, timeout_sec=300)
        if rc != 0 or not out.strip():
            raise RuntimeError(f"download_dir({remote_dir}) failed: rc={rc} err={err[:200]}")
        data = base64.b64decode(out)
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tar:
            tar.extractall(path=str(local), filter="data")
