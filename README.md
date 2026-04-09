# harbor-aws

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

AWS EKS/Fargate execution backend for [Harbor](https://github.com/harbor-framework/harbor) benchmarks. Designed for high-concurrency runs (thousands of trials in parallel) without `kubectl exec` in the data path.

- **Pay-on-demand execution:** Cost scales with benchmark demand. Fargate per-second billing for trial pods.
- **High-concurrency execution:** 2,400+ commands/sec sustained throughput verified at 2,492 concurrent trials.
- **No kubectl exec data path:** Trial pods run a small bash runner (`runner.sh`) that dials the in-cluster gateway (`server.py`) over `/dev/tcp`. The gateway bridges to the orchestrator over an NLB. The K8s apiserver is only used for `create_pod` / `delete_pod`.
- **Runs on any image with bash:** No Python needed in the trial image. The bootstrap installs `bash` via apk/apt-get/dnf/yum on minimal images that don't have it.

## Install

```bash
pip install harbor-aws

# CDK extras for `python -m harbor_aws deploy`
pip install "harbor-aws[cdk]"
```

## Quick Start

```bash
# 1. Deploy your harbor-aws cluster (one-time, ~15-20 min)
python -m harbor_aws deploy --region us-east-1

# 2. Bootstrap the L3 control plane in the cluster:
#    - Build & push the harbor-control image (Dockerfile under docker/harbor-control/)
#    - Apply Deployment for harbor-control with two ports: 8443 (HTTPS API)
#      and 8444 (runner accept)
#    - Apply Service(ClusterIP) exposing both ports + Service(LoadBalancer/NLB)
#      exposing 8443 to the orchestrator
#    - Apply ConfigMap from src/harbor_aws/runner.sh
#    - Install AWS Load Balancer Controller if not already there

# 3. Run benchmarks
HARBOR_CONTROL_URL=http://<harbor-control-nlb-dns>:8443 \
HARBOR_ADMIN_TOKEN=<your-token> \
harbor jobs start -p ./task -a nop -n 2500 \
  --environment-import-path harbor_aws.adapter:AWSEnvironment \
  --ek stack_name=harbor-aws \
  --ek ecr_cache=true \
  --disable-verification --max-retries 2

# 4. Clean up
python -m harbor_aws stop      # delete trial pods, keep infra
python -m harbor_aws destroy   # delete everything
```

> **Prerequisites:** AWS account with admin access. Docker Hub login (`docker login`) recommended to avoid anonymous pull rate limits when building task images.

## Validation

Benchmarks reproduced from the [Kimi K2.5 technical report](https://arxiv.org/abs/2504.05861) using Kimi K2.5 on Amazon Bedrock with [terminus-2](https://github.com/harbor-framework/terminus-2).

| Benchmark | Official | harbor-aws |
|---|:---:|:---:|
| SWE-bench Verified | 76.8% | 71.5% |
| Terminal-Bench 2.0 | 50.8% | 43.8% |
| GPQA-Diamond | 87.6% | 79.8% |
| LiveCodeBench v6 | 85.0% | 88.6% |

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
