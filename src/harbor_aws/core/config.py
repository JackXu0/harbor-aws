"""Configuration for AWS EKS/Fargate backend."""

from __future__ import annotations

import asyncio
import logging
import subprocess
from dataclasses import dataclass, field

import boto3

logger = logging.getLogger(__name__)


@dataclass
class AWSConfig:
    """AWS-specific configuration for the EKS/Fargate backend."""

    # AWS credentials
    region: str = "us-east-1"
    profile_name: str | None = None

    # EKS
    eks_cluster_name: str = "harbor-aws"
    namespace: str = "harbor"

    # Stack-based configuration (alternative to individual fields)
    stack_name: str | None = None

    def validate(self) -> None:
        """Validate that required fields are set."""
        if not self.eks_cluster_name:
            raise ValueError(
                "Missing required AWS config field: eks_cluster_name. "
                "Set it directly or use stack_name to read from CloudFormation outputs."
            )


def create_k8s_client(config: AWSConfig) -> object:
    """Create a Kubernetes CoreV1Api client configured for the EKS cluster.

    Updates kubeconfig via AWS CLI, then loads it with the kubernetes library.
    """
    from kubernetes import client, config as k8s_config

    # Update kubeconfig for the EKS cluster
    cmd = [
        "aws", "eks", "update-kubeconfig",
        "--name", config.eks_cluster_name,
        "--region", config.region,
    ]
    if config.profile_name:
        cmd += ["--profile", config.profile_name]

    subprocess.run(cmd, check=True, capture_output=True, text=True)

    k8s_config.load_kube_config()
    return client.CoreV1Api()


async def load_config_from_stack(
    stack_name: str,
    region: str = "us-east-1",
    profile_name: str | None = None,
) -> AWSConfig:
    """Load AWSConfig from CloudFormation stack outputs."""

    def _read_outputs() -> dict[str, str]:
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

        return {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}

    outputs = await asyncio.to_thread(_read_outputs)
    logger.debug("Loaded %d outputs from stack '%s'", len(outputs), stack_name)

    config = AWSConfig(
        region=region,
        profile_name=profile_name,
        stack_name=stack_name,
        eks_cluster_name=outputs.get("EksClusterName", "harbor-aws"),
        namespace=outputs.get("Namespace", "harbor"),
    )

    config.validate()
    return config
