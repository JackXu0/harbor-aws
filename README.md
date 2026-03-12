# harbor-aws

AWS EKS/Fargate execution backend for [Harbor](https://github.com/harbor-framework/harbor) benchmarks. Run every task in parallel — deploy in one command, pay only when running.

| | |
|---|---|
| **One-command setup** | `python -m harbor_aws deploy` provisions VPC, EKS, IAM, and logging. No Kubernetes or AWS expertise needed. |
| **Max concurrency** | All tasks across all benchmarks run in parallel. No queuing, no waiting. |
| **Pay-as-you-go** | Fargate pods bill per-second. No idle compute. |
| **One-command teardown** | `python -m harbor_aws destroy` removes everything. Nothing left behind. |

## Quick Start

```bash
# 1. Install
uv sync --extra cdk

# 2. Deploy infrastructure (one-time, ~15 min)
uv run python -m harbor_aws deploy

# 3. Run a benchmark
uv run harbor run \
  -d terminal-bench@2.0 \
  -a swe-agent \
  -m bedrock/anthropic.claude-sonnet-4-20250514-v1:0 \
  -n 89 \
  --environment-import-path harbor_aws.environment:AWSEnvironment

# 4. Tear down when done
uv run python -m harbor_aws destroy
```

**Prerequisites:** [uv](https://docs.astral.sh/uv/getting-started/installation/) and AWS CLI configured with credentials.

## Cost

| Component | Cost |
|---|---|
| EKS control plane | ~$73/mo (fixed) |
| NAT gateway | ~$32/mo (fixed) |
| Fargate pods | Per-second, only when running |
| VPC, IAM, CloudWatch | Negligible |

## CLI

```
python -m harbor_aws <command> [options]

Commands:
  deploy    Deploy infrastructure (idempotent)
  status    Show stack status and outputs
  stop      Stop all running pods (keeps infrastructure)
  destroy   Full cleanup — removes everything

Options:
  --stack-name   Stack name (default: harbor-aws)
  --region       AWS region (default: us-east-1)
  --profile      AWS CLI profile
  -y, --yes      Skip confirmation (destroy only)
  -v, --verbose  Verbose output
```

## Architecture

```
Harbor CLI ──▶ AWSEnvironment ──▶ EKS Fargate pods
                    │
              ┌─────┴─────┐
              │           │
        WebSocket exec  Tar-over-exec
         (commands)    (file transfer)
```

Infrastructure is defined as CDK in `src/harbor_aws/cdk/stack.py` and deployed via CloudFormation — no CDK CLI needed. Commands run via Kubernetes WebSocket exec; file transfer uses tar-over-exec (same as `kubectl cp`).

## Monitoring

A CloudWatch dashboard (`harbor-aws-monitor`) is created automatically:

- **EKS Fargate** — pod count, CPU, memory
- **Bedrock** — tokens, invocations, errors, throttles, latency per model

Pod logs: CloudWatch Logs at `/harbor-aws/harbor-aws` (7-day retention).

## Development

```bash
uv sync --extra dev --extra cdk
uv run pytest
uv run ruff check src/
uv run mypy src/
```

## License

[MIT](LICENSE)
