"""CloudFormation stack output reader for auto-configuration."""

from __future__ import annotations

import asyncio
import logging

from harbor_aws.core.config import AWSConfig

logger = logging.getLogger(__name__)


async def load_config_from_stack(
    stack_name: str,
    region: str = "us-east-1",
    profile_name: str | None = None,
    auto_provision: bool = True,
) -> AWSConfig:
    """Load AWSConfig from CloudFormation stack outputs.

    If auto_provision is True and the stack doesn't exist, it will be
    created automatically via CDK synth + CloudFormation (requires harbor-aws[cdk]).
    """
    import boto3

    if auto_provision:
        from harbor_aws.core.provision import ensure_stack

        await ensure_stack(stack_name, region, profile_name)

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
        cluster_name=outputs.get("ClusterName", "harbor-aws"),
        subnets=outputs.get("SubnetIds", "").split(","),
        security_groups=[outputs["SecurityGroupId"]] if "SecurityGroupId" in outputs else [],
        task_execution_role_arn=outputs.get("TaskExecutionRoleArn", ""),
        task_role_arn=outputs.get("TaskRoleArn", ""),
        ecr_repository_uri=outputs.get("ECRRepositoryUri", ""),
        codebuild_project_name=outputs.get("CodeBuildProjectName", "harbor-aws-builder"),
        s3_bucket=outputs.get("S3BucketName", ""),
    )

    config.validate()
    return config
