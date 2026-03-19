"""Deploy harbor-aws EKS infrastructure via CDK CLI.

EKS requires CDK bootstrap (for Lambda assets used by custom resources).
This module uses `cdk deploy` which handles bootstrap assets automatically.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import shutil
import subprocess
import sys
import tempfile

logger = logging.getLogger(__name__)


def _find_cdk() -> str:
    """Find the CDK CLI command. Prefers global 'cdk', falls back to 'npx aws-cdk'."""
    if shutil.which("cdk"):
        return "cdk"
    if shutil.which("npx"):
        return "npx --registry https://registry.npmjs.org -y aws-cdk"
    raise RuntimeError("CDK CLI not found. Install with: npm install -g aws-cdk")


def _write_cdk_app(
    stack_prefix: str,
    out_dir: str,
    runner_account_ids: list[str] | None = None,
    docker_hub_secret_arn: str | None = None,
) -> str:
    """Write a minimal CDK app to a temporary directory. Returns the app.py path."""
    # Resolve the src directory so the CDK app can import harbor_aws
    src_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    runner_ids_repr = repr(runner_account_ids) if runner_account_ids else "None"
    secret_repr = repr(docker_hub_secret_arn) if docker_hub_secret_arn else "None"
    app_code = f"""\
import aws_cdk as cdk
import sys
sys.path.insert(0, "{src_dir}")
from harbor_aws.cdk.stack import HarborAWSStack

