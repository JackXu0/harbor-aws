"""Process-wide HTTPS client for the control pod.

Every HTTPS call to the control pod goes through ``ControlPodClient`` —
both stateless ops (create_pod, list_pods) and per-trial ops keyed by
trial_id (register_trial, exec, stop_trial). Per-trial state lives on
``TrialSession``, which holds it across many calls and delegates here.
"""

from __future__ import annotations

import asyncio
import base64
import os
import socket
import ssl
from typing import Any

import aiohttp

from harbor_aws.core.config import ClusterInfo


class ControlPodClient:

    def __init__(self) -> None:
        self._bootstrap_task: asyncio.Task[None] | None = None
        self.session: aiohttp.ClientSession | None = None
        self.info: ClusterInfo | None = None
        self.nlb_url: str | None = None
        self.bearer_token: str | None = None

    # ===== Bootstrap (idempotent, deduplicated for concurrent first callers) =====

    async def _ensure_bootstrapped(self) -> None:
        if self._bootstrap_task is None:
            self._bootstrap_task = asyncio.create_task(self._bootstrap())
        await self._bootstrap_task

    async def _bootstrap(self) -> None:
        self.nlb_url = os.environ["HARBOR_NLB_URL"]
        self.bearer_token = os.environ["HARBOR_BEARER_TOKEN"]
        cert_pem = base64.b64decode(os.environ["HARBOR_NLB_CERT"]).decode()
        self.session = self._build_session(cert_pem)
        self.info = await self._fetch_info()

    async def _fetch_info(self) -> ClusterInfo:
        assert self.session is not None and self.nlb_url is not None
        async with self.session.get(
            f"{self.nlb_url}/info", headers=self._auth_headers(),
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(f"/info failed ({resp.status}): {body}")
            data = await resp.json()
            return ClusterInfo(
                namespace=data["namespace"],
                account_id=data["account_id"],
                k8s_service_account=data["k8s_service_account"],
                dockerhub_cache_enabled=data["dockerhub_cache_enabled"],
            )

    @staticmethod
    def _build_session(cert_pem: str) -> aiohttp.ClientSession:
        """Shared aiohttp session with HTTPS + TCP keepalive."""
        def keepalive_socket(addr_info: tuple) -> socket.socket:  # type: ignore[type-arg]
            family, type_, proto, *_ = addr_info
            s = socket.socket(family, type_, proto)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            for name, value in (("TCP_KEEPIDLE", 60), ("TCP_KEEPINTVL", 30), ("TCP_KEEPCNT", 4)):
                const = getattr(socket, name, None)
                if const is not None:
                    s.setsockopt(socket.IPPROTO_TCP, const, value)
            return s

        ssl_ctx = ssl.create_default_context(cadata=cert_pem)
        ssl_ctx.check_hostname = False
        return aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(
                limit=0, limit_per_host=0, ssl=ssl_ctx, socket_factory=keepalive_socket,
            ),
            timeout=aiohttp.ClientTimeout(total=None, sock_connect=30),
        )

    # ===== Public accessor =====

    async def get_info(self) -> ClusterInfo:
        await self._ensure_bootstrapped()
        assert self.info is not None
        return self.info

    # ===== Pod-lifecycle HTTPS methods (stateless) =====

    async def create_pod(
        self,
        *,
        trial_id: str,
        image_uri: str,
        environment_name: str,
        cpus: int,
        memory_mb: int,
        pod_timeout_sec: int,
        service_account: str | None = None,
    ) -> str:
        """Ask the control pod to create a trial pod (rate-limited globally)."""
        await self._ensure_bootstrapped()
        body = {
            "trial_id": trial_id,
            "image_uri": image_uri,
            "environment_name": environment_name,
            "cpus": cpus,
            "memory_mb": memory_mb,
            "pod_timeout_sec": pod_timeout_sec,
            "service_account": service_account,
        }
        async with self.session.post(
            f"{self.nlb_url}/create-pod", json=body, headers=self._auth_headers(),
        ) as resp:
            payload = await resp.json()
            if resp.status != 200:
                raise RuntimeError(f"create_pod failed ({resp.status}): {payload.get('error')}")
            return payload["pod_name"]

    async def delete_pod(self, pod_name: str) -> None:
        await self._ensure_bootstrapped()
        async with self.session.post(
            f"{self.nlb_url}/delete-pod",
            json={"pod_name": pod_name},
            headers=self._auth_headers(),
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(f"delete-pod {pod_name} failed ({resp.status}): {body}")

    async def list_pods(self) -> list[str]:
        await self._ensure_bootstrapped()
        async with self.session.get(
            f"{self.nlb_url}/list-pods", headers=self._auth_headers(),
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(f"list-pods failed ({resp.status}): {body}")
            data = await resp.json()
            return data["pod_names"]

    async def wait_pod_running(self, pod_name: str) -> None:
        await self._ensure_bootstrapped()
        async with self.session.post(
            f"{self.nlb_url}/wait-pod-running",
            json={"pod_name": pod_name},
            headers=self._auth_headers(),
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(f"wait-pod-running {pod_name} failed ({resp.status}): {body}")

    # ===== Per-trial HTTPS methods (called by TrialSession, keyed by trial_id) =====

    async def register_trial(self, trial_id: str, *, connect_timeout: float = 1800.0) -> None:
        await self._ensure_bootstrapped()
        async with self.session.post(
            f"{self.nlb_url}/register",
            json={"trial_id": trial_id, "connect_timeout": connect_timeout},
            headers=self._auth_headers(),
            timeout=aiohttp.ClientTimeout(total=connect_timeout + 30),
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(f"register failed ({resp.status}): {body}")

    async def stop_trial(self, trial_id: str) -> None:
        """Tear down a trial's back-channel."""
        await self._ensure_bootstrapped()
        async with self.session.post(
            f"{self.nlb_url}/stop",
            json={"trial_id": trial_id},
            headers=self._auth_headers(),
        ) as resp:
            await resp.read()
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(f"stop failed ({resp.status}): {body}")

    async def exec(
        self,
        trial_id: str,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: int = 300,
    ) -> tuple[str, str, int]:
        """Run a command in a trial pod. Returns (stdout, stderr, rc)."""
        await self._ensure_bootstrapped()
        body: dict[str, Any] = {
            "trial_id": trial_id,
            "cmd": command,
            "cwd": cwd,
            "env": env,
            "timeout_sec": timeout_sec,
        }
        async with self.session.post(
            f"{self.nlb_url}/exec",
            json=body,
            headers=self._auth_headers(),
            timeout=aiohttp.ClientTimeout(total=timeout_sec + 30),
        ) as resp:
            if resp.status == 413:
                raise RuntimeError(
                    "control server rejected /exec body as too large (413). "
                    "Payload exceeded MAX_PAYLOAD_BYTES (see server.py). "
                    "Either reduce the upload size or raise the cap + redeploy."
                )
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"exec failed ({resp.status}): {text}")
            payload = await resp.json()
            return payload["stdout"], payload["stderr"], int(payload["rc"])

    # ===== Helpers =====

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.bearer_token}"}


control_pod = ControlPodClient()
