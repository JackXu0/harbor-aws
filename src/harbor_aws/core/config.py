"""AWS config, Kubernetes client, and CloudFormation stack loader."""

from __future__ import annotations

import asyncio
import logging
import subprocess
import threading
import time
from dataclasses import dataclass

import boto3
from kubernetes import client
from kubernetes import config as k8s_config

logger = logging.getLogger(__name__)

# EKS tokens expire after 15 minutes; refresh every 10 minutes
_TOKEN_REFRESH_INTERVAL = 600
_kubeconfig_lock = threading.Lock()
_kubeconfig_loaded_at: float = 0


def ensure_fresh_kubeconfig() -> None:
    """Reload kubeconfig if the EKS token is stale. Thread-safe."""
    global _kubeconfig_loaded_at
    with _kubeconfig_lock:
        if time.monotonic() - _kubeconfig_loaded_at > _TOKEN_REFRESH_INTERVAL:
            k8s_config.load_kube_config()
            _kubeconfig_loaded_at = time.monotonic()
            logger.debug("Refreshed kubeconfig token")


@dataclass
class AWSConfig:
    """AWS-specific configuration for the EKS/Fargate backend."""

    # AWS credentials
    region: str = "us-east-1"
    profile_name: str | None = None

    # EKS
    eks_cluster_name: str = "harbor-aws"
    namespace: str = "harbor"
    service_account_name: str = "harbor-pod"

    # AWS account (needed for ECR pull-through cache URI)
    account_id: str | None = None

    # ECR pull-through cache (opt-in, requires setup — see README)
    ecr_cache: bool = False

    # Stack-based configuration (alternative to individual fields)
    stack_name: str | None = None

    def validate(self) -> None:
        """Validate that required fields are set."""
        if not self.eks_cluster_name:
            raise ValueError(
                "Missing required AWS config field: eks_cluster_name. "
                "Set it directly or use stack_name to read from CloudFormation outputs."
            )


_kubeconfig_setup = False


def create_k8s_client(config: AWSConfig) -> client.CoreV1Api:
    """Create a Kubernetes CoreV1Api client for the EKS cluster.

    First call runs 'aws eks update-kubeconfig'. Returns a standard CoreV1Api.
    Call ensure_fresh_kubeconfig() before making API calls if tokens may be stale.
    """
    global _kubeconfig_setup

    with _kubeconfig_lock:
        if not _kubeconfig_setup:
            cmd = [
                "aws", "eks", "update-kubeconfig",
                "--name", config.eks_cluster_name,
                "--region", config.region,
            ]
            if config.profile_name:
                cmd += ["--profile", config.profile_name]

            subprocess.run(cmd, check=True, capture_output=True, text=True)
            _kubeconfig_setup = True

    ensure_fresh_kubeconfig()
    return client.CoreV1Api()


async def load_config_from_stack(
    stack_name: str,
    region: str = "us-east-1",
    profile_name: str | None = None,
) -> AWSConfig:
    """Load AWSConfig from CloudFormation stack outputs."""

    def _read_outputs() -> tuple[dict[str, str], str]:
        session = boto3.Session(profile_name=profile_name, region_name=region)
        cfn = session.client("cloudformation")
        response = cfn.describe_stacks(StackName=stack_name)

        stacks = response.get("Stacks", [])
        if not stacks:
            raise RuntimeError(
                f"Stack '{stack_name}' not found. "
                f"Deploy with: python -m harbor_aws deploy --stack-name {stack_name} --region {region}"
            )

        stack = stacks[0]
        if stack["StackStatus"] not in ("CREATE_COMPLETE", "UPDATE_COMPLETE", "UPDATE_ROLLBACK_COMPLETE"):
            raise RuntimeError(f"Stack '{stack_name}' is in status {stack['StackStatus']}")

        outputs = {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}
        account_id = session.client("sts").get_caller_identity()["Account"]
        return outputs, account_id

    outputs, account_id = await asyncio.to_thread(_read_outputs)
    logger.debug("Loaded %d outputs from stack '%s'", len(outputs), stack_name)

    config = AWSConfig(
        region=region,
        profile_name=profile_name,
        stack_name=stack_name,
        eks_cluster_name=outputs.get("EksClusterName", "harbor-aws"),
        namespace=outputs.get("Namespace", "harbor"),
        account_id=account_id,
    )

    config.validate()
    return config
