"""Synth CDK stack and deploy via boto3 CloudFormation API.

No CDK CLI needed — uses aws-cdk-lib to synthesize in-memory,
then deploys the resulting CloudFormation template via boto3.
"""

from __future__ import annotations

import asyncio
import json
import logging

logger = logging.getLogger(__name__)


def synth_template(stack_prefix: str = "harbor-aws") -> dict:
    """Synthesize the harbor-aws CDK stack into a CloudFormation template dict.

    Requires: pip install harbor-aws[cdk]
    """
    try:
        import aws_cdk as cdk
    except ImportError:
        raise ImportError(
            "aws-cdk-lib is required. Install with: pip install harbor-aws[cdk]"
        ) from None

    from harbor_aws.cdk.stack import HarborAWSStack

    app = cdk.App(context={"aws:cdk:disable-metadata": True})
    HarborAWSStack(
        app,
        stack_prefix,
        stack_prefix=stack_prefix,
        synthesizer=cdk.DefaultStackSynthesizer(generate_bootstrap_version_rule=False),
    )
    assembly = app.synth()
    return assembly.stacks[0].template


async def deploy(
    stack_prefix: str = "harbor-aws",
    region: str = "us-east-1",
    profile_name: str | None = None,
) -> dict[str, str]:
    """Deploy harbor-aws infrastructure. Returns stack outputs.

    Idempotent: creates the stack if it doesn't exist, no-ops if it does.
    """
    import boto3

    template_body = json.dumps(synth_template(stack_prefix))

    def _deploy() -> dict[str, str]:
        session = boto3.Session(profile_name=profile_name, region_name=region)
        cfn = session.client("cloudformation")

        # Check current state
        status = _get_stack_status(cfn, stack_prefix)

        if status in ("CREATE_COMPLETE", "UPDATE_COMPLETE", "UPDATE_ROLLBACK_COMPLETE"):
            logger.info("Stack '%s' already deployed (%s). Nothing to do.", stack_prefix, status)
            return _get_outputs(cfn, stack_prefix)

        if status is not None and status.endswith("_IN_PROGRESS"):
            logger.info("Stack '%s' has operation in progress (%s). Waiting...", stack_prefix, status)
            _wait_for_completion(cfn, stack_prefix)
            return _get_outputs(cfn, stack_prefix)

        if status is not None:
            raise RuntimeError(
                f"Stack '{stack_prefix}' is in state '{status}'. "
                "Delete it first: aws cloudformation delete-stack --stack-name " + stack_prefix
            )

        # Create new stack
        logger.info("Creating stack '%s' in %s...", stack_prefix, region)
        cfn.create_stack(
            StackName=stack_prefix,
            TemplateBody=template_body,
            Capabilities=["CAPABILITY_NAMED_IAM"],
            Tags=[
                {"Key": "managed-by", "Value": "harbor-aws"},
            ],
            OnFailure="DELETE",
        )

        logger.info("Waiting for stack creation (this takes ~3-5 minutes)...")
        _wait_for_completion(cfn, stack_prefix)
        logger.info("Stack '%s' is ready.", stack_prefix)
        return _get_outputs(cfn, stack_prefix)

    return await asyncio.to_thread(_deploy)


def _get_stack_status(cfn: object, stack_name: str) -> str | None:
    """Get CloudFormation stack status, or None if it doesn't exist."""
    try:
        response = cfn.describe_stacks(StackName=stack_name)  # type: ignore[union-attr]
        stacks = response.get("Stacks", [])
        return stacks[0]["StackStatus"] if stacks else None
    except Exception as e:
        if "does not exist" in str(e):
            return None
        raise


def _get_outputs(cfn: object, stack_name: str) -> dict[str, str]:
    """Read stack outputs as a dict."""
    response = cfn.describe_stacks(StackName=stack_name)  # type: ignore[union-attr]
    stacks = response.get("Stacks", [])
    if not stacks:
        raise RuntimeError(f"Stack '{stack_name}' not found")
    return {o["OutputKey"]: o["OutputValue"] for o in stacks[0].get("Outputs", [])}


def _wait_for_completion(cfn: object, stack_name: str, timeout_sec: int = 900) -> None:
    """Wait for stack operation to complete."""
    import time

    for elapsed in range(0, timeout_sec, 10):
        status = _get_stack_status(cfn, stack_name)

        if status is None:
            raise RuntimeError(
                f"Stack '{stack_name}' was deleted (creation likely failed). "
                "Check CloudFormation events in the AWS console."
            )

        if status in ("CREATE_COMPLETE", "UPDATE_COMPLETE"):
            return

        if status.endswith("_FAILED") or status in ("ROLLBACK_COMPLETE", "DELETE_COMPLETE"):
            raise RuntimeError(f"Stack '{stack_name}' failed: {status}")

        if elapsed % 30 == 0:
            logger.info("  ...%s (%ds)", status, elapsed)

        time.sleep(10)

    raise RuntimeError(f"Stack '{stack_name}' did not complete within {timeout_sec}s")
