"""Auto-provision harbor-aws infrastructure via CDK synth + CloudFormation."""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


async def ensure_stack(
    stack_name: str,
    region: str = "us-east-1",
    profile_name: str | None = None,
) -> None:
    """Ensure the harbor-aws stack exists. Creates it if needed via CDK synth.

    Requires: pip install harbor-aws[cdk]
    """
    from harbor_aws.cdk.deploy import deploy

    await deploy(stack_prefix=stack_name, region=region, profile_name=profile_name)


async def stack_exists(
    stack_name: str,
    region: str = "us-east-1",
    profile_name: str | None = None,
) -> bool:
    """Check if the harbor-aws CloudFormation stack exists and is ready."""
    import boto3

    def _check() -> bool:
        session = boto3.Session(profile_name=profile_name, region_name=region)
        cfn = session.client("cloudformation")
        try:
            response = cfn.describe_stacks(StackName=stack_name)
            stacks = response.get("Stacks", [])
            if stacks:
                return stacks[0]["StackStatus"] in (
                    "CREATE_COMPLETE",
                    "UPDATE_COMPLETE",
                    "UPDATE_ROLLBACK_COMPLETE",
                )
        except cfn.exceptions.ClientError as e:
            if "does not exist" in str(e):
                return False
            raise
        return False

    return await asyncio.to_thread(_check)
