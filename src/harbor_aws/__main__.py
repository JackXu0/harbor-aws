"""CLI entry point: python -m harbor_aws <command>

Commands:
    deploy   - Create harbor-aws infrastructure in your AWS account
    status   - Check if infrastructure is deployed
    stop     - Stop all running tasks (keeps infrastructure)
    destroy  - Tear down everything
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

    # stop
    stop_p = sub.add_parser("stop", help="stop all running tasks (keeps infrastructure)")
    stop_p.add_argument("--stack-name", default="harbor-aws")
    stop_p.add_argument("--region", default="us-east-1")
    stop_p.add_argument("--profile", default=None)

    # destroy
    destroy_p = sub.add_parser("destroy", help="tear down everything")
    destroy_p.add_argument("--stack-name", default="harbor-aws")
    destroy_p.add_argument("--region", default="us-east-1")
    destroy_p.add_argument("--profile", default=None)
    destroy_p.add_argument("-y", "--yes", action="store_true", help="skip confirmation")

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s",
    )
    logging.getLogger("botocore").setLevel(logging.WARNING)

    if args.command == "deploy":
        asyncio.run(_deploy(args))
    elif args.command == "status":
        asyncio.run(_status(args))
    elif args.command == "stop":
        asyncio.run(_stop(args))
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


async def _stop(args: argparse.Namespace) -> None:
    import boto3

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    await _cleanup_ecs(session, args.stack_name)
    await _cleanup_task_definitions(session, args.stack_name)
    print("Cleaned up running tasks. Infrastructure ready for next run.")


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
    print(f"  + VPC, IAM roles, log groups, dashboard")

    if not args.yes:
        confirm = input("\nProceed? [y/N] ")
        if confirm.lower() != "y":
            print("Cancelled.")
            return

    # 1. Stop all running ECS tasks in the cluster
    if outputs.get("ClusterName"):
        await _cleanup_ecs(session, outputs["ClusterName"])

    # 2. Delete the CloudFormation stack
    print("Deleting CloudFormation stack...")
    cfn.delete_stack(StackName=args.stack_name)
    waiter = cfn.get_waiter("stack_delete_complete")
    waiter.wait(StackName=args.stack_name, WaiterConfig={"Delay": 10, "MaxAttempts": 60})

    # 3. Deregister orphaned task definitions
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


if __name__ == "__main__":
    main()
