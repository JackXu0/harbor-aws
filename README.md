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

# 3. Run benchmarks (using job config)
uv run harbor run -c job-config.yaml \
  -d terminal-bench@2.0 \
  -a terminus-2 \
  -m bedrock/converse/moonshotai.kimi-k2.5 \
  -n 89

# 4. Check status / clean up / tear down
uv run python -m harbor_aws status
uv run python -m harbor_aws stop       # delete pods, keep infra
uv run python -m harbor_aws destroy    # delete everything
```

The `job-config.yaml` wires up the AWS environment so you don't need `--environment-import-path` every time:

```yaml
environment:
  import_path: "harbor_aws.adapter:AWSEnvironment"
  kwargs:
    stack_name: harbor-aws
    region: us-east-1
```

**Prerequisites:** An AWS account with admin access and [Docker Hub Pro](https://www.docker.com/pricing/) ($11/mo) for high-concurrency image pulls.

## Validation Results

To validate harbor-aws, we reproduced benchmarks from the [Kimi K2.5 technical report](https://arxiv.org/abs/2504.05861) using Kimi K2.5 on Amazon Bedrock with the [terminus-2](https://github.com/harbor-framework/terminus-2) agent.

| Benchmark | Official | harbor-aws |
|---|:---:|:---:|
| SWE-bench Verified | 76.8% | 71.5% |
| Terminal-Bench 2.0 | 50.8% | 43.8% |
| GPQA-Diamond | 87.6% | 79.8% |
| LiveCodeBench v6 | 85.0% | 88.6% |
| SWE-bench Pro | 50.7% | _in progress_ |

> Score gaps are expected — official Kimi K2.5 results used their internal agent for some benchmarks (SWE-bench Verified, SWE-bench Pro), while we use terminus-2 throughout.

## Cost

~$0.15/hr fixed (EKS + NAT) + ~$0.07/hr per running pod. Nothing else when idle.

## Development

```bash
uv sync --extra dev --extra cdk
uv run ruff check src/
uv run mypy src/
```

## License

[MIT](LICENSE)
