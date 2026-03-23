"""AWS config, Kubernetes client, and CloudFormation stack loader."""

from __future__ import annotations

import asyncio
import logging
import os
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
    role_arn: str | None = None  # Set for cross-account; None for same account

    # EKS
    eks_cluster_name: str = "harbor-aws"
    namespace: str = "harbor"
    k8s_service_account: str | None = None

    # AWS account (needed for ECR pull-through cache URI)
    account_id: str | None = None

    # ECR pull-through cache (opt-in, requires setup — see README)
    ecr_cache: bool = False

    # Stack-based configuration
    stack_name: str | None = None

    def validate(self) -> None:
        """Validate that required fields are set."""
        if not self.eks_cluster_name:
            raise ValueError(
                "Missing required AWS config field: eks_cluster_name. "
                "Use stack_name to read from CloudFormation outputs."
            )

    def create_boto3_session(self) -> boto3.Session:
        """Create a boto3 session, assuming role_arn if provided (cross-account)."""
        if self.role_arn:
            sts = boto3.client("sts", region_name=self.region)
            creds = sts.assume_role(RoleArn=self.role_arn, RoleSessionName="harbor-aws")["Credentials"]
            return boto3.Session(
                aws_access_key_id=creds["AccessKeyId"],
                aws_secret_access_key=creds["SecretAccessKey"],
                aws_session_token=creds["SessionToken"],
                region_name=self.region,
            )
        return boto3.Session(region_name=self.region)

    def _cli_env(self) -> dict[str, str] | None:
        """Environment variables for running AWS CLI with cross-account credentials."""
        if not self.role_arn:
            return None
        session = self.create_boto3_session()
        creds = session.get_credentials().get_frozen_credentials()
        env = {**os.environ, "AWS_ACCESS_KEY_ID": creds.access_key,
               "AWS_SECRET_ACCESS_KEY": creds.secret_key}
        if creds.token:
            env["AWS_SESSION_TOKEN"] = creds.token
        return env


_kubeconfig_setup = False


def create_k8s_client(config: AWSConfig) -> client.CoreV1Api:
    """Create a Kubernetes CoreV1Api client for the EKS cluster."""
    global _kubeconfig_setup

    with _kubeconfig_lock:
        if not _kubeconfig_setup:
            cmd = ["aws", "eks", "update-kubeconfig",
                   "--name", config.eks_cluster_name, "--region", config.region]
            if config.role_arn:
                cmd += ["--role-arn", config.role_arn]
            subprocess.run(cmd, check=True, capture_output=True, text=True, env=config._cli_env())
            _kubeconfig_setup = True

    ensure_fresh_kubeconfig()
    return client.CoreV1Api()


async def load_config_from_stack(
    stack_name: str,
    region: str = "us-east-1",
    role_arn: str | None = None,
) -> AWSConfig:
    """Load AWSConfig from CloudFormation stack outputs."""

    tmp = AWSConfig(region=region, role_arn=role_arn)

    def _read_outputs() -> tuple[dict[str, str], str]:
        session = tmp.create_boto3_session()
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
        role_arn=role_arn,
        stack_name=stack_name,
        eks_cluster_name=outputs.get("EksClusterName", "harbor-aws"),
        namespace=outputs.get("Namespace", "harbor"),
        k8s_service_account=outputs.get("PodServiceAccount"),
        account_id=account_id,
    )

    config.validate()
    return config
