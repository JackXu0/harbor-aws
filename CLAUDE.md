# harbor-aws

AWS ECS/Fargate execution backend for [Harbor](https://github.com/harbor-framework/harbor) benchmarks. Zero idle cost — pay only for Fargate runtime and CodeBuild minutes.

## Architecture

- **CDK stack** (`src/harbor_aws/cdk/stack.py`) is the single source of truth for infrastructure
- **boto3 client library** (`src/harbor_aws/core/`) wraps AWS APIs for running containers
- **Auto-provision**: infrastructure created automatically on first run via CDK synth + boto3 deploy
- **Shared resources**: one stack (VPC, ECS cluster, ECR, S3, CodeBuild) reused across all Docker environments
- **On-demand Fargate** for max speed; CodeBuild `LARGE` compute for fast image builds

### Cost When Idle: ~$0

All resources are pay-per-use: VPC, ECS cluster, IAM roles, CodeBuild, security groups = free. ECR/S3/CloudWatch have negligible storage costs with auto-cleanup.

## Project Structure

```
src/harbor_aws/
├── __init__.py              # Exports: AWSConfig, AWSEnvironment
├── __main__.py              # CLI: python -m harbor_aws deploy|status|destroy
├── environment.py           # Harbor BaseEnvironment adapter
├── cdk/
│   ├── stack.py             # CDK stack (single source of truth for infra)
│   └── deploy.py            # CDK synth → CloudFormation JSON → boto3 deploy
└── core/
    ├── config.py            # AWSConfig dataclass + Fargate CPU/memory mapping
    ├── clients.py           # Singleton boto3 client manager (ref-counted)
    ├── containers.py        # ECS task lifecycle (register, run, wait, stop)
    ├── images.py            # CodeBuild image building + ECR management
    ├── exec.py              # Command execution via ECS Exec/SSM
    ├── files.py             # File transfer via base64-over-exec (5MB limit)
    ├── stack.py             # Read CloudFormation outputs → AWSConfig
    └── provision.py         # Auto-provision: ensure stack exists before use
infrastructure/cdk/
├── cdk.json                 # For `cdk deploy` users
├── app.py                   # Imports from harbor_aws.cdk.stack
└── requirements.txt
```

## Quick Start

```bash
# Install
pip install -e ".[cdk]"

# Deploy infrastructure (one-time, ~3-5 minutes)
python -m harbor_aws deploy --region us-east-1

# Run benchmarks (infra auto-provisions if not deployed)
harbor trials start -p ./task \
  --environment-import-path harbor_aws.environment:AWSEnvironment \
  --ek stack_name=harbor-aws --ek region=us-east-1

# Check status / tear down
python -m harbor_aws status
python -m harbor_aws destroy
```

## Build & Dev

```bash
pip install -e ".[dev,cdk]"
pytest
ruff check src/
mypy src/
```

## Conventions

- Python 3.10+, async/await throughout (boto3 via `asyncio.to_thread()`)
- `tenacity` for retries on AWS API calls
- Strict typing: `mypy --disallow-untyped-defs`, PEP 561
- Ruff: line-length 120, rules B/E/F/I/N/UP/W
- `aws-cdk-lib` is an optional dependency (`[cdk]` extra) — only needed for deploy/auto-provision
