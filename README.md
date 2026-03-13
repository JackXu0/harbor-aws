# harbor-aws

Harbor-AWS is an execution backend for [Harbor](https://github.com/harbor-framework/harbor) that runs benchmark tasks as isolated ephemeral pods on EKS Fargate. It is designed for bursty, high-parallelism workloads such as SWE-bench and Terminal-bench, where per-task isolation and pay-on-demand compute matter more than low startup latency.

![Architecture](https://raw.githubusercontent.com/JackXu0/harbor-aws/main/docs/architecture.png)

## When to use harbor-aws

harbor-aws is a good fit when:
- A run consists of many mostly independent tasks
- Each task should execute in a clean, isolated environment
- Workloads are bursty rather than continuous
- You want to avoid managing a long-lived worker fleet

It is less suitable for:
- Latency-sensitive interactive workloads
- Very short tasks where startup overhead dominates
- Workloads that need custom node-level tuning or local caching

## How it works

1. `python -m harbor_aws deploy` provisions shared AWS infrastructure — networking, EKS, IAM, and logging — via CloudFormation.
2. Each Harbor benchmark task is mapped to its own ephemeral Fargate pod.
3. Harbor transfers files and executes commands through Kubernetes exec (WebSocket).
4. Pods are deleted when tasks complete. The shared infrastructure can be reused for later runs.

## Quick start

### Prerequisites

- An AWS account with admin access
- [Docker Hub Pro](https://www.docker.com/pricing/) ($11/mo) for high-concurrency image pulls

### Install

```bash
uv sync --extra cdk
```

### Deploy infrastructure (one-time, ~15 min)

```bash
uv run python -m harbor_aws deploy
```

### Run benchmarks

```bash
uv run harbor run -c job-config.yaml \
  -d terminal-bench@2.0 \
  -a terminus-2 \
  -m bedrock/converse/moonshotai.kimi-k2.5 \
  -n 89
```

### Clean up

```bash
uv run python -m harbor_aws status    # check running pods
uv run python -m harbor_aws stop      # delete pods, keep infra
uv run python -m harbor_aws destroy   # delete everything
```

## Configuration

The `job-config.yaml` wires up the AWS environment so you don't need `--environment-import-path` on every invocation:

```yaml
environment:
  import_path: "harbor_aws.adapter:AWSEnvironment"
  kwargs:
    stack_name: harbor-aws
    region: us-east-1
```

## Tradeoffs

This design favors isolation and operational simplicity, but it has real costs:

- **Startup latency.** Pod scheduling on Fargate adds non-trivial latency before a task begins.
- **Control-plane dependence.** Lifecycle management, exec sessions, and file transfer all go through Kubernetes API paths. The control plane can become a bottleneck before raw compute capacity is exhausted.
- **Overhead for small tasks.** One-pod-per-task execution is inefficient when tasks are very short-lived, since startup and teardown may dominate useful work.
- **Less low-level control.** Fargate reduces infrastructure management burden, but gives less room for custom scheduling or runtime tuning than self-managed nodes.

## Validation

To validate harbor-aws, we reproduced benchmarks from the [Kimi K2.5 technical report](https://arxiv.org/abs/2504.05861) using Kimi K2.5 on Amazon Bedrock with the [terminus-2](https://github.com/harbor-framework/terminus-2) agent.

| Benchmark | Official | harbor-aws |
|---|:---:|:---:|
| SWE-bench Verified | 76.8% | 71.5% |
| Terminal-Bench 2.0 | 50.8% | 43.8% |
| GPQA-Diamond | 87.6% | 79.8% |
| LiveCodeBench v6 | 85.0% | 88.6% |
| SWE-bench Pro | 50.7% | 29.9% |

> Score gaps are expected — official Kimi K2.5 results used their internal agent for some benchmarks (SWE-bench Verified, SWE-bench Pro), while we use terminus-2 throughout.

## Cost

Approximate baseline cost is ~$0.15/hr for shared infrastructure (EKS control plane and NAT gateway), plus ~$0.07/hr per running Fargate pod. Compute cost scales with active tasks.

## Development

```bash
uv sync --extra dev --extra cdk
uv run ruff check src/
uv run mypy src/
```

## License

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
