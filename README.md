# harbor-aws

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

AWS EKS/Fargate execution backend for [Harbor](https://github.com/harbor-framework/harbor) benchmarks — one pod per task, isolated, ephemeral, pay-on-demand.

![Architecture](https://raw.githubusercontent.com/JackXu0/harbor-aws/main/docs/architecture.png)

## Install

```bash
uv sync --extra cdk
```

## Quick Start

```bash
# Deploy infrastructure (one-time, ~15 min)
uv run python -m harbor_aws deploy

# Run benchmarks
uv run harbor run -c job-config.yaml \
  -d terminal-bench@2.0 \
  -a terminus-2 \
  -m bedrock/converse/moonshotai.kimi-k2.5 \
  -n 89

# Clean up
uv run python -m harbor_aws stop      # delete pods, keep infra
uv run python -m harbor_aws destroy   # delete everything
```

The `job-config.yaml` wires up the AWS environment:

```yaml
environment:
  import_path: "harbor_aws.adapter:AWSEnvironment"
  kwargs:
    stack_name: harbor-aws
    region: us-east-1
```

> **Prerequisites:** AWS account with admin access. Docker Hub login (`docker login`) recommended to avoid anonymous pull rate limits.

## Scaling

Image pulls are capped at 50 concurrent operations by default to avoid Docker Hub rate limiting. For higher sustained concurrency, configure Amazon ECR pull-through cache for Docker Hub images.

<details>
<summary><strong>ECR pull-through cache setup</strong></summary>

To use ECR pull-through cache for Docker Hub images:

#### 1. Create the Docker Hub secret in AWS Secrets Manager

This must be done before running `deploy`:

```bash
aws secretsmanager create-secret \
  --name ecr-pullthroughcache/docker-hub \
  --secret-string '{"username":"YOUR_DOCKERHUB_USER","accessToken":"YOUR_ACCESS_TOKEN"}' \
  --region us-east-1
```

The CDK stack automatically creates the ECR cache rule using this secret.

**2. Enable in job config:**

```yaml
environment:
  import_path: "harbor_aws.adapter:AWSEnvironment"
  kwargs:
    stack_name: harbor-aws
    region: us-east-1
    ecr_cache: true
```

</details>

## Validation

Benchmarks reproduced from the [Kimi K2.5 technical report](https://arxiv.org/abs/2504.05861) using Kimi K2.5 on Amazon Bedrock with [terminus-2](https://github.com/harbor-framework/terminus-2).

| Benchmark | Official | harbor-aws |
|---|:---:|:---:|
| SWE-bench Verified | 76.8% | 71.5% |
| Terminal-Bench 2.0 | 50.8% | 43.8% |
| GPQA-Diamond | 87.6% | 79.8% |
| LiveCodeBench v6 | 85.0% | 88.6% |
| SWE-bench Pro | 50.7% | 29.9% |

> Score gaps are expected — official results used Kimi's internal agent for some benchmarks, while we use terminus-2 throughout.

## Documentation

- [System Architecture & Design Principles](https://hammerhead-floor-229.notion.site/Harbor-AWS-System-Architecture-Design-Principles-322c2bfbdd1781b997dad4c5e54b2ee7) — architecture overview, tradeoffs, and design rationale

## Development

```bash
uv sync --extra dev --extra cdk
uv run ruff check src/
uv run mypy src/
```

## License

Apache License 2.0 — see [LICENSE](LICENSE).
