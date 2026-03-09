"""Deploy harbor-aws EKS infrastructure via CDK CLI.

EKS requires CDK bootstrap (for Lambda assets used by custom resources).
This module uses `cdk deploy` which handles bootstrap assets automatically.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import tempfile

logger = logging.getLogger(__name__)


def _write_cdk_app(stack_prefix: str, out_dir: str) -> str:
    """Write a minimal CDK app to a temporary directory. Returns the app.py path."""
    app_code = f"""\
import aws_cdk as cdk
import sys
sys.path.insert(0, "{os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))}")
from harbor_aws.cdk.stack import HarborAWSStack

app = cdk.App()
HarborAWSStack(app, "{stack_prefix}", stack_prefix="{stack_prefix}")
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


async def deploy(
    stack_prefix: str = "harbor-aws",
    region: str = "us-east-1",
    profile_name: str | None = None,
) -> dict[str, str]:
    """Deploy harbor-aws EKS infrastructure. Returns stack outputs.

    Requires: npm install -g aws-cdk
    """
    import boto3

    def _deploy() -> dict[str, str]:
        session = boto3.Session(profile_name=profile_name, region_name=region)

        # First, bootstrap CDK if needed
        _ensure_cdk_bootstrap(region, profile_name)

        # Deploy via CDK CLI
        with tempfile.TemporaryDirectory() as tmp_dir:
            _write_cdk_app(stack_prefix, tmp_dir)

            env = os.environ.copy()
            env["AWS_DEFAULT_REGION"] = region
            if profile_name:
                env["AWS_PROFILE"] = profile_name

            logger.info("Deploying stack '%s' in %s (EKS takes ~15-20 minutes)...", stack_prefix, region)

            result = subprocess.run(
                [
                    "cdk", "deploy",
                    "--app", f"python {os.path.join(tmp_dir, 'app.py')}",
                    "--require-approval", "never",
                    "--outputs-file", os.path.join(tmp_dir, "outputs.json"),
                ],
                cwd=tmp_dir,
                env=env,
                capture_output=True,
                text=True,
                timeout=1800,
            )

            if result.returncode != 0:
                logger.error("CDK deploy stderr:\n%s", result.stderr[-2000:] if result.stderr else "")
                raise RuntimeError(f"CDK deploy failed (exit code {result.returncode})")

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

    return await asyncio.to_thread(_deploy)


def _ensure_cdk_bootstrap(region: str, profile_name: str | None) -> None:
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

    logger.info("CDK bootstrap not found. Running 'cdk bootstrap'...")
    env = os.environ.copy()
    env["AWS_DEFAULT_REGION"] = region
    if profile_name:
        env["AWS_PROFILE"] = profile_name

    result = subprocess.run(
        ["cdk", "bootstrap", f"aws://unknown-account/{region}"],
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


def _get_outputs(cfn: object, stack_name: str) -> dict[str, str]:
    """Read stack outputs as a dict."""
    response = cfn.describe_stacks(StackName=stack_name)  # type: ignore[union-attr]
    stacks = response.get("Stacks", [])
    if not stacks:
        raise RuntimeError(f"Stack '{stack_name}' not found")
    return {o["OutputKey"]: o["OutputValue"] for o in stacks[0].get("Outputs", [])}
