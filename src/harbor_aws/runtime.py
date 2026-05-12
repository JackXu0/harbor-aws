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
from kubernetes import client

from harbor_aws.core import pods
from harbor_aws.core.config import ClusterConfig, create_k8s_client, load_config_from_stack


class AdapterRuntime:
    """Process-wide cache shared across all AWSEnvironment instances.

    Lazily resolves the CloudFormation stack config, K8s API client, control plane
    NLB URL + bearer token, and aiohttp session on first use. All getters are idempotent.
    """

    def __init__(self) -> None:
        self.cluster_config_task: asyncio.Task[ClusterConfig] | None = None
        self.k8s_api: client.CoreV1Api | None = None
        self.session: aiohttp.ClientSession | None = None
        self.nlb_url: str | None = None
        self.create_semaphore = asyncio.Semaphore(100)

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
        self.k8s_api = create_k8s_client(cluster)
        await asyncio.to_thread(pods.validate_runner_configmap, self.k8s_api, cluster.namespace)
        self.nlb_url = await asyncio.to_thread(pods.discover_nlb_url, self.k8s_api, cluster.namespace)

        # TCP keepalive so AWS NLB's ~350s idle timeout doesn't drop long /exec calls.
        def keepalive_socket(addr_info: tuple) -> socket.socket:  # type: ignore[type-arg]
            family, type_, proto, *_ = addr_info
            s = socket.socket(family, type_, proto)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            for name, value in (("TCP_KEEPIDLE", 60), ("TCP_KEEPINTVL", 30), ("TCP_KEEPCNT", 4)):
                const = getattr(socket, name, None)
                if const is not None:
                    s.setsockopt(socket.IPPROTO_TCP, const, value)
            return s

        # For HTTPS
        ssl_ctx = ssl.create_default_context(cadata=cluster.nlb_cert_pem)
        ssl_ctx.check_hostname = False
        connector = aiohttp.TCPConnector(
            limit=0,
            limit_per_host=0,
            ssl=ssl_ctx,
            socket_factory=keepalive_socket,
        )
        self.session = aiohttp.ClientSession(connector=connector)
        return cluster

    def get_k8s_client(self, cluster: ClusterConfig) -> client.CoreV1Api:
        if self.k8s_api is None:
            self.k8s_api = create_k8s_client(cluster)
        return self.k8s_api

    def get_nlb_url(self) -> str:
        if self.nlb_url is None:
            raise RuntimeError("AdapterRuntime.get_nlb_url() called before bootstrap")
        return self.nlb_url

    def get_session(self) -> aiohttp.ClientSession:
        if self.session is None:
            raise RuntimeError("AdapterRuntime.get_session() called before bootstrap")
        return self.session


runtime = AdapterRuntime()