app = cdk.App()
HarborAWSStack(
    app, "{stack_prefix}",
    stack_prefix="{stack_prefix}",
    runner_account_ids={runner_ids_repr},
    docker_hub_secret_arn={secret_repr},
)
app.synth()
"""
    app_path = os.path.join(out_dir, "app.py")
    with open(app_path, "w") as f:
        f.write(app_code)

    cdk_json = {"app": f"python {app_path}"}
    cdk_json_path = os.path.join(out_dir, "cdk.json")
    with open(cdk_json_path, "w") as f:
        json.dump(cdk_json, f)

    return out_dir


def deploy(
    stack_prefix: str = "harbor-aws",
    region: str = "us-east-1",
    profile_name: str | None = None,
    runner_account_ids: list[str] | None = None,
) -> dict[str, str]:
    """Deploy harbor-aws EKS infrastructure. Returns stack outputs.

    Requires: CDK CLI (npm install -g aws-cdk) or npx.
    """
    import boto3

    # Check for Docker Hub credentials before the long deploy
    session = boto3.Session(profile_name=profile_name, region_name=region)
    docker_hub_secret_arn = _find_docker_hub_secret(session)
    if not docker_hub_secret_arn:
        docker_hub_secret_arn = _prompt_docker_hub_credentials(session)

    cdk_cmd = _find_cdk()

    # First, bootstrap CDK if needed
    _ensure_cdk_bootstrap(region, profile_name, cdk_cmd)

    # Deploy via CDK CLI
    with tempfile.TemporaryDirectory() as tmp_dir:
        _write_cdk_app(stack_prefix, tmp_dir, runner_account_ids, docker_hub_secret_arn)

        env = os.environ.copy()
        env["AWS_DEFAULT_REGION"] = region
        if profile_name:
            env["AWS_PROFILE"] = profile_name

        logger.info("Deploying stack '%s' in %s (EKS takes ~15-20 minutes)...", stack_prefix, region)

        app_arg = f"{sys.executable} {os.path.join(tmp_dir, 'app.py')}"
        cmd = f"{cdk_cmd} deploy --app {shlex.quote(app_arg)} --require-approval never --outputs-file {shlex.quote(os.path.join(tmp_dir, 'outputs.json'))}"
        result = subprocess.run(
            cmd,
            shell=True,
            cwd=tmp_dir,
            env=env,
            capture_output=True,
            text=True,
            timeout=1800,
        )

        if result.returncode != 0:
            stderr = result.stderr[-2000:] if result.stderr else "(no output)"
            raise RuntimeError(f"CDK deploy failed:\n{stderr}")

        # Read outputs from CDK output file
        outputs_file = os.path.join(tmp_dir, "outputs.json")
        if os.path.exists(outputs_file):
            with open(outputs_file) as f:
                all_outputs = json.load(f)
            # CDK outputs are nested under the stack name
            return all_outputs.get(stack_prefix, {})

    # Fallback: read from CloudFormation
    cfn = session.client("cloudformation")
    return _get_outputs(cfn, stack_prefix)


def _ensure_cdk_bootstrap(region: str, profile_name: str | None, cdk_cmd: str) -> None:
    """Ensure CDK bootstrap stack exists."""
    import boto3

    session = boto3.Session(profile_name=profile_name, region_name=region)
    cfn = session.client("cloudformation")

    try:
        response = cfn.describe_stacks(StackName="CDKToolkit")
        status = response["Stacks"][0]["StackStatus"]
        if status in ("CREATE_COMPLETE", "UPDATE_COMPLETE"):
            return  # Already bootstrapped
    except Exception as e:
        if "does not exist" not in str(e):
            raise

    # Get account ID for bootstrap
    sts = session.client("sts")
    account_id = sts.get_caller_identity()["Account"]

    logger.info("CDK bootstrap not found. Running 'cdk bootstrap'...")
    env = os.environ.copy()
    env["AWS_DEFAULT_REGION"] = region
    if profile_name:
        env["AWS_PROFILE"] = profile_name

    result = subprocess.run(
        f"{cdk_cmd} bootstrap aws://{account_id}/{region}",
        shell=True,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"CDK bootstrap failed. Install CDK CLI: npm install -g aws-cdk\n"
            f"stderr: {result.stderr[-1000:] if result.stderr else ''}"
        )
    logger.info("CDK bootstrap complete.")


def _prompt_docker_hub_credentials(session: object) -> str | None:
    """Prompt the user to optionally provide Docker Hub credentials for ECR pull-through cache."""
    import getpass

    print("\nECR pull-through cache avoids Docker Hub rate limits at high concurrency.")
    print("Provide Docker Hub credentials to enable it, or skip.\n")
    answer = input("Set up ECR pull-through cache? [y/N] ").strip().lower()
    if answer != "y":
        print("Skipping — pods will pull directly from Docker Hub.\n")
        return None

    username = input("Docker Hub username: ").strip()
    token = getpass.getpass("Docker Hub access token: ").strip()
    if not username or not token:
        print("Empty credentials — skipping ECR pull-through cache.\n")
        return None

    import json

    sm = session.client("secretsmanager")  # type: ignore[union-attr]
    secret_name = "ecr-pullthroughcache/docker-hub"
    secret_value = json.dumps({"username": username, "accessToken": token})
    resp = sm.create_secret(Name=secret_name, SecretString=secret_value)
    arn = resp["ARN"]
    print(f"Created secret: {secret_name}\n")
    return arn


def _find_docker_hub_secret(session: object) -> str | None:
    """Return the ARN of the Docker Hub secret if it exists, else None."""
    sm = session.client("secretsmanager")  # type: ignore[union-attr]
    try:
        resp = sm.describe_secret(SecretId="ecr-pullthroughcache/docker-hub")
        print("ECR pull-through cache: enabled (Docker Hub credentials found).")
        return resp["ARN"]
    except Exception:
        return None


def _get_outputs(cfn: object, stack_name: str) -> dict[str, str]:
    """Read stack outputs as a dict."""
    response = cfn.describe_stacks(StackName=stack_name)  # type: ignore[union-attr]
    stacks = response.get("Stacks", [])
    if not stacks:
        raise RuntimeError(f"Stack '{stack_name}' not found")
    return {o["OutputKey"]: o["OutputValue"] for o in stacks[0].get("Outputs", [])}
