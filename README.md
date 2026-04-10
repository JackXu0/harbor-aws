# harbor-aws

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

AWS EKS/Fargate execution backend for [Harbor](https://github.com/harbor-framework/harbor) benchmarks. Run thousands of sandbox trials in parallel with per-second billing and VM-level isolation.

## How it works

```
Orchestrator (laptop / CI)
    |  HTTPS via NLB
    v
harbor-control pod (in-cluster gateway)
    |  TCP via /dev/tcp
    v
Trial pods (one per task, Fargate)
```

Each trial pod runs a small **bash runner** (`runner.sh`) as PID 1 — no Python needed in the trial image. The runner dials the in-cluster **harbor-control** gateway over plain TCP. The orchestrator talks to harbor-control over an NLB. The K8s API server is only used for pod create/delete, never in the exec data path.

## Install

```bash
pip install "harbor-aws[cdk]"
```

## Quick start

### 1. Deploy (~15 min, one-time)

```bash
python -m harbor_aws deploy --region us-east-1
```

Creates everything: VPC, EKS, harbor-control, NLB, Load Balancer Controller. Prints `HARBOR_CONTROL_URL` and `HARBOR_ADMIN_TOKEN` when ready.

### 2. Run benchmarks

Copy the env vars from deploy output:

```bash
HARBOR_CONTROL_URL=http://<printed-nlb-hostname>:8443 \
HARBOR_ADMIN_TOKEN=<printed-token> \
harbor jobs start -p ./task -a nop -n 2500 \
  --environment-import-path harbor_aws.adapter:AWSEnvironment \
  --ek stack_name=harbor-aws --ek ecr_cache=true
```

### 3. Clean up

```bash
python -m harbor_aws stop      # delete trial pods, keep infra
python -m harbor_aws destroy   # tear down everything
```

## Cost

| Component | Cost |
|---|---|
| EKS control plane | ~$73/mo (fixed) |
| Fargate trial pods | per-second, only when running |
| harbor-control pod | ~$5/mo (always-on) |
| NLB | ~$16/mo |

## Development

```bash
pip install -e ".[dev,cdk]"
ruff check src/
mypy src/
```

## License

Apache License 2.0 — see [LICENSE](LICENSE).
