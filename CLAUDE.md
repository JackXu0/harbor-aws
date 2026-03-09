# harbor-aws

AWS ECS/Fargate execution backend for [Harbor](https://github.com/harbor-framework/harbor) benchmarks. Zero idle cost — pay only for Fargate runtime.

## Architecture

- **CDK stack** (`src/harbor_aws/cdk/stack.py`) is the single source of truth for infrastructure
- **boto3 client library** (`src/harbor_aws/core/`) wraps AWS APIs for running containers
- **Shared resources**: one stack (VPC, ECS cluster, IAM roles, CloudWatch) reused across all Docker environments
- **On-demand Fargate** for max speed; prebuilt Docker images only

### Cost When Idle: ~$0

All resources are pay-per-use: VPC, ECS cluster, IAM roles, security groups = free. CloudWatch has negligible storage costs.

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
    ├── config.py            # AWSConfig dataclass, Fargate CPU/memory mapping, ECS client factory, stack loader
    ├── containers.py        # ECS task lifecycle (register, run, wait, stop)
    └── exec.py              # Command execution + file transfer via ECS Exec/SSM
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

# Run benchmarks
harbor trials start -p ./task \
  --environment-import-path harbor_aws.environment:AWSEnvironment

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

- Python 3.12+, async/await throughout (boto3 via `asyncio.to_thread()`)
- `tenacity` for retries on AWS API calls
- Strict typing: `mypy --disallow-untyped-defs`, PEP 561
- Ruff: line-length 120, rules B/E/F/I/N/UP/W
- `aws-cdk-lib` is an optional dependency (`[cdk]` extra) — only needed for deploy
