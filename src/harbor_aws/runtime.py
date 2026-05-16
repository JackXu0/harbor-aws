"""Process-wide shared state for harbor-aws.

`AdapterRuntime` caches the CloudFormation stack config, K8s API client,
NLB URL, and aiohttp session — created once per process and reused across
every AWSEnvironment instance.
"""

from __future__ import annotations

import asyncio
import socket
import ssl

import aiohttp

from harbor_aws.core import pods
from harbor_aws.core.config import ClusterConfig, create_k8s_client, load_config_from_stack


class AdapterRuntime:

    def __init__(self) -> None:
        self.cluster_config_task: asyncio.Task[ClusterConfig] | None = None
        self.session: aiohttp.ClientSession | None = None
        self.nlb_url: str | None = None

    async def get_cluster_config(
        self, stack_name: str, region: str, role_arn: str | None,
    ) -> ClusterConfig:
        if self.cluster_config_task is None:
            self.cluster_config_task = asyncio.create_task(
                self._bootstrap(stack_name, region, role_arn)
            )
        return await self.cluster_config_task

    async def _bootstrap(
        self, stack_name: str, region: str, role_arn: str | None,
    ) -> ClusterConfig:
        cluster = await load_config_from_stack(
            stack_name=stack_name, region=region, role_arn=role_arn,
        )
        k8s_api = create_k8s_client(cluster)
        await asyncio.to_thread(pods.validate_runner_configmap, k8s_api, cluster.namespace)
        self.nlb_url = await asyncio.to_thread(pods.discover_nlb_url, k8s_api, cluster.namespace)
        self.session = self._build_session(cluster.nlb_cert_pem)
        return cluster

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

    def get_nlb_url(self) -> str:
        if self.nlb_url is None:
            raise RuntimeError("AdapterRuntime.get_nlb_url() called before bootstrap")
        return self.nlb_url

    def get_session(self) -> aiohttp.ClientSession:
        if self.session is None:
            raise RuntimeError("AdapterRuntime.get_session() called before bootstrap")
        return self.session

    async def delete_pod(self, pod_name: str) -> None:
        """Tell the control pod to delete a trial pod by name."""
        cluster = await self._require_cluster_config()
        headers = {"Authorization": f"Bearer {cluster.bearer_token}"}
        async with self.get_session().post(
            f"{self.get_nlb_url()}/delete-pod",
            json={"pod_name": pod_name},
            headers=headers,
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(f"delete-pod {pod_name} failed ({resp.status}): {body}")

    async def list_pods(self) -> list[str]:
        """Ask the control pod for all running trial pod names."""
        cluster = await self._require_cluster_config()
        headers = {"Authorization": f"Bearer {cluster.bearer_token}"}
        async with self.get_session().get(
            f"{self.get_nlb_url()}/list-pods",
            headers=headers,
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(f"list-pods failed ({resp.status}): {body}")
            data = await resp.json()
            return data["pod_names"]

    async def wait_pod_running(self, pod_name: str) -> None:
        """Block until the control pod observes the trial pod as Running."""
        cluster = await self._require_cluster_config()
        headers = {"Authorization": f"Bearer {cluster.bearer_token}"}
        async with self.get_session().post(
            f"{self.get_nlb_url()}/wait-pod-running",
            json={"pod_name": pod_name},
            headers=headers,
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(f"wait-pod-running {pod_name} failed ({resp.status}): {body}")

    async def _require_cluster_config(self) -> ClusterConfig:
        if self.cluster_config_task is None:
            raise RuntimeError("AdapterRuntime: cluster config not bootstrapped")
        return await self.cluster_config_task


runtime = AdapterRuntime()
