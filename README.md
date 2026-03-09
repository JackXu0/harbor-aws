# harbor-aws

AWS ECS/Fargate execution backend for [Harbor](https://github.com/harbor-framework/harbor) benchmarks.

Deploy once, run any Docker-based benchmark elastically on Fargate. Pay only for compute — $0 when idle.

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
uv run harbor run -d terminal-bench@2.0 -a swe-agent -m bedrock/zai.glm-4.7 -n 89 --environment-import-path harbor_aws.environment:AWSEnvironment```

Other agent/model combinations:

```bash
# Sonnet 4 via Bedrock with swe-agent
uv run harbor run -d terminal-bench@2.0 -a swe-agent -m bedrock/anthropic.claude-sonnet-4-20250514-v1:0 -n 89 --environment-import-path harbor_aws.environment:AWSEnvironment
# Sonnet 4 via API key with aider
export ANTHROPIC_API_KEY=sk-ant-...
uv run harbor run -d terminal-bench@2.0 -a aider -m anthropic/claude-sonnet-4-20250514 -n 89 --environment-import-path harbor_aws.environment:AWSEnvironment```

### 4. Clean Up

```bash
uv run python -m harbor_aws destroy
```

Fully removes everything — running tasks, stack, task definitions. Nothing left behind.

## Prerequisites

- [uv](https://docs.astral.sh/uv/getting-started/installation/)
- AWS CLI configured with credentials

## Cost

| State | Cost |
|---|---|
| Idle (deployed, no tasks running) | ~$0/month |
| Running benchmarks | Fargate on-demand per task-second |

No NAT gateways, no EC2 instances.

## CLI Reference

```
uv run python -m harbor_aws <command> [options]

Commands:
  deploy    Deploy infrastructure (idempotent)
  status    Show stack status and outputs
  stop      Stop all running tasks (keeps infrastructure)
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
│  Harbor CLI  │────▶│ AWSEnvironment │────▶│ ECS Fargate  │
│             │     │   (adapter)    │     │ (on-demand)  │
└─────────────┘     └────────────────┘     └──────────────┘
                            │                     │
                            ▼                     ▼
                     ┌─────────────┐       ┌─────────────┐
                     │  S3 Bucket  │◀─────▶│   Daemon    │
                     │  (commands) │       │ (polls S3)  │
                     └─────────────┘       └─────────────┘
```

Commands are submitted as JSON to S3. A daemon in each container polls for commands, executes them, and uploads results back to S3. No SSM or ECS Exec needed.

Infrastructure defined as CDK in `src/harbor_aws/cdk/stack.py`. Deployment synthesizes in-memory and deploys via CloudFormation — no CDK CLI needed.

## Monitoring

A CloudWatch dashboard (`harbor-aws-monitor`) is created with the stack:

- **ECS Fargate** — running/pending task counts
- **Bedrock** — tokens, invocations, errors, throttles, latency (per model)

View at: `https://us-east-1.console.aws.amazon.com/cloudwatch/home?region=us-east-1#dashboards:name=harbor-aws-monitor`

## Development

```bash
uv sync --extra dev --extra cdk
uv run pytest
uv run ruff check src/
uv run mypy src/
```
