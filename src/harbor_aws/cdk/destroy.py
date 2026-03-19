"""Destroy harbor-aws infrastructure and clean up resources.

Handles the quirks of EKS teardown: Fargate profiles must be deleted before
the cluster, ECR pull-through cache repos aren't managed by CloudFormation,
and VPC subnets can get stuck due to external ENIs (e.g. GuardDuty).
"""

from __future__ import annotations

import logging
import time
from typing import Any

import boto3

logger = logging.getLogger(__name__)


def destroy(stack_name: str, region: str, profile_name: str | None = None) -> None:
    """Delete the harbor-aws stack and all associated resources."""
    session = boto3.Session(profile_name=profile_name, region_name=region)
    cfn = session.client("cloudformation")
    outputs = _get_stack_outputs(cfn, stack_name)

    # Pre-cleanup: resources that block or aren't managed by CloudFormation
    cluster_name = outputs.get("EksClusterName")
    if cluster_name:
        _delete_fargate_profiles(session, cluster_name)

    ecr_repos = _list_ecr_cache_repos(session)
    if ecr_repos:
        _delete_ecr_cache_repos(session, ecr_repos)

    # Delete the stack (with retry for stuck resources)
    print("Deleting CloudFormation stack (this may take 10-15 minutes for EKS)...")
    _delete_stack(cfn, stack_name)

    print("All resources cleaned up.")


def get_destroy_summary(stack_name: str, region: str, profile_name: str | None = None) -> dict[str, object]:
    """Return info about what will be destroyed, for confirmation prompts."""
    session = boto3.Session(profile_name=profile_name, region_name=region)
    cfn = session.client("cloudformation")
    outputs = _get_stack_outputs(cfn, stack_name)
    ecr_repos = _list_ecr_cache_repos(session)
    return {"outputs": outputs, "ecr_repo_count": len(ecr_repos)}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _get_stack_outputs(cfn: Any, stack_name: str) -> dict[str, str]:
    response = cfn.describe_stacks(StackName=stack_name)  # type: ignore[union-attr]
    stack = response["Stacks"][0]
    return {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}


def _delete_fargate_profiles(session: boto3.Session, cluster_name: str) -> None:
    eks = session.client("eks")
    profiles = eks.list_fargate_profiles(clusterName=cluster_name).get("fargateProfileNames", [])
    if not profiles:
        return

    print(f"Deleting {len(profiles)} Fargate profile(s)...")
    for name in profiles:
        try:
            eks.delete_fargate_profile(clusterName=cluster_name, fargateProfileName=name)
        except Exception as e:
            logger.warning("Failed to delete Fargate profile %s: %s", name, e)

    for _ in range(60):
        remaining = eks.list_fargate_profiles(clusterName=cluster_name).get("fargateProfileNames", [])
        if not remaining:
            break
        time.sleep(5)
    print("Fargate profiles deleted.")


def _list_ecr_cache_repos(session: boto3.Session) -> list[str]:
    ecr = session.client("ecr")
    repos = []
    for page in ecr.get_paginator("describe_repositories").paginate():
        for repo in page["repositories"]:
            if repo["repositoryName"].startswith("docker-hub/"):
                repos.append(repo["repositoryName"])
    return repos


def _delete_ecr_cache_repos(session: boto3.Session, repos: list[str]) -> None:
    ecr = session.client("ecr")
    print(f"Deleting {len(repos)} ECR pull-through cache repos...")
    for name in repos:
        try:
            ecr.delete_repository(repositoryName=name, force=True)
        except Exception as e:
            logger.warning("Failed to delete ECR repo %s: %s", name, e)
    print("ECR cache repos deleted.")


def _wait_for_stack_delete(cfn: Any, stack_name: str) -> str | None:
    """Poll until stack deletion completes. Returns final status or None if gone."""
    for _ in range(120):
        time.sleep(15)
        try:
            status: str = cfn.describe_stacks(StackName=stack_name)["Stacks"][0]["StackStatus"]
        except Exception as e:
            if "does not exist" in str(e):
                return None  # Successfully deleted
            raise
        if status != "DELETE_IN_PROGRESS":
            return status
    return None


def _delete_stack(cfn: Any, stack_name: str) -> None:
    """Delete a CloudFormation stack, retaining stuck resources if needed."""
    cfn.delete_stack(StackName=stack_name)  # type: ignore[union-attr]
    status = _wait_for_stack_delete(cfn, stack_name)

    if status != "DELETE_FAILED":
        return

    # Find resources that failed to delete and retry without them
    failed = list({
        event["LogicalResourceId"]
        for event in cfn.describe_stack_events(StackName=stack_name).get("StackEvents", [])  # type: ignore[union-attr]
        if event["ResourceStatus"] == "DELETE_FAILED" and event["LogicalResourceId"] != stack_name
    })
    if not failed:
        return

    print(f"Retrying with {len(failed)} stuck resource(s) retained: {', '.join(failed)}")
    cfn.delete_stack(StackName=stack_name, RetainResources=failed)  # type: ignore[union-attr]
    _wait_for_stack_delete(cfn, stack_name)

    # Best-effort cleanup of retained VPC resources
    _cleanup_retained_vpc(boto3.Session(region_name=cfn.meta.region_name), stack_name)  # type: ignore[union-attr]


def _cleanup_retained_vpc(session: boto3.Session, stack_name: str) -> None:
    """Best-effort cleanup of VPC resources stuck due to external ENIs."""
    ec2 = session.client("ec2")

    vpcs = ec2.describe_vpcs(Filters=[{"Name": "tag:aws:cloudformation:stack-name", "Values": [stack_name]}])
    if not vpcs["Vpcs"]:
        return
    vpc_id = vpcs["Vpcs"][0]["VpcId"]

    # Detach and delete ENIs (e.g. GuardDuty), then subnets
    subnets = ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])
    for subnet in subnets.get("Subnets", []):
        enis = ec2.describe_network_interfaces(Filters=[{"Name": "subnet-id", "Values": [subnet["SubnetId"]]}])
        for eni in enis.get("NetworkInterfaces", []):
            try:
                if eni.get("Attachment"):
                    ec2.detach_network_interface(AttachmentId=eni["Attachment"]["AttachmentId"], Force=True)
                ec2.delete_network_interface(NetworkInterfaceId=eni["NetworkInterfaceId"])
            except Exception:
                pass
        try:
            ec2.delete_subnet(SubnetId=subnet["SubnetId"])
        except Exception:
            pass

    # Detach and delete internet gateways
    igws = ec2.describe_internet_gateways(Filters=[{"Name": "attachment.vpc-id", "Values": [vpc_id]}])
    for igw in igws.get("InternetGateways", []):
        try:
            ec2.detach_internet_gateway(InternetGatewayId=igw["InternetGatewayId"], VpcId=vpc_id)
            ec2.delete_internet_gateway(InternetGatewayId=igw["InternetGatewayId"])
        except Exception:
            pass

    try:
        ec2.delete_vpc(VpcId=vpc_id)
        print(f"Cleaned up retained VPC {vpc_id}")
    except Exception as e:
        print(f"Note: VPC {vpc_id} could not be fully deleted: {e}")
        print("  You may need to delete it manually in the AWS console.")
