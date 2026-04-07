# harbor-aws

AWS EKS/Fargate execution backend for [Harbor](https://github.com/harbor-framework/harbor) benchmarks. Designed for maximum concurrency — run thousands of benchmark trials in parallel without `kubectl exec` in the data path.

## Architecture

- **CDK stack** (`src/harbor_aws/cdk/stack.py`) is the single source of truth for the harbor-aws cluster infrastructure (VPC, EKS, IAM)
- **Pod-initiated outbound runner** (`src/harbor_aws/runner.py`) — each Fargate trial pod runs this stdlib-only TCP server as PID 1, mounted via a ConfigMap. The runner authenticates an inbound TCP connection from the harbor-control gateway and runs commands in a long-lived bash subprocess.
- **In-cluster control gateway** (`src/harbor_aws/server.py`) — a single Deployment in the harbor namespace exposed via a Service of type LoadBalancer (NLB). Routes commands from the orchestrator (running on a laptop or anywhere else with outbound HTTPS) to the appropriate trial pod over direct in-VPC TCP. The K8s apiserver is **not in the data path** — it's only used for `create_pod` / `delete_pod`.
- **Adapter** (`src/harbor_aws/adapter.py`) — Harbor `BaseEnvironment` implementation. Talks to the control gateway via aiohttp using one process-wide shared `ClientSession`.

### Why no kubectl exec
The original architecture used `kubectl exec` WebSockets, which routes through the K8s API server to the kubelet. At >2000 concurrent trials this path becomes unreliable: the apiserver→kubelet TLS dialer returns sporadic 500s and the failure rate climbs to 25%+ even with retries. Layer 3 eliminates that path entirely; the data plane is plain TCP between two Fargate pods in the same VPC.

### Cost
- EKS control plane: ~$73/mo (fixed)
- Fargate trial pods: pay-per-second only when running
- harbor-control pod: ~$5/mo idle (1 always-on Fargate pod)
- NLB: ~$16/mo
- VPC, IAM, CloudWatch: negligible

## Project Structure

```
src/harbor_aws/
├── __init__.py              # Exports: AWSConfig, AWSEnvironment
├── __main__.py              # CLI: python -m harbor_aws deploy|status|stop|destroy
├── adapter.py               # Harbor BaseEnvironment adapter
├── runner.py                # Pod-side stdlib TCP server (mounted via ConfigMap)
├── server.py                # In-cluster control gateway (packaged into harbor-control image)
├── cdk/
│   ├── stack.py             # CDK stack (VPC, EKS, IAM)
│   └── deploy.py            # CDK synth → CloudFormation JSON → boto3 deploy
└── core/
    ├── config.py            # AWSConfig dataclass, k8s client factory, stack loader
    ├── pods.py              # Pod lifecycle (create with ConfigMap mount, wait, delete)
    ├── remote_shell.py      # Adapter-side wrapper around the control gateway HTTP API
    └── watcher.py           # Watch-based pod status monitor (O(1) API calls)
```

## Quick Start

```bash
# Install (orchestrator side)
pip install -e ".[cdk]"

# Deploy your own harbor-aws cluster (one-time, ~15-20 minutes)
python -m harbor_aws deploy --region us-east-1

# Bootstrap the L3 control plane in the cluster:
#   1. Build & push the control server image
#   2. Apply the harbor-control Deployment + LoadBalancer Service
#   3. Apply the harbor-runner ConfigMap with src/harbor_aws/runner.py
#   4. Install AWS Load Balancer Controller if not already present
# (See the layer3-outbound-runner branch's commit history for the exact recipe.)

# Point Harbor at the adapter:
HARBOR_CONTROL_URL=https://<harbor-control-nlb-dns>:8443 \
HARBOR_ADMIN_TOKEN=<token> \
harbor jobs start -p ./task -a nop -n 2500 \
  --environment-import-path harbor_aws.adapter:AWSEnvironment \
  --ek stack_name=harbor-aws \
  --ek skip_image_check=true \
  --disable-verification --max-retries 2

# Check status / clean up / tear down
python -m harbor_aws status
python -m harbor_aws stop      # delete trial pods, keep infra
python -m harbor_aws destroy   # delete everything
```

## Build & Dev

```bash
pip install -e ".[dev,cdk]"
ruff check src/
mypy src/
```

## Conventions

- Python 3.12+, async/await throughout (boto3 + kubernetes client via `asyncio.to_thread()`, aiohttp for the control gateway)
- `tenacity` for retries on AWS / K8s API calls
- Strict typing: `mypy --disallow-untyped-defs`, PEP 561
- Ruff: line-length 120, rules B/E/F/I/N/UP/W
- `aws-cdk-lib` is an optional dependency (`[cdk]` extra) — only needed for `python -m harbor_aws deploy`
- The `runner.py` script is intentionally **stdlib-only** (no aiohttp / no requests / no pip-installable deps) so it runs inside any base image with Python 3.8+ without needing image rebuilds
