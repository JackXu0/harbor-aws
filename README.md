# harbor-aws

AWS ECS/Fargate execution backend for [Harbor](https://github.com/harbor-framework/harbor) benchmarks.

Deploy once, run any Docker-based benchmark elastically on Fargate. Pay only for compute — $0 when idle.

## Quick Start

### 1. Install

```bash
git clone https://github.com/JackXu0/harbor-aws.git
cd harbor-aws
pip install -e ".[cdk]"
```

### 2. Deploy Infrastructure

```bash
python -m harbor_aws deploy
```

That's it. Creates a shared VPC, ECS cluster, ECR repo, S3 bucket, and CodeBuild project in ~3-5 minutes. All resources are reused across benchmark environments.

### 3. Run Benchmarks

```bash
harbor trials start -p ./task \
  --environment-import-path harbor_aws.environment:AWSEnvironment \
  --ek stack_name=harbor-aws
```

Infrastructure is also auto-provisioned on first run if you skip step 2.

### 4. Clean Up

```bash
python -m harbor_aws destroy
```

This fully removes **everything** — stops running tasks, empties S3 and ECR, deletes the CloudFormation stack, and deregisters task definitions. Nothing is left behind.

> **Note:** Do not use `aws cloudformation delete-stack` directly — it will fail on the non-empty S3 bucket and leave the ECR repository orphaned.

### 5. Check Status

```bash
python -m harbor_aws status
```

## Prerequisites

- Python 3.10+
- AWS CLI configured with credentials
- [session-manager-plugin](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html) (required for ECS Exec)

## Cost

| State | Cost |
|---|---|
| Idle (deployed, no tasks running) | ~$0/month |
| Running benchmarks | Fargate on-demand pricing per task-second |

All resources use pay-per-use pricing. No NAT gateways, no EC2 instances. S3 objects auto-expire after 7 days, logs after 7 days, ECR keeps last 50 images.

## CLI Reference

```
python -m harbor_aws <command> [options]

Commands:
  deploy    Deploy infrastructure (idempotent, safe to re-run)
  status    Show stack status and outputs
  destroy   Full cleanup of all resources

Options:
  --stack-name   Stack name (default: harbor-aws)
  --region       AWS region (default: us-east-1)
  --profile      AWS CLI profile
  -y, --yes      Skip confirmation (destroy only)
  -v, --verbose  Verbose output
```

## Configuration

Configuration is auto-loaded from CloudFormation stack outputs. Just pass the stack name:

```bash
--ek stack_name=harbor-aws
```

For advanced use, you can pass individual parameters:

```bash
harbor trials start -p ./task \
  --environment-import-path harbor_aws.environment:AWSEnvironment \
  --ek region=us-east-1 \
  --ek cluster_name=harbor-aws \
  --ek subnets=subnet-abc,subnet-def \
  --ek security_groups=sg-123 \
  --ek task_execution_role_arn=arn:aws:iam::123:role/exec \
  --ek task_role_arn=arn:aws:iam::123:role/task \
  --ek ecr_repository_uri=123.dkr.ecr.us-east-1.amazonaws.com/harbor-aws \
  --ek s3_bucket=harbor-aws-123-us-east-1 \
  --ek codebuild_project_name=harbor-aws-builder
```

## Architecture

```
┌─────────────┐     ┌────────────────┐     ┌──────────────┐
│  Harbor CLI  │────▶│ AWSEnvironment │────▶│ ECS Fargate  │
│             │     │   (adapter)    │     │ (on-demand)  │
└─────────────┘     └────────────────┘     └──────────────┘
                           │                      │
                    ┌──────┴──────┐         ┌─────┴─────┐
                    │  CodeBuild   │         │ ECS Exec  │
                    │  (LARGE)     │         │  (SSM)    │
                    └──────┬──────┘         └───────────┘
                           │
                    ┌──────┴──────┐
                    │    ECR      │
                    │ (registry)  │
                    └─────────────┘
```

Infrastructure is defined as a CDK stack in `src/harbor_aws/cdk/stack.py`. Deployment synthesizes the CDK in-memory and deploys via CloudFormation — no CDK CLI needed.

## Development

```bash
pip install -e ".[dev,cdk]"
pytest
ruff check src/
mypy src/
```
