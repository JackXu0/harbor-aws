"""CLI entry point: python -m harbor_aws <command>

Commands:
    deploy   - Deploy the harbor-aws CDK stack (one-shot, no scaffolding)
    destroy  - Tear down the harbor-aws stack
    stop     - Delete all running pods (keeps infrastructure)
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import subprocess
import sys
import tempfile


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="harbor-aws",
        description="Harbor AWS — manage EKS/Fargate infrastructure for benchmarks",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="verbose output")

    sub = parser.add_subparsers(dest="command")

    deploy_p = sub.add_parser("deploy", help="deploy the harbor-aws CDK stack")
    _add_cdk_args(deploy_p)
    deploy_p.add_argument(
        "--cluster-admin-role-arn",
        default=None,
        help="IAM role ARN granted EKS cluster-admin (so kubectl works for that role)",
    )
    deploy_p.add_argument(
        "--docker-hub-secret-arn",
        default=None,
        help="Secrets Manager ARN for Docker Hub credentials (enables ECR pull-through cache)",
    )
    deploy_p.add_argument(
        "--cross-account-caller-id",
        action="append",
        default=None,
        dest="cross_account_caller_ids",
        help="AWS account ID allowed to assume the runner role (repeat for multiple)",
    )
    deploy_p.add_argument(
        "--require-approval",
        choices=["never", "any-change", "broadening"],
        default="never",
        help="cdk deploy --require-approval value (default: never)",
    )

    destroy_p = sub.add_parser("destroy", help="tear down the harbor-aws stack")
    _add_cdk_args(destroy_p)
    destroy_p.add_argument("--force", action="store_true", help="skip confirmation prompt")

    stop_p = sub.add_parser("stop", help="delete all running pods (keeps infrastructure)")
    stop_p.add_argument("--stack-name", default="harbor-aws")
    stop_p.add_argument("--region", default="us-east-1")
    stop_p.add_argument("--profile", default=None)

    args = parser.parse_args()

    # boto3 picks up AWS_PROFILE automatically — set it once so every code
    # path uses the right credentials without each call site passing it through.
    if getattr(args, "profile", None):
        os.environ["AWS_PROFILE"] = args.profile

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s",
    )
    logging.getLogger("botocore").setLevel(logging.WARNING)
    logging.getLogger("kubernetes").setLevel(logging.WARNING)

    commands = {
        "deploy": _deploy,
        "destroy": _destroy,
        "stop": _stop,
    }
    if args.command in commands:
        commands[args.command](args)
    else:
        parser.print_help()
        sys.exit(1)


def _add_cdk_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--stack-name", default="harbor-aws")
    p.add_argument("--region", default="us-east-1")
    p.add_argument("--profile", default=None)


_DOCKERHUB_SECRET_NAME = "ecr-pullthroughcache/docker-hub"


def _synth(args: argparse.Namespace, outdir: str) -> None:
    """Build the cdk.App in-process and synth into ``outdir``."""
    import aws_cdk as cdk
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError

    from harbor_aws.cdk.stack import HarborAWSStack

    # Derive account from STS
    try:
        account = boto3.client("sts", region_name=args.region).get_caller_identity()["Account"]
    except (BotoCoreError, ClientError, NoCredentialsError) as e:
        sys.exit(
            f"failed to read AWS account ID via STS: {e}\n"
            f"Set AWS credentials (e.g. `aws sso login` or `AWS_PROFILE=<profile>`) and retry."
        )

    docker_hub_secret_arn = getattr(args, "docker_hub_secret_arn", None) or _discover_dockerhub_secret(args.region)

    app = cdk.App(outdir=outdir)
    HarborAWSStack(
        app,
        args.stack_name,
        env=cdk.Environment(account=account, region=args.region),
        cluster_admin_role_arn=getattr(args, "cluster_admin_role_arn", None),
        docker_hub_secret_arn=docker_hub_secret_arn,
        cross_account_caller_ids=getattr(args, "cross_account_caller_ids", None),
    )
    app.synth()


def _discover_dockerhub_secret(region: str) -> str | None:
    """Look up the Docker Hub secret by name."""
    import boto3
    from botocore.exceptions import ClientError

    try:
        resp = boto3.client("secretsmanager", region_name=region).describe_secret(
            SecretId=_DOCKERHUB_SECRET_NAME,
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceNotFoundException":
            return None
        raise
    arn: str = resp["ARN"]
    print(f"using Docker Hub secret: {arn}")
    return arn


def _run_cdk(cdk_args: list[str]) -> None:
    if shutil.which("cdk") is None:
        sys.exit(
            "cdk CLI not found. Install with: npm install -g aws-cdk"
        )
    subprocess.run(["cdk", *cdk_args], check=True)


def _deploy(args: argparse.Namespace) -> None:
    with tempfile.TemporaryDirectory(prefix="harbor-aws-cdk-") as outdir:
        _synth(args, outdir)
        _run_cdk([
            "deploy",
            "--app", outdir,
            "--require-approval", args.require_approval,
            args.stack_name,
        ])


def _destroy(args: argparse.Namespace) -> None:
    with tempfile.TemporaryDirectory(prefix="harbor-aws-cdk-") as outdir:
        _synth(args, outdir)
        cdk_args = ["destroy", "--app", outdir, args.stack_name]
        if args.force:
            cdk_args.append("--force")
        _run_cdk(cdk_args)


def _stop(args: argparse.Namespace) -> None:
    import asyncio

    from harbor_aws.core.config import create_k8s_client, load_config_from_stack
    from harbor_aws.core.pods import delete_pod, list_pods

    config = asyncio.run(load_config_from_stack(
        stack_name=args.stack_name,
        region=args.region,
    ))
    api = create_k8s_client(config)

    pod_names = asyncio.run(list_pods(api, config.namespace))
    if not pod_names:
        print("No running pods.")
        return

    for name in pod_names:
        asyncio.run(delete_pod(api, config.namespace, name))
    print(f"Deleted {len(pod_names)} pod(s). Infrastructure ready for next run.")


if __name__ == "__main__":
    main()
