# harbor-aws

AWS EKS/Fargate execution backend for [Harbor](https://github.com/harbor-framework/harbor) benchmarks. Designed for maximum concurrency — run thousands of benchmark trials in parallel without `kubectl exec` in the data path.

## North Star: fail fast on infrastructure bugs

**Misconfiguration should surface loudly at startup, not be papered over with silent fallbacks.** Required env vars, stack outputs, K8s resources, and CDK-provisioned defaults are part of the infrastructure contract — if any is missing, raise immediately with a clear message. Do **not** generate fake values, retry indefinitely, or degrade silently for things the operator was responsible for getting right.

Apply to: missing env vars (`HARBOR_NLB_URL`, `HARBOR_BEARER_TOKEN`), missing CloudFormation outputs, missing K8s Services/ConfigMaps, missing IRSA bindings, wrong namespace, unreachable control pod at deploy time.

Do **not** apply to: transient runtime conditions (NLB connection blip, Fargate scheduling delay, K8s API rate limits) — those still warrant bounded retry with backoff (`tenacity`), because they're recoverable.

Rule of thumb: if a healthy deploy guarantees the value exists, missing-at-runtime means the deploy is broken; surface that bug rather than hide it.

## Architecture

- **CDK stack** (`src/harbor_aws/cdk/stack.py`) is the single source of truth for the harbor-aws cluster infrastructure (VPC, EKS, IAM, harbor-control Deployment + NLB Service, harbor-runner ConfigMap, AWS Load Balancer Controller).
- **Pod-side bash runner** (`src/harbor_aws/runner.sh`) — each Fargate trial pod runs this ~80-line bash script as PID 1, mounted via a ConfigMap. It uses bash's built-in `/dev/tcp` to dial *out* to the control pod, authenticates with `HARBOR_TRIAL_TOKEN` + `HARBOR_TRIAL_ID`, then handles a command loop. No Python or extra binaries required in the trial image; if `bash` is missing (e.g. plain Alpine) a tiny POSIX-sh bootstrap installs it.
- **Control pod** (`src/harbor_aws/server.py`, packaged as the `harbor-control` Docker image under `docker/harbor-control/`) — a single Deployment in the `harbor` namespace. Exposes:
  - port **8443** as a Service of type LoadBalancer (NLB) for the orchestrator-side HTTPS API (`/register`, `/exec`, `/stop`, `/healthz`).
  - port **8444** as a ClusterIP Service for the inbound runner connections.
  Routes commands from the orchestrator (laptop or anywhere with outbound HTTPS) to the appropriate trial pod over the open TCP connection. The K8s apiserver is **not in the data path** — only `create_pod` / `delete_pod` go through it.
- **Adapter** (`src/harbor_aws/adapter.py`) — Harbor `BaseEnvironment` implementation. Talks to the control pod via aiohttp. The module-level `AdapterRuntime` singleton owns process-wide state (CloudFormation stack config, K8s client, aiohttp session, pod-create semaphore) so 2000+ concurrent `AWSEnvironment` instances share one connection pool, one DescribeStacks call, one K8s client.

### Why no kubectl exec
The original architecture used `kubectl exec` WebSockets, which routes through the K8s API server to the kubelet. At >2000 concurrent trials this path becomes unreliable: the apiserver→kubelet TLS dialer returns sporadic 500s and the failure rate climbs to 25%+ even with retries. The current design eliminates that path entirely; the data plane is plain TCP between the control pod and each trial pod, both in the same VPC.

### Why pod-initiated (reverse runner)
Earlier iterations had the control pod dialing pod IPs over in-VPC TCP, which forced the runner image to ship a Python TCP listener — and therefore Python in every trial image. Flipping the direction (runner dials control) lets the runner be ~80 lines of bash using built-in `/dev/tcp`, which works on essentially any image with `bash` (Ubuntu/Debian/RHEL/Amazon Linux/distroless-base). Alpine and other busybox-only images get bash installed by the bootstrap script.

### Cost
- EKS control plane: ~$73/mo (fixed)
- Fargate trial pods: pay-per-second only when running
- Control pod: ~$320/mo idle (1 always-on Fargate pod at Fargate's max — 16 vCPU / 120 GiB — sized for max concurrency, not cost)
- NLB: ~$16/mo
- VPC, IAM, CloudWatch: negligible

### Payload size limits
The `/exec` endpoint caps request bodies at `MAX_PAYLOAD_BYTES` (4 GiB, defined in `server.py`). All file transfer (`upload_file`, `upload_dir`, `download_file`, `download_dir`) is a single tar+base64 blob inside one `/exec` call — base64 inflates 4/3, so usable raw payload is ~3 GiB per call. No chunking; larger transfers fail with `413 Payload Too Large`.

When raising `MAX_PAYLOAD_BYTES`, also raise:
- `RUNNER_STREAM_LIMIT` in `server.py` (asyncio per-line buffer for the runner→control return path)
- Control pod memory in `cdk/stack.py` (sized for `concurrent_uploads × MAX_PAYLOAD_BYTES` with headroom)
- **Each task's** `memory_mb` in `task.toml` — the trial pod's `bash` runner holds the inbound base64 payload in a shell variable, so trial pods need roughly `1.5 × MAX_PAYLOAD_BYTES` RAM to absorb a max-size upload without OOM. harbor-aws does not enforce this; it's per-task config.

## Project Structure

```
src/harbor_aws/
├── __init__.py              # Exports: AWSEnvironment, ClusterConfig, TrialOptions
├── __main__.py              # CLI: python -m harbor_aws deploy|status|stop|destroy
├── adapter.py               # Harbor BaseEnvironment adapter + AdapterRuntime singleton
├── runner.sh                # Pod-side bash runner (dials the control pod via /dev/tcp)
├── server.py                # Control pod application (packaged into harbor-control image)
├── cdk/
│   ├── stack.py             # CDK stack (VPC, EKS, IAM, control pod Deployment + NLB)
│   ├── deploy.py            # CDK synth → CloudFormation JSON → boto3 deploy
│   └── destroy.py           # Stack teardown (handles EKS Fargate-profile ordering)
└── core/
    ├── config.py            # ClusterConfig (process-wide) + TrialOptions (per-trial), k8s client factory, stack loader
    ├── images.py            # 3-tier image resolution: task.toml → simple Dockerfile replay → docker build + ECR
    ├── pods.py              # Pod lifecycle (create with ConfigMap mount, wait, delete)
    ├── remote_shell.py      # Per-trial wrapper around the control pod's HTTP API
    └── watcher.py           # Watch-based pod status monitor (O(1) API calls)

docker/
└── harbor-control/
    └── Dockerfile           # python:3.12-slim + aiohttp + server.py
```

## Quick Start

```bash
# Install (orchestrator side)
pip install -e ".[cdk]"

# Deploy your own harbor-aws cluster (one-time, ~15-20 minutes).
# This single command provisions everything: VPC, EKS, the control pod
# Deployment, NLB Service, harbor-runner ConfigMap, and AWS Load Balancer
# Controller. It prints the HARBOR_NLB_URL and HARBOR_BEARER_TOKEN
# values you need below.
python -m harbor_aws deploy --region us-east-1

# Point Harbor at the adapter. The two env vars are emitted by the deploy
# command — copy them from its output.
HARBOR_NLB_URL=http://<nlb-dns>:8443 \
HARBOR_BEARER_TOKEN=<bearer-token> \
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

`HARBOR_NLB_URL` is the NLB DNS that fronts the control pod. `HARBOR_BEARER_TOKEN` is the bearer token the control pod checks on every request.

## Build & Dev

```bash
pip install -e ".[dev,cdk]"
ruff check src/
mypy src/
```

## Conventions

- Python 3.12+, async/await throughout (boto3 + kubernetes client via `asyncio.to_thread()`, aiohttp for the control-pod HTTP path)
- `tenacity` for retries on AWS / K8s API calls
- Strict typing: `mypy --disallow-untyped-defs`, PEP 561
- Ruff: line-length 120, rules B/E/F/I/N/UP/W
- `aws-cdk-lib` is an optional dependency (`[cdk]` extra) — only needed for `python -m harbor_aws deploy`
- The `runner.sh` script is intentionally **bash-only** (no Python, no extra binaries beyond `coreutils`) so it runs inside any image with `bash`. The pod's PID 1 is a tiny POSIX-sh bootstrap that auto-installs `bash` via `apk` / `apt-get` / `dnf` / `yum` if missing.
- Process-wide singletons (K8s client, aiohttp session, stack config Task, pod-create semaphore) live on `AdapterRuntime` in `adapter.py` rather than as class attributes on `AWSEnvironment`, so each adapter instance only carries per-trial state.
