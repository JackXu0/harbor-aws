# harbor-aws

AWS EKS/Fargate execution backend for [Harbor](https://github.com/harbor-framework/harbor) benchmarks.

Harbor runs benchmarks locally by default — one task at a time. This package adds an AWS backend so you can run hundreds of tasks in parallel on EKS Fargate.

## Quick Start

### 1. Install

```bash
uv sync --extra cdk
```

### 2. Deploy Infrastructure

```bash
uv run python -m harbor_aws deploy
```

### 3. Run Terminal-Bench

```bash
uv run harbor run -d terminal-bench@2.0 -a swe-agent -m bedrock/zai.glm-4.7 -n 89 --environment-import-path harbor_aws.environment:AWSEnvironment
```

Other agent/model combinations:

```bash
# Sonnet 4 via Bedrock with swe-agent
uv run harbor run -d terminal-bench@2.0 -a swe-agent -m bedrock/anthropic.claude-sonnet-4-20250514-v1:0 -n 89 --environment-import-path harbor_aws.environment:AWSEnvironment

# Sonnet 4 via API key with aider
export ANTHROPIC_API_KEY=sk-ant-...
uv run harbor run -d terminal-bench@2.0 -a aider -m anthropic/claude-sonnet-4-20250514 -n 89 --environment-import-path harbor_aws.environment:AWSEnvironment
```

### 4. Clean Up

```bash
uv run python -m harbor_aws destroy
```

Fully removes everything — running tasks, stack, task definitions. Nothing left behind.

## Prerequisites

- [uv](https://docs.astral.sh/uv/getting-started/installation/)
- AWS CLI configured with credentials

## Cost

| Component | Cost |
|---|---|
| EKS control plane | ~$73/mo (fixed) |
| NAT gateway | ~$32/mo (fixed) |
| Fargate pods | Pay-per-second only when running |
| VPC, IAM, CloudWatch | Negligible |

## CLI Reference

```
uv run python -m harbor_aws <command> [options]

Commands:
  deploy    Deploy infrastructure (idempotent)
  status    Show stack status and outputs
  stop      Stop all running pods (keeps infrastructure)
  destroy   Full cleanup — tasks, stack, task definitions

Options:
  --stack-name   Stack name (default: harbor-aws)
  --region       AWS region (default: us-east-1)
  --profile      AWS CLI profile
  -y, --yes      Skip confirmation (destroy only)
  -v, --verbose  Verbose output
```

## Architecture

```
┌─────────────┐     ┌────────────────┐     ┌──────────────┐
│  Harbor CLI  │────▶│ AWSEnvironment │────▶│ EKS Fargate  │
│             │     │   (adapter)    │     │   (pods)     │
└─────────────┘     └────────────────┘     └──────────────┘
                            │
                    ┌───────┴───────┐
                    │               │
              WebSocket exec   Tar-over-exec
              (commands)       (file transfer)
```

Commands run via Kubernetes WebSocket exec (same as `kubectl exec`) — no daemon, no polling. File transfer uses tar-over-exec (same mechanism as `kubectl cp`).

Infrastructure defined as CDK in `src/harbor_aws/cdk/stack.py`. Deployment synthesizes in-memory and deploys via CloudFormation — no CDK CLI needed.

## Monitoring

A CloudWatch dashboard (`harbor-aws-monitor`) is created with the stack:

- **EKS Fargate** — running pod count, CPU utilization, memory utilization
- **Bedrock** — input/output tokens, invocations, errors, throttles, latency (per model)

Pod logs are sent to CloudWatch Logs at `/harbor-aws/harbor-aws` (7-day retention).

## Development

```bash
uv sync --extra dev --extra cdk
uv run pytest
uv run ruff check src/
uv run mypy src/
```
