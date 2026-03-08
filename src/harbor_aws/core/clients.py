"""Singleton manager for AWS boto3 clients."""

from __future__ import annotations

import asyncio
import atexit
import logging
import sys
from dataclasses import dataclass

import boto3

from harbor_aws.core.config import AWSConfig

logger = logging.getLogger(__name__)


@dataclass
class AWSClients:
    """Container for boto3 clients."""

    ecs: object
    ecr: object
    s3: object
    codebuild: object
    cloudformation: object


class ECSClientManager:
    """Singleton manager for AWS ECS/ECR/CodeBuild/S3 clients.

    Ensures a single shared set of boto3 clients across all AWSEnvironment
    instances. boto3 clients are thread-safe and can be shared. Each client
    gets its own dedicated session (sessions are NOT thread-safe).

    Follows the KubernetesClientManager pattern from Harbor's GKE environment.
    """

    _instance: ECSClientManager | None = None
    _lock = asyncio.Lock()

    def __init__(self) -> None:
        self._clients: AWSClients | None = None
        self._reference_count = 0
        self._client_lock = asyncio.Lock()
        self._initialized = False
        self._cleanup_registered = False
        self._config_key: str | None = None

    @classmethod
    async def get_instance(cls) -> ECSClientManager:
        """Get or create the singleton instance."""
        if cls._instance is None:
            async with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def _create_client(self, service: str, config: AWSConfig) -> object:
        """Create a boto3 client with a fresh session."""
        if config.profile_name:
            session = boto3.Session(profile_name=config.profile_name, region_name=config.region)
        else:
            session = boto3.Session(region_name=config.region)
        return session.client(service)

    def _init_clients(self, config: AWSConfig) -> None:
        """Initialize all boto3 clients."""
        if self._initialized:
            return

        self._clients = AWSClients(
            ecs=self._create_client("ecs", config),
            ecr=self._create_client("ecr", config),
            s3=self._create_client("s3", config),
            codebuild=self._create_client("codebuild", config),
            cloudformation=self._create_client("cloudformation", config),
        )
        self._initialized = True
        self._config_key = f"{config.region}:{config.cluster_name}:{config.profile_name}"

    async def get_clients(self, config: AWSConfig) -> AWSClients:
        """Get shared boto3 clients, creating them if necessary.

        Increments reference count. Call release_clients() when done.
        """
        async with self._client_lock:
            if not self._initialized:
                logger.debug("Creating new AWS clients")
                await asyncio.to_thread(self._init_clients, config)

                if not self._cleanup_registered:
                    atexit.register(self._cleanup_sync)
                    self._cleanup_registered = True
            else:
                new_key = f"{config.region}:{config.cluster_name}:{config.profile_name}"
                if self._config_key != new_key:
                    raise ValueError(
                        f"ECSClientManager already initialized for {self._config_key}. "
                        f"Cannot connect with config {new_key}. "
                        f"Use separate processes for different clusters."
                    )

            self._reference_count += 1
            logger.debug("AWS client reference count incremented to %d", self._reference_count)
            assert self._clients is not None
            return self._clients

    async def release_clients(self) -> None:
        """Decrement the reference count. Cleanup happens at exit."""
        async with self._client_lock:
            if self._reference_count > 0:
                self._reference_count -= 1
                logger.debug("AWS client reference count decremented to %d", self._reference_count)

    def _cleanup_sync(self) -> None:
        """Synchronous cleanup wrapper for atexit."""
        try:
            asyncio.run(self._cleanup())
        except Exception as e:
            print(f"Error during AWS client cleanup: {e}", file=sys.stderr)

    async def _cleanup(self) -> None:
        """Clean up clients."""
        async with self._client_lock:
            if self._initialized:
                try:
                    logger.debug("Cleaning up AWS clients at program exit")
                    self._clients = None
                    self._initialized = False
                except Exception as e:
                    logger.error("Error cleaning up AWS clients: %s", e)
