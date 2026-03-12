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
| EKS control plane | ~$0.10/hr (fixed) |
| NAT gateway | ~$0.045/hr (fixed) |
| Fargate pod (1 vCPU, 4 GB) | ~$0.07/hr per pod |
| VPC, IAM, CloudWatch | Negligible |

> **Note:** High-concurrency runs pull many Docker images simultaneously. A [Docker Hub Pro subscription](https://www.docker.com/pricing/) ($11/mo) is recommended to avoid rate limits. AWS credentials must remain valid for the duration of the run (~1 hour expiry by default).

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
