# harbor-aws

AWS EKS/Fargate execution backend for [Harbor](https://github.com/harbor-framework/harbor) benchmarks. Designed for maximum concurrency — run thousands of benchmark trials in parallel without `kubectl exec` in the data path.

## Architecture

- **CDK stack** (`src/harbor_aws/cdk/stack.py`) is the single source of truth for the harbor-aws cluster infrastructure (VPC, EKS, IAM).
- **Pod-side bash runner** (`src/harbor_aws/runner.sh`) — each Fargate trial pod runs this ~80-line bash script as PID 1, mounted via a ConfigMap. It uses bash's built-in `/dev/tcp` to dial *out* to the harbor-control server, authenticates with `HARBOR_TOKEN` + `HARBOR_TRIAL_ID`, then handles a command loop. No Python or extra binaries required in the trial image; if `bash` is missing (e.g. plain Alpine) a tiny POSIX-sh bootstrap installs it.
- **In-cluster control gateway** (`src/harbor_aws/server.py`, packaged as the `harbor-control` Docker image under `docker/harbor-control/`) — a single Deployment in the `harbor` namespace. Exposes:
  - port **8443** as a Service of type LoadBalancer (NLB) for the Mac-side HTTPS API (`/register`, `/exec`, `/stop`, `/healthz`).
  - port **8444** as a ClusterIP Service for the inbound runner connections.
  Routes commands from the orchestrator (laptop or anywhere with outbound HTTPS) to the appropriate trial pod over the open TCP connection. The K8s apiserver is **not in the data path** — only `create_pod` / `delete_pod` go through it.
- **Adapter** (`src/harbor_aws/adapter.py`) — Harbor `BaseEnvironment` implementation. Talks to the control gateway via aiohttp using one process-wide shared `ClientSession` (HTTP keepalive across all trials).

### Why no kubectl exec
The original architecture used `kubectl exec` WebSockets, which routes through the K8s API server to the kubelet. At >2000 concurrent trials this path becomes unreliable: the apiserver→kubelet TLS dialer returns sporadic 500s and the failure rate climbs to 25%+ even with retries. Layer 3 eliminates that path entirely; the data plane is plain TCP between the harbor-control pod and each trial pod, both in the same VPC.

### Why pod-initiated (reverse runner)
Earlier L3 iterations had the control server dialing pod IPs over in-VPC TCP, which forced the runner image to ship a Python TCP listener — and therefore Python in every trial image. Flipping the direction (runner dials control) lets the runner be ~80 lines of bash using built-in `/dev/tcp`, which works on essentially any image with `bash` (Ubuntu/Debian/RHEL/Amazon Linux/distroless-base). Alpine and other busybox-only images get bash installed by the bootstrap script.

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
├── runner.sh                # Pod-side bash runner (dials harbor-control via /dev/tcp)
├── server.py                # In-cluster control gateway (packaged into harbor-control image)
├── cdk/
│   ├── stack.py             # CDK stack (VPC, EKS, IAM)
│   └── deploy.py            # CDK synth → CloudFormation JSON → boto3 deploy
└── core/
    ├── config.py            # AWSConfig dataclass, k8s client factory, stack loader
    ├── pods.py              # Pod lifecycle (create with ConfigMap mount, wait, delete)
    ├── remote_shell.py      # Adapter-side wrapper around the control gateway HTTP API
    └── watcher.py           # Watch-based pod status monitor (O(1) API calls)

docker/
└── harbor-control/
    └── Dockerfile           # python:3.12-slim + aiohttp + server.py
```

## Quick Start

```bash
# Install (orchestrator side)
pip install -e ".[cdk]"

# Deploy your own harbor-aws cluster (one-time, ~15-20 minutes)
python -m harbor_aws deploy --region us-east-1

# Bootstrap the L3 control plane in the cluster:
#   1. Build & push the harbor-control image:
#        docker buildx build --platform linux/amd64 \
#          -f docker/harbor-control/Dockerfile \
#          -t <account>.dkr.ecr.<region>.amazonaws.com/harbor-control:latest --push .
#   2. Apply the harbor-control Deployment + ClusterIP Service (port 8444 for runners)
#      + LoadBalancer Service (port 8443 for the Mac).
#   3. Apply the harbor-runner ConfigMap from src/harbor_aws/runner.sh:
#        kubectl -n harbor create configmap harbor-runner \
#          --from-file=runner.sh=src/harbor_aws/runner.sh --dry-run=client -o yaml | kubectl apply -f -
#   4. Install AWS Load Balancer Controller if not already present.

# Point Harbor at the adapter:
HARBOR_CONTROL_URL=http://<harbor-control-nlb-dns>:8443 \
HARBOR_ADMIN_TOKEN=<token> \
harbor jobs start -p ./task -a nop -n 2500 \
  --environment-import-path harbor_aws.adapter:AWSEnvironment \
  --ek stack_name=harbor-aws \
  --ek ecr_cache=true \
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
- The `runner.sh` script is intentionally **bash-only** (no Python, no extra binaries beyond `coreutils`) so it runs inside any image with `bash`. The pod's PID 1 is a tiny POSIX-sh bootstrap that auto-installs `bash` via `apk` / `apt-get` / `dnf` / `yum` if missing.
