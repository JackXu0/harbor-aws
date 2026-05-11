"""AWS config, Kubernetes client, and CloudFormation stack loader."""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import threading
from dataclasses import dataclass

import boto3
from kubernetes import client
from kubernetes import config as k8s_config

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ClusterConfig:
    """Process-wide cluster configuration loaded from a CloudFormation stack.

    All fields are shared across every trial in the process. Construct via
    ``load_config_from_stack()``; do not instantiate directly.
    """

    # AWS credentials
    region: str = "us-east-1"
    role_arn: str | None = None  # Set for cross-account; None for same account

    # EKS
    eks_cluster_name: str = "harbor-aws"
    namespace: str = "harbor"
    k8s_service_account: str | None = None

    # AWS account (needed for ECR pull-through cache URI)
    account_id: str | None = None

    # Stack-based configuration
    stack_name: str | None = None

    def validate(self) -> None:
        """Validate that required fields are set."""
        if not self.eks_cluster_name:
            raise ValueError(
                "Missing required cluster config field: eks_cluster_name. "
                "Use stack_name to read from CloudFormation outputs."
            )

    def create_boto3_session(self) -> boto3.Session:
        """Create a boto3 session, assuming role_arn if provided (cross-account)"""
        if not self.role_arn:
            return boto3.Session(region_name=self.region)

        sts = boto3.client("sts", region_name=self.region)
        
        try:
            caller_arn = sts.get_caller_identity()["Arn"]
            target_account = self.role_arn.split(":")[4]
            target_role_name = self.role_arn.rsplit("/", 1)[-1]
            if (f"::{target_account}:" in caller_arn
                    and f":assumed-role/{target_role_name}/" in caller_arn):
                logger.debug("Already in target role %s, skipping AssumeRole", self.role_arn)
                return boto3.Session(region_name=self.region)
        except Exception:
            logger.debug("Caller identity check failed; proceeding with AssumeRole", exc_info=True)

        creds = sts.assume_role(RoleArn=self.role_arn, RoleSessionName="harbor-aws")["Credentials"]
        return boto3.Session(
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
            region_name=self.region,
        )

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


@dataclass(frozen=True)
class TrialOptions:
    """Per-trial overrides set by the adapter caller (kwargs to ``AWSEnvironment``)."""

    # ECR pull-through cache (opt-in, requires setup — see README)
    ecr_cache: bool = False

    # Maximum pod lifetime in seconds (default: 4 hours)
    pod_timeout_sec: int = 14400

    # If True, attach the cluster's pod service account so the pod can call Bedrock.
    use_bedrock: bool = False


_kubeconfig_setup_lock = threading.Lock()
_kubeconfig_setup_done = False


def create_k8s_client(config: ClusterConfig) -> client.CoreV1Api:
    global _kubeconfig_setup_done

    with _kubeconfig_setup_lock:
        if not _kubeconfig_setup_done:
            cmd = ["aws", "eks", "update-kubeconfig",
                   "--name", config.eks_cluster_name, "--region", config.region]
            if config.role_arn:
                cmd += ["--role-arn", config.role_arn]
            subprocess.run(cmd, check=True, capture_output=True, text=True, env=config._cli_env())
            k8s_config.load_kube_config()
            _kubeconfig_setup_done = True

    cfg = client.Configuration.get_default_copy()
    cfg.connection_pool_maxsize = 500
    return client.CoreV1Api(api_client=client.ApiClient(configuration=cfg))


async def load_config_from_stack(
    stack_name: str,
    region: str = "us-east-1",
    role_arn: str | None = None,
) -> ClusterConfig:
    """Load ClusterConfig from CloudFormation stack outputs."""

    tmp = ClusterConfig(region=region, role_arn=role_arn)

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

    config = ClusterConfig(
        region=region,
        role_arn=role_arn,
        stack_name=stack_name,
        eks_cluster_name=_required(outputs, "EksClusterName", stack_name),
        namespace=_required(outputs, "Namespace", stack_name),
        k8s_service_account=outputs.get("PodServiceAccount"),  # optional — only used when bedrock=True
        account_id=account_id,
    )

    config.validate()
    return config


def _required(outputs: dict[str, str], key: str, stack_name: str) -> str:
    """Read a CloudFormation output that must exist on a healthy deploy."""
    if key not in outputs:
        raise RuntimeError(
            f"Stack '{stack_name}' is missing required output '{key}'. "
            f"Redeploy with the current harbor-aws CDK: harbor-aws deploy --stack-name {stack_name}"
        )
    return outputs[key]
