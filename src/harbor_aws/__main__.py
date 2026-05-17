"""CLI entry point: python -m harbor_aws <command>

Commands:
    deploy   - Deploy the harbor-aws CDK stack
    destroy  - Tear down the harbor-aws stack
    env      - Print HARBOR_* env-var exports (use: eval $(harbor-aws env ...))
    stop     - Delete all running pods (requires HARBOR_* env vars)
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import logging
import os
import shutil
import subprocess
import sys
import tempfile

_DOCKERHUB_SECRET_NAME = "ecr-pullthroughcache/docker-hub"


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

    env_p = sub.add_parser("env", help="print HARBOR_* env exports (use: eval $(harbor-aws env ...))")
    _add_cdk_args(env_p)
    env_p.add_argument("--role-arn", default=None, help="cross-account role to assume")

    sub.add_parser("stop", help="delete all running pods (requires HARBOR_* env vars)")

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
        "env": _env,
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


def _synth(args: argparse.Namespace, outdir: str) -> None:
    """Build the cdk.App in-process and synth into ``outdir``."""
    import aws_cdk as cdk
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError

    from harbor_aws.cdk.stack import HarborAWSStack

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
    print(f"using Docker Hub secret: {arn}", file=sys.stderr)
    return arn


def _run_cdk(cdk_args: list[str]) -> None:
    if shutil.which("cdk") is None:
        sys.exit("cdk CLI not found. Install with: npm install -g aws-cdk")
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
    print(f"\nRun `eval $(harbor-aws env --stack-name {args.stack_name} --region {args.region})` "
          "to set the HARBOR_* env vars.",
          file=sys.stderr)


def _destroy(args: argparse.Namespace) -> None:
    with tempfile.TemporaryDirectory(prefix="harbor-aws-cdk-") as outdir:
        _synth(args, outdir)
        cdk_args = ["destroy", "--app", outdir, args.stack_name]
        if args.force:
            cdk_args.append("--force")
        _run_cdk(cdk_args)


# ===== env / stop =====


def _env(args: argparse.Namespace) -> None:
    """Read CFN outputs + discover NLB URL, print env exports for the adapter to consume."""
    nlb_url, bearer_token, cert_pem = _resolve_harbor_env(
        args.stack_name, args.region, getattr(args, "role_arn", None),
    )
    print(f"export HARBOR_NLB_URL={nlb_url}")
    print(f"export HARBOR_BEARER_TOKEN={bearer_token}")
    print(f"export HARBOR_NLB_CERT={base64.b64encode(cert_pem.encode()).decode()}")


def _stop(args: argparse.Namespace) -> None:
    asyncio.run(_async_stop(args))


async def _async_stop(_args: argparse.Namespace) -> None:
    from harbor_aws.orchestrator.client import control_pod

    pod_names = await control_pod.list_pods()
    if not pod_names:
        print("No running pods.")
        return
    await asyncio.gather(*(control_pod.delete_pod(name) for name in pod_names))
    print(f"Deleted {len(pod_names)} pod(s). Infrastructure ready for next run.")


def _resolve_harbor_env(stack_name: str, region: str, role_arn: str | None) -> tuple[str, str, str]:
    """Read CFN outputs and discover the NLB URL. Returns (nlb_url, bearer_token, cert_pem)."""
    import boto3
    from kubernetes import client as k8s_client
    from kubernetes import config as k8s_config
    from tenacity import retry, stop_after_attempt, wait_exponential_jitter

    # CFN outputs
    session = _assume_role_session(region, role_arn) if role_arn else boto3.Session(region_name=region)
    cfn = session.client("cloudformation")
    response = cfn.describe_stacks(StackName=stack_name)
    stack = response["Stacks"][0]
    outputs = {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}
    eks_cluster_name = outputs["EksClusterName"]
    namespace = outputs["Namespace"]
    bearer_token = outputs["HarborAdminToken"]
    cert_pem = outputs["HarborNlbCert"]

    # K8s NLB discovery (one-shot)
    cmd = ["aws", "eks", "update-kubeconfig", "--name", eks_cluster_name, "--region", region]
    if role_arn:
        cmd += ["--role-arn", role_arn]
    subprocess.run(cmd, check=True, capture_output=True, text=True)
    k8s_config.load_kube_config()
    api = k8s_client.CoreV1Api(api_client=k8s_client.ApiClient())

    @retry(stop=stop_after_attempt(10), wait=wait_exponential_jitter(initial=2, max=10, jitter=2), reraise=True)
    def _discover() -> str:
        svc = api.read_namespaced_service(name="harbor-control-nlb", namespace=namespace)
        ingress = getattr(getattr(svc.status, "load_balancer", None), "ingress", None)
        if not ingress or not ingress[0].hostname:
            raise RuntimeError(
                f"NLB hostname not ready in namespace '{namespace}' (LB Controller still provisioning)."
            )
        return f"https://{ingress[0].hostname}:8443"

    nlb_url = _discover()
    return nlb_url, bearer_token, cert_pem


def _assume_role_session(region: str, role_arn: str):  # noqa: ANN201 — boto3 Session
    import boto3
    creds = boto3.client("sts", region_name=region).assume_role(
        RoleArn=role_arn, RoleSessionName="harbor-aws",
    )["Credentials"]
    return boto3.Session(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
        region_name=region,
    )


if __name__ == "__main__":
    main()
