# harbor-aws

AWS EKS/Fargate execution backend for [Harbor](https://github.com/harbor-framework/harbor) benchmarks. Designed for maximum concurrency — run hundreds of benchmark tasks in parallel.

## Architecture

- **CDK stack** (`src/harbor_aws/cdk/stack.py`) is the single source of truth for infrastructure
- **Kubernetes exec**: commands run via WebSocket exec (same as `kubectl exec`) — no daemon, no polling
- **Tar-over-exec** for file transfer (same mechanism as `kubectl cp`)
- **Shared resources**: one stack (VPC, EKS cluster, IAM roles) reused across all benchmark environments
- **EKS Fargate** for serverless pods; prebuilt Docker images only

### Cost

- EKS control plane: ~$73/mo (fixed)
- Fargate pods: pay-per-second only when running
- VPC, IAM, CloudWatch: negligible

## Project Structure

```
src/harbor_aws/
├── __init__.py              # Exports: AWSConfig, AWSEnvironment
├── __main__.py              # CLI: python -m harbor_aws deploy|status|stop|destroy
├── environment.py           # Harbor BaseEnvironment adapter
├── cdk/
│   ├── stack.py             # CDK stack (single source of truth for infra)
│   └── deploy.py            # CDK synth → CloudFormation JSON → boto3 deploy
└── core/
    ├── config.py            # AWSConfig dataclass, k8s client factory, stack loader
    ├── pods.py              # Kubernetes pod lifecycle (create, wait, delete)
    ├── exec.py              # Command execution via Kubernetes WebSocket exec
    └── files.py             # File transfer via tar-over-exec
```

## Quick Start

```bash
# Install
pip install -e ".[cdk]"

# Deploy infrastructure (one-time, ~15-20 minutes for EKS)
python -m harbor_aws deploy --region us-east-1

# Run benchmarks
harbor trials start -p ./task \
  --environment-import-path harbor_aws.adapter:AWSEnvironment \
  --ek stack_name=harbor-aws

# Check status / clean up / tear down
python -m harbor_aws status
python -m harbor_aws stop      # delete pods, keep infra
python -m harbor_aws destroy   # delete everything
```

## Build & Dev

```bash
pip install -e ".[dev,cdk]"
ruff check src/
mypy src/
```

## Conventions

- Python 3.12+, async/await throughout (boto3 + kubernetes client via `asyncio.to_thread()`)
- `tenacity` for retries on AWS/K8s API calls
- Strict typing: `mypy --disallow-untyped-defs`, PEP 561
- Ruff: line-length 120, rules B/E/F/I/N/UP/W
- `aws-cdk-lib` is an optional dependency (`[cdk]` extra) — only needed for deploy
