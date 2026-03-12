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
  import_path: "harbor_aws.environment:AWSEnvironment"
  kwargs:
    stack_name: harbor-aws
    region: us-east-1
```

**Prerequisites:** [uv](https://docs.astral.sh/uv/getting-started/installation/), an AWS account with admin access, and [Docker Hub Pro](https://www.docker.com/pricing/) ($11/mo) for high-concurrency image pulls.

## Benchmark Examples

All benchmarks below use [Kimi K2.5](https://arxiv.org/abs/2504.05861) via Amazon Bedrock with the [terminus-2](https://github.com/harbor-framework/terminus-2) agent at maximum concurrency.

```bash
# Terminal-Bench 2.0 (89 tasks)
uv run harbor run -c job-config.yaml \
  -d terminal-bench@2.0 -a terminus-2 \
  -m bedrock/converse/moonshotai.kimi-k2.5 -n 89

# SWE-bench Verified (500 tasks)
uv run harbor run -c job-config.yaml \
  -d swebenchverified@1.0 -a terminus-2 \
  -m bedrock/converse/moonshotai.kimi-k2.5 -n 500

# GPQA-Diamond (198 tasks)
uv run harbor run -c job-config.yaml \
  -d gpqa-diamond@1.0 -a terminus-2 \
  -m bedrock/converse/moonshotai.kimi-k2.5 -n 198

# LiveCodeBench v6 (100 tasks)
uv run harbor run -c job-config.yaml \
  -d livecodebench@6.0 -a terminus-2 \
  -m bedrock/converse/moonshotai.kimi-k2.5 -n 100

# SWE-bench Pro (731 tasks)
uv run harbor run -c job-config.yaml \
  -d swebenchpro@1.0 -a terminus-2 \
  -m bedrock/converse/moonshotai.kimi-k2.5 -n 731
```

## Validation Results

We ran the benchmarks from the [Kimi K2.5 technical report](https://arxiv.org/abs/2504.05861) to validate harbor-aws against official scores. All runs used Kimi K2.5 on Bedrock with terminus-2 at full concurrency.

| Benchmark | Official (Kimi K2.5) | harbor-aws | Agent | Notes |
|---|---|---|---|---|
| SWE-bench Verified | 76.8% (internal agent) | 71.5% | terminus-2 | Official used internal agent, not terminus-2 |
| Terminal-Bench 2.0 | 50.8% | 43.8% | terminus-2 | Official used terminus-2 |
| GPQA-Diamond | 87.6% | 79.8% | terminus-2 | |
| LiveCodeBench v6 | 85.0% | 88.6% | terminus-2 | Exceeds official score |
| SWE-bench Pro | 50.7% (internal agent) | _in progress_ | terminus-2 | |

> **Note:** Score differences are expected. Official Kimi K2.5 results used their internal agent for SWE-bench tasks, while we use the open-source terminus-2 agent throughout. LiveCodeBench v6 exceeded the official score. GPQA-Diamond and Terminal-Bench 2.0 gaps likely reflect agent differences rather than infrastructure issues.

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
