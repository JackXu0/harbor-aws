# harbor-aws

AWS EKS/Fargate execution backend for [Harbor](https://github.com/harbor-framework/harbor) benchmarks. Run every task in parallel — deploy in one command, pay only when running.

| | |
|---|---|
| **One-command setup** | `python -m harbor_aws deploy` provisions VPC, EKS, IAM, and logging. No Kubernetes or AWS expertise needed. |
| **Max concurrency** | All tasks across all benchmarks run in parallel. No queuing, no waiting. |
| **Pay-as-you-go** | Fargate pods bill per-second. No idle compute. |
| **One-command teardown** | `python -m harbor_aws destroy` removes everything. Nothing left behind. |

## How It Works

1. `python -m harbor_aws deploy` creates a shared EKS cluster and networking via CloudFormation.
2. When Harbor runs a benchmark, each task gets its own Fargate pod with a prebuilt Docker image.
3. Harbor executes commands and transfers files to/from pods over Kubernetes WebSocket.
4. Pods are deleted after each task. The cluster stays up for the next run.

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

**Prerequisites:** [uv](https://docs.astral.sh/uv/getting-started/installation/), an AWS account with admin access, and [Docker Hub Pro](https://www.docker.com/pricing/) ($11/mo) for high-concurrency image pulls.

## Cost

~$0.15/hr fixed (EKS + NAT) + ~$0.07/hr per running pod. Nothing else when idle.

## Development

```bash
uv sync --extra dev --extra cdk
uv run pytest
uv run ruff check src/
uv run mypy src/
```

## License

[MIT](LICENSE)
