# harbor-aws

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

AWS EKS/Fargate execution backend for [Harbor](https://github.com/harbor-framework/harbor) benchmarks. Run thousands of sandbox trials in parallel with per-second billing and VM-level isolation.

## System Overview

![harbor-aws architecture](docs/architecture.png)

## Main Bottleneck

The main bottleneck of running Harbor benchmarks on AWS EKS Fargate is using Kubernetes `exec` as the command execution path. As concurrency grows, the EKS control plane can become the limiting factor instead of the underlying Fargate compute capacity.

## Solution

Harbor-aws exposes an in-cluster Harbor control service through a Network Load Balancer. The control service holds long-lived TCP connections from the trial pods (each runner dials out to the control pod). Benchmark commands are sent to the control service and then forwarded to the target pod over those connections — bypassing the AWS-managed Kubernetes control plane entirely.

## Install

```bash
pip install "harbor-aws[cdk]"
npm install -g aws-cdk
```

## Quick start

### 1. Deploy (~15 min, one-time)

```bash
cdk bootstrap
harbor-aws deploy
```

Creates VPC, EKS, control pod, NLB.

### 2. (Recommended at scale) ECR pull-through cache

Docker Hub rate-limits anonymous pulls (~100/6h per IP) and all Fargate pods share one NAT. The secret lets ECR mirror Docker Hub in-VPC so thousands of pods reuse one upstream pull. Create it any time — `harbor-aws deploy` (or a re-deploy) picks it up automatically:

```bash
aws secretsmanager create-secret \
  --name ecr-pullthroughcache/docker-hub \
  --secret-string '{"username":"<user>","accessToken":"<token>"}'
```

### 3. Set HARBOR_* env vars

```bash
eval $(harbor-aws env --stack-name harbor-aws --region us-east-1)
```

Sets `HARBOR_NLB_URL`, `HARBOR_BEARER_TOKEN`, `HARBOR_NLB_CERT` so the adapter can find and authenticate to the control pod.

### 4. Run benchmarks

```bash
# Example: terminal-bench with terminus-2 + Sonnet 4.6 via Bedrock, 89 concurrent trials.
harbor jobs start \
  --task-git-url https://github.com/laude-institute/terminal-bench \
  -a terminus-2 \
  -m bedrock/us.anthropic.claude-sonnet-4-6-v1:0 \
  --environment-import-path harbor_aws.adapter:AWSEnvironment \
  -n 89
```

### 5. Clean up

```bash
harbor-aws stop              # delete trial pods, keep cluster
harbor-aws destroy --force   # tear down everything
```

## Development

```bash
pip install -e ".[dev,cdk]"
ruff check src/
mypy src/
```

## License

Apache License 2.0 — see [LICENSE](LICENSE).
