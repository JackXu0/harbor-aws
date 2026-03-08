#!/usr/bin/env python3
"""CDK app entry point for harbor-aws infrastructure.

Usage:
    cd infrastructure/cdk
    pip install -r requirements.txt
    pip install -e ../..       # install harbor-aws package
    cdk deploy
"""

import aws_cdk as cdk

from harbor_aws.cdk.stack import HarborAWSStack

app = cdk.App()

stack_prefix = app.node.try_get_context("stack_prefix") or "harbor-aws"

HarborAWSStack(
    app,
    stack_prefix,
    stack_prefix=stack_prefix,
    description=(
        "Harbor AWS - Shared ECS/Fargate infrastructure for running "
        "containerized benchmarks. Deploy once, reuse across all environments."
    ),
)

app.synth()
