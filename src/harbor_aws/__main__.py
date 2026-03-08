"""CLI entry point: python -m harbor_aws <command>

Commands:
    deploy   - Create harbor-aws infrastructure in your AWS account
    status   - Check if infrastructure is deployed
    destroy  - Tear down infrastructure
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="harbor-aws",
        description="Harbor AWS — manage ECS/Fargate infrastructure for benchmarks",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="verbose output")

    sub = parser.add_subparsers(dest="command")

    # deploy
    deploy_p = sub.add_parser("deploy", help="deploy infrastructure (idempotent)")
    deploy_p.add_argument("--stack-name", default="harbor-aws", help="CloudFormation stack name (default: harbor-aws)")
    deploy_p.add_argument("--region", default="us-east-1", help="AWS region (default: us-east-1)")
    deploy_p.add_argument("--profile", default=None, help="AWS CLI profile name")

    # status
    status_p = sub.add_parser("status", help="check infrastructure status")
    status_p.add_argument("--stack-name", default="harbor-aws")
    status_p.add_argument("--region", default="us-east-1")
    status_p.add_argument("--profile", default=None)

    # destroy
    destroy_p = sub.add_parser("destroy", help="tear down infrastructure")
    destroy_p.add_argument("--stack-name", default="harbor-aws")
    destroy_p.add_argument("--region", default="us-east-1")
    destroy_p.add_argument("--profile", default=None)
    destroy_p.add_argument("-y", "--yes", action="store_true", help="skip confirmation")

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s",
    )

    if args.command == "deploy":
        asyncio.run(_deploy(args))
    elif args.command == "status":
        asyncio.run(_status(args))
    elif args.command == "destroy":
        asyncio.run(_destroy(args))
    else:
        parser.print_help()
        sys.exit(1)


async def _deploy(args: argparse.Namespace) -> None:
    from harbor_aws.cdk.deploy import deploy

    outputs = await deploy(
        stack_prefix=args.stack_name,
        region=args.region,
        profile_name=args.profile,
    )
    print("\nStack outputs:")
    for key, value in sorted(outputs.items()):
        print(f"  {key}: {value}")
    print(f"\nUse with Harbor:\n  harbor trials start -p ./task \\\n    --environment-import-path harbor_aws.environment:AWSEnvironment \\\n    --ek stack_name={args.stack_name} --ek region={args.region}")


async def _status(args: argparse.Namespace) -> None:
    import boto3

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    cfn = session.client("cloudformation")

    try:
        response = cfn.describe_stacks(StackName=args.stack_name)
        stack = response["Stacks"][0]
        print(f"Stack: {args.stack_name}")
        print(f"Status: {stack['StackStatus']}")
        print(f"Region: {args.region}")
        if stack.get("Outputs"):
            print("\nOutputs:")
            for o in stack["Outputs"]:
                print(f"  {o['OutputKey']}: {o['OutputValue']}")
    except Exception as e:
        if "does not exist" in str(e):
            print(f"Stack '{args.stack_name}' does not exist in {args.region}.")
            print(f"Deploy with: python -m harbor_aws deploy --region {args.region}")
        else:
            raise


async def _destroy(args: argparse.Namespace) -> None:
    import boto3

    session = boto3.Session(profile_name=args.profile, region_name=args.region)

    # Read stack outputs before deleting so we can clean up retained resources
    cfn = session.client("cloudformation")
    try:
        response = cfn.describe_stacks(StackName=args.stack_name)
        stack = response["Stacks"][0]
        outputs = {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}
    except Exception as e:
        if "does not exist" in str(e):
            print(f"Stack '{args.stack_name}' does not exist.")
            return
        raise

    # Show what will be cleaned up
    print(f"This will delete all harbor-aws resources in {args.region}:")
    print(f"  Stack:      {args.stack_name}")
    if outputs.get("ClusterName"):
        print(f"  ECS:        cluster '{outputs['ClusterName']}' (+ stop running tasks)")
    if outputs.get("ECRRepositoryUri"):
        ecr_name = outputs["ECRRepositoryUri"].rsplit("/", 1)[-1]
        print(f"  ECR:        repository '{ecr_name}' (all images deleted)")
    if outputs.get("S3BucketName"):
        print(f"  S3:         bucket '{outputs['S3BucketName']}' (all objects deleted)")
    print(f"  + VPC, IAM roles, CodeBuild project, log groups")

    if not args.yes:
        confirm = input("\nProceed? [y/N] ")
        if confirm.lower() != "y":
            print("Cancelled.")
            return

    # 1. Stop all running ECS tasks in the cluster
    if outputs.get("ClusterName"):
        await _cleanup_ecs(session, outputs["ClusterName"])

    # 2. Empty the S3 bucket (CloudFormation can't delete non-empty buckets)
    if outputs.get("S3BucketName"):
        await _empty_s3_bucket(session, outputs["S3BucketName"])

    # 3. Delete all images in ECR (repo has RETAIN policy, must clean manually)
    ecr_repo_name = None
    if outputs.get("ECRRepositoryUri"):
        ecr_repo_name = outputs["ECRRepositoryUri"].rsplit("/", 1)[-1]
        await _empty_ecr_repo(session, ecr_repo_name)

    # 4. Delete the CloudFormation stack
    print("Deleting CloudFormation stack...")
    cfn.delete_stack(StackName=args.stack_name)
    waiter = cfn.get_waiter("stack_delete_complete")
    waiter.wait(StackName=args.stack_name, WaiterConfig={"Delay": 10, "MaxAttempts": 60})

    # 5. Delete the retained ECR repo (not deleted by CloudFormation due to RETAIN policy)
    if ecr_repo_name:
        await _delete_ecr_repo(session, ecr_repo_name)

    # 6. Deregister orphaned task definitions
    await _cleanup_task_definitions(session, args.stack_name)

    print("All resources cleaned up.")


async def _cleanup_ecs(session: object, cluster_name: str) -> None:
    """Stop all running tasks in the cluster."""
    import asyncio as aio

    ecs = session.client("ecs")  # type: ignore[union-attr]

    def _stop_tasks() -> int:
        count = 0
        paginator = ecs.get_paginator("list_tasks")
        for page in paginator.paginate(cluster=cluster_name, desiredStatus="RUNNING"):
            task_arns = page.get("taskArns", [])
            for arn in task_arns:
                ecs.stop_task(cluster=cluster_name, task=arn, reason="harbor-aws destroy")
                count += 1
        return count

    count = await aio.to_thread(_stop_tasks)
    if count:
        print(f"  Stopped {count} running task(s).")


async def _empty_s3_bucket(session: object, bucket_name: str) -> None:
    """Delete all objects and versions from an S3 bucket."""
    import asyncio as aio

    s3 = session.resource("s3")  # type: ignore[union-attr]

    def _empty() -> int:
        bucket = s3.Bucket(bucket_name)
        # Delete all object versions (handles versioned buckets too)
        count = 0
        for batch in _batched(bucket.object_versions.all(), 1000):
            bucket.delete_objects(Delete={"Objects": [{"Key": v.key, "VersionId": v.id} for v in batch]})
            count += len(batch)
        if count == 0:
            # Try non-versioned delete
            for batch in _batched(bucket.objects.all(), 1000):
                bucket.delete_objects(Delete={"Objects": [{"Key": o.key} for o in batch]})
                count += len(batch)
        return count

    count = await aio.to_thread(_empty)
    print(f"  S3: deleted {count} object(s) from '{bucket_name}'.")


async def _empty_ecr_repo(session: object, repo_name: str) -> None:
    """Delete all images from an ECR repository."""
    import asyncio as aio

    ecr = session.client("ecr")  # type: ignore[union-attr]

    def _delete_images() -> int:
        count = 0
        try:
            paginator = ecr.get_paginator("list_images")
            for page in paginator.paginate(repositoryName=repo_name):
                image_ids = page.get("imageIds", [])
                if image_ids:
                    ecr.batch_delete_image(repositoryName=repo_name, imageIds=image_ids)
                    count += len(image_ids)
        except ecr.exceptions.RepositoryNotFoundException:
            pass
        return count

    count = await aio.to_thread(_delete_images)
    if count:
        print(f"  ECR: deleted {count} image(s) from '{repo_name}'.")


async def _delete_ecr_repo(session: object, repo_name: str) -> None:
    """Delete the ECR repository itself (retained by CloudFormation)."""
    import asyncio as aio

    ecr = session.client("ecr")  # type: ignore[union-attr]

    def _delete() -> None:
        try:
            ecr.delete_repository(repositoryName=repo_name, force=True)
        except ecr.exceptions.RepositoryNotFoundException:
            pass

    await aio.to_thread(_delete)
    print(f"  ECR: deleted repository '{repo_name}'.")


async def _cleanup_task_definitions(session: object, stack_prefix: str) -> None:
    """Deregister task definitions created by harbor-aws."""
    import asyncio as aio

    ecs = session.client("ecs")  # type: ignore[union-attr]

    def _deregister() -> int:
        count = 0
        try:
            paginator = ecs.get_paginator("list_task_definitions")
            for page in paginator.paginate(familyPrefix=f"harbor-aws-", status="ACTIVE"):
                for arn in page.get("taskDefinitionArns", []):
                    ecs.deregister_task_definition(taskDefinition=arn)
                    count += 1
        except Exception:
            pass
        return count

    count = await aio.to_thread(_deregister)
    if count:
        print(f"  ECS: deregistered {count} task definition(s).")


def _batched(iterable, n: int):  # type: ignore[no-untyped-def]
    """Yield successive n-sized chunks from an iterable."""
    batch = []
    for item in iterable:
        batch.append(item)
        if len(batch) == n:
            yield batch
            batch = []
    if batch:
        yield batch


if __name__ == "__main__":
    main()
