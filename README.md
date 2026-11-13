# Auton — Local AI Lab on k3d + AMD GPU

A self-contained, self-hosted AI stack on a single Linux box with an AMD GPU
(RX 6600 / `gfx1030` class). Everything runs inside a [k3d](https://k3d.io)
(k3s-in-Docker) cluster:

- **Ollama** serves `qwen2.5-coder:7b-instruct` with **full GPU offload**
  (Vulkan / Mesa RADV), models persisted on a PVC.
- **Auton** (`auton`) is an HTTP service that drives the model in a tool-calling
  loop against an **MCP server**. Besides filesystem + shell tools it can read
  and change its own Kubernetes namespace and manage **n8n** workflows.
- **Autonomous operations** are split across pods and a Redis queue: two
  read-only `auton-scanner` pods turn rule-based findings into repair tasks, two
  `auton-worker` pods execute the playbooks (the only writers), and one
  `auton-ops` pod runs the LLM jobs from the agent queue. Every mutation is
  audit-gated and the UI shows both queues under **Tasks**.
- **n8n** orchestrates the agent (manual trigger or `POST` webhook).
- Everything application-level lives in the **`auton`** namespace; the AMD
  device plugin stays in **`kube-system`**.

No cloud calls, no port-forwards required between services — they talk over
in-cluster DNS. All Services are **ClusterIP**; host access is via Traefik
ingress or `kubectl port-forward`.

---

## Quick start

The fastest path is the idempotent installer; it follows the runbook below and
ends with a verification pass (RBAC, token, provider, incident store):

```bash
./scripts/install.sh --create-cluster   # or --verify to only run the checks
```

Installer options (all steps check first, so re-running is safe):

| Flag | Effect |
|---|---|
| `--create-cluster` | Create the k3d cluster if it does not exist |
| `--skip-build` | Do not build/import `auton:local`; use the image already in the cluster |
| `--skip-model-pull` | Do not pull the Ollama model |
| `--no-ingress` | Skip the Traefik Ingress |
| `--verify` | Only run the verification checks (token, RBAC, provider, incidents) |
| `--dry-run` | Print the commands without changing anything |
| `--show-token` | Print the bearer token at the end |

Environment: `AUTON_IMAGE` (deploy a registry image instead of building
`auton:local`), `AUTON_CLUSTER` (default `ai-cluster`) and `AUTON_TOKEN`
(reuse a token instead of generating one). `./scripts/install.sh --help`
prints the same list.

If you prefer to see every step, the manual sequence is:

```bash
# 1. Cluster with GPU passthrough (80:80 is only needed for ingress)
k3d cluster create ai-cluster \
  -v /dev/kfd:/dev/kfd -v /dev/dri:/dev/dri \
  -p "80:80@loadbalancer"

# 2. GPU device plugin
kubectl apply -f yamls/k8s-ds-amdgpu-dp.yaml

# 3. Namespaces, API token, RBAC, model config
kubectl apply -f yamls/namespace.yaml
kubectl apply -f yamls/auton-apps.yaml
kubectl -n auton create secret generic auton-api-token \
  --from-literal=token="$(openssl rand -hex 24)"
kubectl apply -f yamls/auton-rbac.yaml
kubectl apply -f yamls/auton-model.yaml   # provider=ollama by default

# 4. Ollama + model
kubectl apply -f yamls/ollama-deployment.yaml
kubectl -n auton rollout status deploy/ollama
kubectl -n auton exec deploy/ollama -- ollama pull qwen2.5-coder:7b-instruct

# 5. Build + import the agent image, deploy the queue, API, scanners, workers
#    and the LLM executor
docker build -t auton:local ./mcp
k3d image import auton:local -c ai-cluster
kubectl apply -f yamls/redis.yaml                  # the queue must exist first
kubectl apply -f yamls/agent-deployment.yaml       # API + UI (producer only)
kubectl apply -f yamls/auton-ops-deployment.yaml   # LLM executor
kubectl apply -f yamls/auton-scanner-deployment.yaml
kubectl apply -f yamls/auton-worker-deployment.yaml
kubectl -n auton rollout status deploy/redis
kubectl -n auton rollout status deploy/auton
kubectl -n auton rollout status deploy/auton-ops
kubectl -n auton rollout status deploy/auton-scanner
kubectl -n auton rollout status deploy/auton-worker

# 6. n8n + ingress
kubectl apply -f yamls/n8n-deployment.yaml
kubectl apply -f yamls/ingress.yaml
```

The agent serves a UI at `/` (port-forward the service, paste the token):

```bash
kubectl -n auton port-forward svc/auton 8080:8080
# open http://localhost:8080/  →  "Set token"
```

> On many setups `docker`, `k3d` and `kubectl` need `sudo` (they talk to a
> root-owned Docker socket). Add `sudo` to each command if yours does.
>
> k3d publishes **only** the ports declared at cluster creation. ClusterIP
> services need no mappings, so `kubectl port-forward` is the safe default:
> `kubectl -n auton port-forward svc/auton 8080:8080`.

Send the agent a task (the `/health` probe is open; everything else needs the
bearer token):

```bash
TOKEN=$(kubectl -n auton get secret auton-api-token -o jsonpath='{.data.token}' | base64 -d)

curl -s http://localhost:8080/health
curl -s http://localhost:8080/run \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"task":"Create fib.py that prints the first 15 Fibonacci numbers, then run it."}' \
  | python3 -m json.tool
```

(Or via ingress: `curl -H 'Host: auton.localhost' http://localhost/run ...`.)

---

## Helm (Docker Hub images)

The same stack is packaged as a chart so it can be installed without this
repository. Images and the chart are published under `d3vhex` on Docker Hub by
`scripts/publish.sh` (`docker.io/d3vhex/auton`, plus
`oci://registry-1.docker.io/d3vhex/charts/auton`):

```bash
helm install auton oci://registry-1.docker.io/d3vhex/charts/auton \
  --version 0.2.0 --namespace auton --create-namespace \
  --set agent.auth.token="$(openssl rand -hex 24)"
```

From a checkout, `helm install auton ./charts/auton ...` works the same way.
The chart creates the `auton`/`auton-apps` namespaces, RBAC, the audit PVC, the
queue, the API, the scanners, the repair workers and the LLM executor, and
optionally Ollama (with a model-pull Job), n8n and ingress.
`charts/auton/README.md` documents every value, remote-provider examples,
multi-node claims and `--take-ownership` adoption of an existing k3d install.

To publish your own build:

```bash
DOCKERHUB_USER=<user> DOCKERHUB_TOKEN=<token> ./scripts/publish.sh
```

`--no-push` only builds the image and packages the chart (`dist/`).

---

## Architecture

```
                     host (Linux, AMD RX 6600 / gfx1030)
  ┌──────────────────────────────────────────────────────────────────────┐
  │  k3d cluster "ai-cluster" (k3s)                                      │
  │                                                                      │
  │  ns: kube-system              ns: auton                              │
  │  ┌──────────────────┐         ┌───────────────────────────────────┐  │
  │  │ amdgpu device    │         │ ollama  :11434   (amd.com/gpu: 1) │  │
  │  │ plugin DaemonSet │──alloc──▶│   └─ PVC ollama-models  20Gi     │  │
  │  └──────────────────┘  GPU    │                                   │  │
  │                               │ redis :6379  (ClusterIP)         │  │
  │                               │   └─ PVC redis-data  1Gi (AOF)    │  │
  │                               │   streams · locks · cooldown ·    │  │
  │                               │   hourly budget · GPU slot        │  │
  │                               │                                   │  │
  │                               │ auton :8080  (ClusterIP)          │  │
  │                               │   API + UI, producer only         │  │
  │                               │   SA auton-api (patch 3 objects)  │  │
  │                               │        │ enqueue                 │  │
  │                               │        ▼                          │  │
  │                               │  2× auton-scanner  SA: read-only  │  │
  │                               │     findings ─▶ repair queue      │  │
  │                               │  2× auton-worker   SA: playbooks  │  │
  │                               │     claim ─▶ audit ─▶ kubectl     │  │
  │                               │  1× auton-ops      SA auton-agent │  │
  │                               │     agent_worker ─▶ agent_core    │  │
  │                               │       └─ MCP stdio ─▶ agent_server│  │
  │                               │   shared PVC agent-workspace 5Gi  │  │
  │                               │   (audit log + incidents)         │  │
  │                               │                                   │  │
  │                               │ n8n :5678  (ClusterIP)            │  │
  │                               │   └─ PVC n8n-data  5Gi            │  │
  │                               └───────────────────────────────────┘  │
  └──────────────────────────────────────────────────────────────────────┘
       Traefik *.localhost:80   ·   kubectl port-forward for everything else
```

The dependency chain is one-way: **n8n → auton → ollama → GPU**. Ollama has no
dependency on the agent, so the GPU stack can be tested on its own. The queue is
the only thing the pod roles share:

- **`auton`** validates a request and, with `AGENT_QUEUE=redis`, enqueues an
  `agent` task and answers `202`. It never runs the model and never runs
  `kubectl`.
- **`auton-scanner`** snapshots `auton-apps` read-only every 60 s and enqueues a
  `repair` task per finding. Two replicas collapse duplicate findings on the
  queue's idempotency key.
- **`auton-worker`** claims repair tasks. Two workers repair *different*
  workloads in parallel; a per-resource lock serialises the same workload. Each
  task is **planned** and **policy-checked** before anything is changed: a plan
  the policy rates above the automatic tier is parked as an approval request and
  the incident waits in `AWAITING_APPROVAL` until a human decides.
- **`auton-ops`** claims `agent` tasks and runs the LLM loop. One replica, and a
  global GPU slot in Redis keeps two model calls from overlapping.

Four identities, one per pod role (`yamls/auton-rbac.yaml`):

| ServiceAccount | May do |
|---|---|
| `auton-api` | `patch` exactly `configmaps/auton-model-config` + `configmaps/auton-policy` + `secrets/auton-model` |
| `auton-scanner` | `get/list/watch` in `auton-apps` |
| `auton-worker` | read + delete pods + patch/scale Deployments in `auton-apps` |
| `auton-agent` | repair in `auton` + `auton-apps`, cluster read-only (LLM executor) |

RBAC, not the Python code, is the boundary. None of them can read `secrets`,
manage `serviceaccounts`, touch Roles/RoleBindings or delete PVCs/namespaces.

---

## Components

| Manifest (`yamls/`) | Namespace | What it creates | Image | Service |
|---|---|---|---|---|
| `k8s-ds-amdgpu-dp.yaml` | `kube-system` | `amdgpu-device-plugin-daemonset` (registers `amd.com/gpu`) | `rocm/k8s-device-plugin` | — |
| `gpu-test.yaml` | `auton` | One-shot `rocminfo` pod (GPU smoke test) | `rocm/rocm-terminal:latest` | — |
| `namespace.yaml` | — | `auton` namespace | — | — |
| `auton-apps.yaml` | — | `auton-apps` namespace + quota + LimitRange | — | — |
| `auton-rbac.yaml` | `auton`, `auton-apps` | SAs `auton-agent`/`auton-api`/`auton-scanner`/`auton-worker` + Roles/Bindings + cluster read-only | — | — |
| `auton-policy.yaml` | `auton` | The repair policy ConfigMap `auton-policy` (mode + `policy.json`), mounted into the API and the workers | — | — |
| `ollama-deployment.yaml` | `auton` | Ollama Deployment + PVC + **ClusterIP** | `ollama/ollama:latest` | `ollama-service:11434` |
| `redis.yaml` | `auton` | Queue Deployment + AOF PVC + **ClusterIP** | `redis:7-alpine` | `redis:6379` |
| `agent-deployment.yaml` | `auton` | API + UI Deployment (producer) + workspace PVC + **ClusterIP** | `auton:local` (built from `mcp/`) | `auton:8080` |
| `auton-scanner-deployment.yaml` | `auton` | 2 read-only scanners | `auton:local` | — |
| `auton-worker-deployment.yaml` | `auton` | 2 repair workers (shares `agent-workspace`) | `auton:local` | — |
| `auton-ops-deployment.yaml` | `auton` | LLM executor `agent_worker.py` (shares `agent-workspace`) | `auton:local` | — |
| `n8n-deployment.yaml` | `auton` | n8n Deployment + PVC + **ClusterIP** | `docker.n8n.io/n8nio/n8n:latest` | `n8n:5678` |
| `ingress.yaml` | `auton` | Traefik `IngressRoute` (`auton`/`agent`, `ollama`, `n8n`) | — | 80 |

All PVCs use the k3s `local-path` provisioner, so data survives pod restarts
but **not** cluster deletion. Every Deployment uses `strategy: Recreate`
because `agent-workspace` and `redis-data` are `ReadWriteOnce` (a rolling update
would deadlock trying to mount the same volume in two pods).

### Secrets

| Secret | Key | Used by | Notes |
|---|---|---|---|
| `auton-api-token` | `token` | API, scanners (none), workers, executor, n8n | Bearer token for `/run`, `/jobs*`, `/tasks*`, `/audit`, `/provider*`, `/incidents*`, `/policy`, `/approvals*`; also passed to n8n, the workers and the executor |
| `n8n-api` | `api-key` | Executor (and a local memory-mode API) | n8n public-API key; mounted at `/etc/n8n-api/api-key` and read **per call**, so rotation needs no restart (projected volume lags up to ~1 min; use `scripts/rotate-n8n-api-key.sh`) |
| `auton-model` | `api-key` | Executor (and the API for `/provider/test`) | Key for the active remote model provider (empty for local Ollama). Written by `scripts/set-provider.sh`, read per call |

The n8n volume is `optional: true`, so the agent pod starts before the key
exists and picks it up once you create the Secret (allow ~1 minute for the
kubelet to sync the volume, or restart the deployment). The model ConfigMap
(`auton-model-config`, in `yamls/auton-model.yaml`) holds the non-secret
provider settings.

---

## Repository layout

```
.
├── README.md                  ← you are here (overview + full runbook)
├── AGENTS.md                  short operating notes for coding agents
├── yamls/
│   ├── namespace.yaml         auton namespace
│   ├── auton-apps.yaml        autonomous-repair namespace + quota + LimitRange
│   ├── auton-rbac.yaml        SA + Roles (auton, auton-apps) + cluster read-only
│   ├── auton-model.yaml       provider ConfigMap (ollama/openai/…) + API-key Secret
│   ├── auton-policy.yaml      repair-policy ConfigMap (mode + policy.json)
│   ├── k8s-ds-amdgpu-dp.yaml  AMD GPU device plugin (kube-system)
│   ├── gpu-test.yaml          rocminfo smoke-test pod
│   ├── ollama-deployment.yaml Ollama + GPU + model PVC + ClusterIP
│   ├── ollama-deployment.yaml.orig  legacy pre-PVC version — do not apply
│   ├── redis.yaml             queue + AOF PVC + ClusterIP
│   ├── agent-deployment.yaml  Auton API/UI pod + workspace PVC + ClusterIP
│   ├── auton-scanner-deployment.yaml  read-only detection ×2
│   ├── auton-worker-deployment.yaml   repair playbooks ×2 (the writers)
│   ├── auton-ops-deployment.yaml      LLM executor ×1 (agent_worker.py)
│   ├── n8n-deployment.yaml    n8n + data PVC + ClusterIP
│   └── ingress.yaml           Traefik routes: auton/ollama/n8n.localhost
├── scripts/
│   ├── install.sh             idempotent bootstrap + verification
│   ├── publish.sh             build/push the Docker Hub image and chart
│   ├── rotate-n8n-api-key.sh  Secret update + projected-volume wait + verify
│   ├── set-provider.sh        switch model provider + verify (no restart)
│   └── set-policy.sh          switch the repair policy (mode/tiers/rules)
├── charts/auton/              Helm chart (API + scanner + worker + executor)
├── mcp/                       container for auton:local  (see mcp/README.md)
│   ├── agent_server.py        MCP stdio server (fs/shell/git/k8s/n8n/queue)
│   ├── audit.py               write-ahead audit log (JSONL + SQLite)
│   ├── llm.py                 provider layer (ollama/openai/gemini/anthropic/custom)
│   ├── provider_admin.py      validated provider writes (UI -> ConfigMap/Secret)
│   ├── k8s_tools.py           kubectl wrapper (13 tools, audit-gated)
│   ├── n8n_tools.py           n8n REST client (6 tools)
│   ├── ops_queue.py           memory/Redis queue backends
│   ├── task_store.py          task lifecycle, locks, cooldown, budget, GPU slot
│   ├── incidents.py           incident records v2 (fingerprint, timeline, 13 states)
│   ├── evidence.py            structured evidence collector (never reads a Secret)
│   ├── diagnosis.py           deterministic root causes + action allowlist
│   ├── policy.py              policy engine (modes, tiers, rules, fail closed)
│   ├── planner.py             repair planner (the RepairPlan schema)
│   ├── approvals.py           approval store (plans waiting for a human)
│   ├── playbooks.py           deterministic repairs (OOM/stuck/rollout/replicas)
│   ├── ops_controller.py      detection rules + scan_once/execute_task
│   ├── ops_scanner.py         scanner entry point (read-only, enqueues)
│   ├── ops_worker.py          worker entry point (claims repair tasks)
│   ├── agent_worker.py        LLM executor entry point (claims agent tasks)
│   ├── agent_core.py          dual-path tool loop, context budget, cancellation
│   ├── agent_client.py        CLI entry point
│   ├── agent_service.py       HTTP front end (UI, /run, /jobs, /tasks, /audit)
│   ├── ui/index.html          single-page UI served at "/" (no build step)
│   ├── Dockerfile             builds auton:local (git, ripgrep, bwrap, kubectl)
│   ├── requirements.txt       mcp>=2,<3 · ollama>=0.4 · pathspec>=0.12 · redis>=5
│   ├── tests/                 pytest suite (tools, loop, HTTP, k8s/n8n, queue, RBAC)
│   ├── eval/run_eval.py       end-to-end task harness (needs live Ollama)
│   └── README.md              deep dive on the agent
└── n8n/
    ├── ai-agent-demo.json     manual trigger → POST auton:8080/run
    └── ai-agent-webhook.json  POST /webhook/auton-agent → async job + polling
```

`ollama-deployment.yaml.orig` is a historical copy (emptyDir, default
namespace, no PVC). It is kept for reference only — **do not apply it**, or you
will create a second, GPU-competing Ollama in `default`.

The n8n JSON files use `=$env.AGENT_API_TOKEN` for the `Authorization` header.
Imported workflows are inactive: activate them in the UI (or let
`n8n_create_agent_pipeline` generate and activate one for you).

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Linux host | Tested on Fedora-style host; any distro with Docker works |
| Docker | k3d nodes are Docker containers |
| `k3d` + `kubectl` | `k3d` ≥ 5.x (`k3d cluster create --help` supports `-p ...@loadbalancer`) |
| AMD GPU | RX 6600 here (`gfx1032`, overridden to `gfx1030`), ≥ 8 GB VRAM recommended for a 7B model |
| `/dev/kfd` + `/dev/dri` | Must exist on the host and be mounted into the k3d node |
| ROCm userspace | For `rocminfo` / the device plugin's device discovery |
| Mesa RADV (Vulkan) | The path Ollama actually uses for inference on this card |

`HSA_OVERRIDE_GFX_VERSION=10.3.0` is set on Ollama and the test pod. It makes
ROCm treat the RX 6600 as `gfx1030`, which historically unlocked the ROCm
backend. On this setup inference runs over **Vulkan (Mesa RADV)** with full layer
offload, so the override is effectively redundant — it is kept because it is
harmless and helps if Ollama falls back to HIP.

---

## Full runbook

`scripts/install.sh` runs the steps below in order and verifies the result;
each section is kept here so a failure can be reproduced by hand.

### 1. Create the cluster

k3d only maps ports that are declared **at cluster creation** — you cannot add
them later without recreating the cluster.

```bash
k3d cluster create ai-cluster \
  -v /dev/kfd:/dev/kfd -v /dev/dri:/dev/dri \
  -p "80:80@loadbalancer"
```

- `-v /dev/kfd -v /dev/dri` — GPU device passthrough into the k3s node.
- `80:80@loadbalancer` — enables the Traefik ingress (`*.localhost` hosts).

No NodePorts are needed: every Service is ClusterIP. Use `kubectl port-forward`
(see [Access without port mappings](#access-without-port-mappings)).

If `kubectl` tries `localhost:8080`, merge the k3d context:

```bash
k3d kubeconfig merge ai-cluster --kubeconfig-merge-default --kubeconfig-switch-context
```

#### Restarting a stopped cluster

k3d runs the whole cluster as Docker containers. If the host suspends, the
Docker daemon is restarted, or the node containers are killed (`docker ps -a`
shows `k3d-ai-cluster-server-0 ... Exited (137)`), the kubeconfig context
disappears too. Start it again — PVC data and pulled models survive a
stop/start:

```bash
k3d cluster start ai-cluster
k3d kubeconfig merge ai-cluster --kubeconfig-merge-default --kubeconfig-switch-context
kubectl -n auton get pods,pvc
```

Only `k3d cluster delete` destroys the `local-path` volumes (and therefore the
model and workspaces); recreate in that case with
[step 1](#1-create-the-cluster).

### 2. Install the GPU device plugin

```bash
kubectl apply -f yamls/k8s-ds-amdgpu-dp.yaml
kubectl -n kube-system rollout status ds/amdgpu-device-plugin-daemonset
```

Verify the node advertises the resource:

```bash
kubectl get nodes -o jsonpath='{.items[0].status.capacity.amd\.com/gpu}{"\n"}'
# expected: 1
```

Then prove the GPU is actually usable from a pod:

```bash
kubectl apply -f yamls/gpu-test.yaml
kubectl -n auton wait --for=jsonpath='{.status.phase}'=Succeeded pod/rocm-test-pod --timeout=120s
kubectl -n auton logs rocm-test-pod | head -40
kubectl -n auton delete pod rocm-test-pod --ignore-not-found
```

`gpu-test.yaml` creates `rocm-test-pod` (restartPolicy `Never`) that runs
`rocminfo`. If it stays `Pending` with `0/1 nodes are available: ... amd.com/gpu`,
the plugin did not register — see [Troubleshooting](#troubleshooting).

### 3. Namespaces, tokens and RBAC

```bash
kubectl apply -f yamls/namespace.yaml
# The agent may repair workloads here without being asked. Quota + LimitRange
# keep a bad playbook from eating the node.
kubectl apply -f yamls/auton-apps.yaml

# Bearer token for the agent API (n8n gets the same token from this Secret).
kubectl -n auton create secret generic auton-api-token \
  --from-literal=token="$(openssl rand -hex 32)"

# ServiceAccount + Role/RoleBinding in both namespaces + cluster read-only.
kubectl apply -f yamls/auton-rbac.yaml

# Model provider config (ConfigMap + API-key Secret); ollama by default.
kubectl apply -f yamls/auton-model.yaml
```

The Role grants CRUD on pods, services, configmaps, workloads, jobs and
`*/scale` in `auton` and `auton-apps`, ingress create/update, and **not**
secrets, serviceaccounts, roles/rolebindings or PVC deletion. Cluster-scoped
access is read-only (`auton-cluster-reader`: nodes, PVs, CRDs, storage classes,
apiservices, metrics). See the comments in `yamls/auton-rbac.yaml` for why.

> Why PVC delete is blocked twice: data loss is irreversible. `k8s_tools.py`
> refuses it and returns a hint, and the Role has no `delete` verb on
> `persistentvolumeclaims`, so a bypassed tool still cannot do it. The correct
> behaviour is a report + a human running `kubectl delete pvc`.

### 4. Deploy Ollama and pull the model

```bash
kubectl apply -f yamls/ollama-deployment.yaml   # PVC + Deployment + Service
kubectl -n auton rollout status deploy/ollama

# Pull once; it lands on the 20Gi PVC and survives restarts.
kubectl -n auton exec deploy/ollama -- ollama pull qwen2.5-coder:7b-instruct

# Confirm the model is loaded and offloaded to the GPU.
kubectl -n auton exec deploy/ollama -- ollama ps
```

The service is reachable in-cluster as `ollama-service:11434`. From the host
use `kubectl -n auton port-forward svc/ollama-service 11434:11434`.

The single GPU is shared: stop any other Ollama (e.g. an old namespace) before
starting this one, or the new pod stays `Pending` on `amd.com/gpu`.

### 5. Build, import and deploy Auton

The agent image is built locally and imported into the cluster — there is no
registry:

```bash
docker build -t auton:local ./mcp
k3d image import auton:local -c ai-cluster
kubectl apply -f yamls/auton-policy.yaml
kubectl apply -f yamls/agent-deployment.yaml
kubectl apply -f yamls/auton-ops-deployment.yaml
kubectl apply -f yamls/auton-scanner-deployment.yaml
kubectl apply -f yamls/auton-worker-deployment.yaml
kubectl -n auton rollout status deploy/auton

# The queue must exist before the API: with AGENT_QUEUE=redis and no
# reachable Redis the service refuses to start (there is no memory fallback).
kubectl apply -f yamls/redis.yaml
kubectl -n auton rollout status deploy/redis

# Everything else (the LLM executor, scanners, repair workers).
kubectl -n auton rollout status deploy/auton-ops
kubectl -n auton rollout status deploy/auton-scanner
kubectl -n auton rollout status deploy/auton-worker
```

Rebuild + re-import +
`kubectl rollout restart deploy/auton deploy/auton-ops deploy/auton-scanner deploy/auton-worker`
whenever you change anything under `mcp/`. `imagePullPolicy: IfNotPresent`
makes every pod use the imported image. The workers and the executor share the
`agent-workspace` PVC (single-node k3d), so the audit log and incident files
they write are exactly the ones the API serves.

The image installs `git` (for `git_diff`/`git_status`), `ripgrep` (fast,
gitignore-aware search), `bubblewrap` (optional host sandbox) and **kubectl
v1.35.5** (matching the cluster's server minor) on top of the agent modules and
the `ui/` page. New Python modules must be added to the `COPY` line in
`mcp/Dockerfile` (and new pages under `ui/`), or they will be missing inside the
pod.

`yamls/auton-policy.yaml` creates the repair policy the workers read before
every action. It is optional in the sense that the pods start without it (the
policy volume is `optional: true`), but apply it — without it the engine uses
its built-in default, which is the same `safe-auto` policy, and `GET /policy`
reports `source: default` instead of `source: configmap`.

### 6. Deploy n8n and create its API key

```bash
kubectl apply -f yamls/n8n-deployment.yaml
kubectl -n auton rollout status deploy/n8n
```

Open the UI with `kubectl -n auton port-forward svc/n8n 5678:5678` →
`http://localhost:5678` (or `http://n8n.localhost` via ingress) and create the
owner account.

n8n has **no environment variable for public-API keys**. Create one once:

1. Settings → **n8n API** → *Create an API key*.
2. Give it the scopes the agent needs: `workflow:list`, `workflow:read`,
   `workflow:create`, `workflow:update`, `workflow:delete`,
   `workflow:activate`, `workflow:deactivate`, `execution:list`,
   `execution:read`.
3. Store it as a Secret (the agent reads the file per call):

   ```bash
   printf '%s' 'PASTE_API_KEY' > /tmp/n8n-api-key
   kubectl -n auton create secret generic n8n-api \
     --from-file=api-key=/tmp/n8n-api-key
   rm /tmp/n8n-api-key
   ```

The agent pod already mounts `/etc/n8n-api` as an **optional** Secret volume, so
it starts fine before the key exists. After creating the Secret, give the
kubelet a minute (or `kubectl -n auton rollout restart deploy/auton`) and check:

```bash
kubectl -n auton exec deploy/auton -- cat /etc/n8n-api/api-key
```

#### Rotating the n8n API key

The agent reads the file on **every** call, so a rotation never needs a pod
restart — but the mount is a *projected* Secret volume, and kubelet refreshes
those asynchronously (usually well under a minute, bounded by its sync period;
there is no immediate update signal). Automation handles both cases:

```bash
# 1. create the new key in the n8n UI (same scopes as above), keep the old one
#    active until this succeeds
printf '%s' 'NEW_API_KEY' > /tmp/n8n-api-key
chmod 600 /tmp/n8n-api-key

# 2. update the Secret, wait for the projected file, verify against n8n
scripts/rotate-n8n-api-key.sh --key-file /tmp/n8n-api-key
rm /tmp/n8n-api-key
```

The script applies Secret `n8n-api`, polls
`/etc/n8n-api/api-key` inside the pod until it matches (up to 150 s), restarts
`deploy/auton` only if the projection never catches up, then runs
`n8n_workflows("list")` from inside the pod to prove n8n accepts the key. Use
`--no-restart` to forbid the restart fallback, `--restart` to force it, and
`--skip-verify` when n8n is down. Finish by revoking the old key in the UI.

Because the agent fails soft here, a stale key shows up as
`n8n tools: no n8n API key available` (missing file) or a `401`/`403` from n8n —
both point at the Secret, not at the agent code.

Then either **Import from File**:

- `n8n/ai-agent-demo.json` — manual trigger → `POST http://auton:8080/run`
  with a hardcoded demo task.
- `n8n/ai-agent-webhook.json` — exposes `POST /webhook/auton-agent`; starts the
  task with `?async=1`, polls `/jobs/{id}` every 5 s and responds when the job
  is `done`/`error`/`cancelled`.

…or let the agent generate and activate the same polling pipeline:

```bash
kubectl -n auton exec deploy/auton -- python -c \
  'import n8n_tools; print(n8n_tools.n8n_create_agent_pipeline("Auton agent", "auton-agent"))'
```

The generated graph is fixed (webhook → start async job → wait → get job → IF →
respond/loop), so a slow 7B run never depends on a single long-lived HTTP
request. `Done?` checks `status` against `done`/`error`/`cancelled`; the false
branch loops back to the 5 s Wait. The workflow's `executionTimeout` is 3600 s.

### 7. Ingress (optional)

```bash
kubectl apply -f yamls/ingress.yaml
curl -s -H 'Host: auton.localhost'  http://localhost/health
curl -s -H 'Host: ollama.localhost' http://localhost/api/tags
curl -s -H 'Host: n8n.localhost'    http://localhost/healthz
```

Requires the cluster to have been created with `-p "80:80@loadbalancer"`.
The routes are plain `Host(...)` matches on the Traefik `web` entrypoint.
`agent.localhost` is kept as an alias for `auton.localhost`.

---

## Services and ports

| Service | In-cluster DNS | Type | Ingress host | Health |
|---|---|---|---|---|
| Ollama | `ollama-service:11434` | ClusterIP | `ollama.localhost` | `GET /` |
| Auton | `auton:8080` | ClusterIP | `auton.localhost` (alias `agent.localhost`) | `GET /health`, UI at `/` |
| n8n | `n8n:5678` | ClusterIP | `n8n.localhost` | `GET /healthz` |

---

## Using the agent

### Authentication

`/health`, `/` and `/ui` are open (`/` and `/ui` serve the page, which holds no
data). Every API call needs:

```
Authorization: Bearer <token from the auton-api-token Secret>
```

The service **refuses to start** without `AGENT_API_TOKEN` unless
`AGENT_AUTH_DISABLED=1`. A missing token is never interpreted as "open": with
no token configured the handler answers `503`. A wrong or missing token answers
`401` with `WWW-Authenticate: Bearer`. n8n receives the same token via its own
`AGENT_API_TOKEN` env var (from the Secret) and the generated workflow sends it
with the `$env.AGENT_API_TOKEN` expression.

`GET /auth/check` is the cheap probe the UI uses before saving a token: it
requires the token and returns `{"ok": true, "auth": "bearer"}` when it is
accepted, so the browser can tell "wrong token" from "API down".

### UI (`/`)

The service bundles a single-page UI (no build step, no CDN) for the common
day-to-day work:

- **Chat** — submit a task over `POST /run?async=1` and follow the steps and
  the final answer while the job runs; cancel is cooperative.
- **Incidents** — the records the workers write, newest first, filterable by
  status; click one for the full observation/actions/verification JSON.
- **Approvals** — the repair plans the policy parked for a human: the action,
  its risk tier, the expected effect, how it would be verified and how it would
  be undone, with **Approve** / **Reject** buttons. The effective policy (mode,
  tiers, rules) is shown next to it.
- **Tasks** — both queues (`/tasks`): queue depth, active counts, worker
  heartbeats and the hourly budget as chips, a filterable task list, a detail
  pane with the log tail, cancel, and a **manual repair** form (kind / name /
  namespace / cause). The list refreshes every 5 s; the open detail does not.
- **Audit** — the mutation log (`GET /audit`) with action, target, phase,
  incident id and audit id.
- **Provider** — edit the active provider, model, base URL, max tokens, data
  policy and API key, then save, clear the key or send a test completion.

```bash
kubectl -n auton port-forward svc/auton 8080:8080
# http://localhost:8080/  →  "API access" → paste the Secret value
```

**Why a token at all?** The page is public on purpose (it contains no data),
but `/run` can execute shell commands and patch workloads in the cluster. A
page that embedded the token would leak it with the HTML, so the token is
entered once and kept only in this browser's `localStorage`, sent only to the
same origin. The UI explains this in the **API access** modal (with the
`kubectl get secret` command and a copy button) instead of using a bare
`prompt()`. While no valid token is stored, a banner stays visible and every
`401` re-opens the modal naming the rejected request; “Forget token” removes
it. `AGENT_AUTH_DISABLED=1` (local development only) hides the whole flow.

The page is also reachable through the ingress at `http://auton.localhost/`.

### Model providers

The agent is not tied to Ollama. `mcp/llm.py` supports:

| Provider | `--provider` | Default model | Endpoint |
|---|---|---|---|
| Local Ollama (default) | `ollama` | `qwen2.5-coder:7b-instruct` | `http://ollama-service:11434` |
| OpenAI | `openai` | `gpt-4o-mini` | `https://api.openai.com/v1` |
| Gemini | `gemini` | `gemini-2.0-flash` | Gemini's OpenAI-compatible endpoint |
| Claude | `anthropic` (alias `claude`) | `claude-sonnet-4-5` | `https://api.anthropic.com` |
| Any OpenAI-compatible server | `custom` | *(required)* | `--base-url` (vLLM, LM Studio, llama.cpp, …) |

Settings are read **on every request** from a projected volume, so switching
does not need a pod restart:

```
/etc/auton-model/config/{provider,base_url,model,max_tokens,data_policy}
/etc/auton-model/secret/api-key
```

`scripts/set-provider.sh` writes them, waits for the pod to observe the change,
prints what the agent's own code sees, and runs an optional round-trip:

```bash
# remote provider: the key comes from a file (or stdin), never argv
scripts/set-provider.sh --provider openai --model gpt-4o-mini \
  --api-key-file /tmp/opencode/openai.key --test

scripts/set-provider.sh --provider claude --model claude-sonnet-4-5 \
  --api-key-file ~/.config/auton/anthropic.key

scripts/set-provider.sh --provider custom --base-url http://vllm:8000/v1 \
  --model qwen2.5-coder-32b --api-key-file /tmp/opencode/vllm.key

# back to the local model (and remove the stored remote key)
scripts/set-provider.sh --provider ollama --clear-key --test
```

The same settings are editable from the UI (**Provider** tab) or over HTTP:

```bash
# partial update; api_key is optional and write-only
curl -s -X POST http://localhost:8080/provider \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"provider":"openai","model":"gpt-4o-mini","api_key":"sk-..."}'

# remove the stored key
curl -s -X DELETE http://localhost:8080/provider/key \
  -H "Authorization: Bearer $TOKEN"
```

`POST /provider` validates before writing: an unknown provider, an unknown
field, `max_tokens` outside 64–8192, `data_policy=local-only` with a remote
provider, `custom` without a base URL, or a remote provider without a key are
all rejected with `400` — the agent never stores a configuration it would then
refuse at request time. The change is written to ConfigMap
`auton-model-config` and Secret `auton-model` (the pod then observes it through
the mounted volume, no restart) and audited as `provider_update` with only
“key set/cleared”, never the key value. `GET /provider` stays read-only and
returns the key masked (`sk-…3456`), never the raw value, plus `providers`,
`policies`, `defaults` and `editable` for the form.

RBAC keeps this narrow: the API runs as ServiceAccount `auton-api`, whose Role
rules allow `patch` on the named ConfigMaps `auton-model-config` and
`auton-policy` and on the named Secret `auton-model` — and nothing else. It
cannot read secrets, cannot touch workloads, and no other Role may touch
secrets at all.

Two rules are enforced in code, not by configuration:

- **No fallback.** When the provider is not `ollama`, the local model is never
  contacted. A missing key or an unreachable endpoint fails the run with an
  error instead of silently degrading to Ollama.
- **Remote data is redacted.** With `data_policy=redact` (the default),
  passwords, tokens, `Authorization:` headers, cloud keys and private-key blocks
  are masked before any message leaves the cluster. `data_policy=local-only`
  refuses remote providers entirely; `allow` disables redaction (only for
  endpoints you trust).

Check the active state and test it:

```bash
curl -s http://localhost:8080/provider -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
# {"ok": true, "provider": "ollama", "model": "qwen2.5-coder:7b-instruct",
#  "local_model_enabled": true, "api_key_set": false, ...}

curl -s -X POST http://localhost:8080/provider/test -H "Authorization: Bearer $TOKEN"
# {"ok": true, "reply": "ok", ...}   (502 on a provider error)
```

`/health` also reports `provider`, `model` and `local_model_enabled`, which is
what the UI uses to show whether a remote provider is active. Remote
providers receive tool schemas as function definitions and their tool calls are
normalised into the JSON-text format the loop already parses, so the
assistant/tool history has one shape. A 7B model is not very reliable through
the OpenAI-compatible path either — use a stronger model when you pick a
remote provider.

### HTTP API (`agent_service.py`)

`GET /` and `GET /ui` serve the bundled UI; `GET /health` is the open JSON
probe. Everything below needs the bearer token.

Synchronous (default — this is what the n8n workflows call):

```bash
TOKEN=$(kubectl -n auton get secret auton-api-token -o jsonpath='{.data.token}' | base64 -d)

# liveness / readiness (also reports queue depth)
curl -s http://localhost:8080/health
# {"status":"ok","model":"qwen2.5-coder:7b-instruct","ollama":"http://ollama-service:11434", ...}

# run a task
curl -s http://localhost:8080/run \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"task":"Write greet.py that prints hello, run it, then show the file."}' \
  | python3 -m json.tool
```

Response shape:

```json
{
  "job_id": "9f2c1a4b7d10",
  "final": "Plain-text summary from the model when the task is done.",
  "stop_reason": "final",
  "elapsed": 41.2,
  "steps": [
    {"tool": "write_file", "arguments": {"path": "greet.py", "content": "..."}, "output": "Wrote 42 bytes ...", "ms": 2},
    {"tool": "bash", "arguments": {"command": "python3 greet.py"}, "output": "$ python3 greet.py\n[exit 0]\n...", "ms": 180}
  ]
}
```

`stop_reason` is one of `final`, `max_steps`, `parse_failed`, `repeat`,
`cancelled` or `error` — useful when the model stops early. `repeat` means the
loop guard fired because the model kept re-running the same call (or the same
failing call). Errors are JSON too: `401` missing/wrong token, `400` missing
`task`, `429` queue full, `500` agent error, `503` no token configured,
`504` sync wait timed out.

### Audit log (`GET /audit`)

Every mutation writes a `begin`/`end` pair to `/workspace/.ops/audit.jsonl`
(with a SQLite index beside it). The endpoint requires the same bearer token:

```bash
curl -s "http://localhost:8080/audit?limit=20" \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
# {"count": 2, "records": [{"phase":"begin","action":"k8s_scale",...},
#                          {"phase":"end","status":"ok",...}]}

# one incident only
curl -s "http://localhost:8080/audit?incident_id=inc-abc123" \
  -H "Authorization: Bearer $TOKEN"
```

If the audit volume is not writable the endpoint answers `503` and every
mutating Kubernetes tool refuses to run. That is deliberate: the log is a
precondition, not a best-effort side effect.

### Incidents (`/incidents`)

`auton-scanner` detects problems without being asked; `auton-worker` repairs the
deterministic ones, verifies the result and records what it did. Every incident
is one atomic JSON file under `/workspace/.ops/incidents` (shared PVC). It
carries a stable `fingerprint`, an occurrence count, a full `timeline[]` of
state transitions, the structured evidence document, the deterministic
diagnosis, the repair plan and the policy decision that was made about it:

```bash
# newest first; filter by one or more statuses
curl -s "http://localhost:8080/incidents?limit=20" \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
curl -s "http://localhost:8080/incidents?status=needs_human" \
  -H "Authorization: Bearer $TOKEN"

# one record: diagnosis, evidence, timeline, plan, policy, actions, report
curl -s "http://localhost:8080/incidents/inc-8173cfab0b" \
  -H "Authorization: Bearer $TOKEN"
```

The incident walks an explicit state machine — `DETECTED → TRIAGING →
DIAGNOSING → PLANNING → POLICY_CHECK → QUEUED → EXECUTING → VERIFYING →
RESOLVED`, with `AWAITING_APPROVAL`, `SUPPRESSED` and `NEEDS_HUMAN` as the
side exits. A closed incident can only be re-opened through `reopen()`, and a
repair retry reuses the *same* incident; a recurrence opens a *new* one linked
to the old. `GET /incidents` filters by `severity`, `cause`, `namespace`,
`resource` and `fingerprint` (each a comma list).

`POST /incidents/ingest` is what the workers call; it is idempotent by
`id` and lets a worker move to its own PVC later without changing the API. The
record also contains a ready-to-send `report` string (used by the UI and
Slack/Telegram).

The scanners snapshot `auton-apps` every 60 s and run **rule-based** detection —
the LLM is not in this path:

| Symptom | Cause | Action |
|---|---|---|
| `OOMKilled` (last state) on a Deployment pod | `oom` | raise the memory limit by 25% (min 32Mi, cap `AGENT_OPS_MAX_MEMORY=2Gi`), roll out, verify Available |
| pod stuck `Terminating` for more than `AGENT_OPS_TERMINATION_GRACE` | `stuck` | one forced delete with grace 0 |
| `CrashLoopBackOff`, no config-shaped log evidence | `crashloop` | delete the pod so its ReplicaSet recreates it, verify |
| `ProgressDeadlineExceeded` / `ReplicaFailure` | `rollout` | `kubectl rollout undo`, verify |
| Deployment scaled to zero | `replicas_zero` | scale to 1, verify |
| `ImagePullBackOff` / bad image | `image` | **report only** (`needs_human`) |
| missing ConfigMap/Secret, connection/auth failures | `config` | **report only** |
| unschedulable / node capacity | `scheduling` | **report only** |
| unbound PVC, StatefulSet, bare failed pod, not Ready | — | **report only** |

Safety rails: only `AGENT_OPS_NAMESPACES` (default `auton-apps`) is repaired;
`auton.io/protect=true` resources are skipped; every mutation goes through the
audit gate and carries the incident id; a cooldown (1800 s per resource+cause)
and an hourly action budget (6) stop flapping; `AGENT_OPS_DRY_RUN=1` reports
without changing anything. `AGENT_OPS_STARTUP_GRACE` (120 s) ignores a young
`Pending`/`NotReady` pod so an image pull is not an incident, and a pod that
reports several reasons gets a single action (`OOMKilled` wins over
`CrashLoopBackOff`). Secrets and PVC deletion are impossible in code and in
RBAC, so the workers report those cases and ask a human.

Two scanners writing the same finding at the same time submit the same
idempotency key (`scan:<namespace>|<resource>|<cause>|<bucket>`), so the queue
keeps one task; an active task or a live cooldown suppresses the finding before
it is queued.

### Repair policy and approvals (`/policy`, `/approvals`)

A repair is never executed just because it was detected. Every task is first
**planned** (what would be done, at which risk tier, how it would be verified
and undone) and then **policy-checked**. The policy lives in ConfigMap
`auton-policy` and is mounted into the API and the workers as a projected
volume, so it is read per decision and a change takes effect without a
restart.

```bash
# the effective policy
curl -s http://localhost:8080/policy -H "Authorization: Bearer $TOKEN"

# change it (validated before it is written)
scripts/set-policy.sh --show
scripts/set-policy.sh --mode observe-only
scripts/set-policy.sh --mode safe-auto --auto-tiers 0,1,2 --approval-tiers 3
scripts/set-policy.sh --deny-cause crashloop
scripts/set-policy.sh --require-approval-cause oom
scripts/set-policy.sh --reset
```

| Mode | Behaviour |
|---|---|
| `disabled` | **The off switch.** Nothing runs; every decision is a denial. |
| `observe-only` | Shadow mode: plans are computed and recorded, never executed. |
| `safe-auto` | The tiers in `auto_approve_tiers` run unattended, the tiers in `approval_tiers` wait for a human. |
| `approval-required` | Every mutating tier waits for a human. |

The tier describes the **risk of the action**, not the severity of the symptom:
0 = observe/report, 1 = transient (restart a pod, undo a rollout, scale up),
2 = changes the workload's specification (raise a memory limit), 3 =
data-affecting or irreversible. The shipped default is `safe-auto` with
`auto_approve_tiers: [0, 1, 2]` and `approval_tiers: [3]` — exactly the
behaviour before the policy engine existed.

**Fail closed.** An unreadable or invalid policy document is treated as
`disabled`: the decision is a denial that names the problem, and `GET /policy`
reports the error. A typo must never silently *widen* what the platform may do.

When the policy asks for an approval, **nothing in the cluster changes**. The
plan becomes an approval request and the incident waits in
`AWAITING_APPROVAL`:

```bash
# the pending plans
curl -s http://localhost:8080/approvals?status=pending \
  -H "Authorization: Bearer $TOKEN"

# approve one: the repair is enqueued and the same incident continues
curl -s -X POST http://localhost:8080/approvals/apr-569317262639/approve \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"by":"alice","comment":"safe to bump the limit"}'

# or reject it: the incident goes to needs_human
curl -s -X POST http://localhost:8080/approvals/apr-569317262639/reject \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"by":"alice","comment":"not now"}'
```

An approval can be decided **once**: a second click answers `409` instead of
running the repair twice. The UI has an **Approvals** tab with the same
buttons and the policy view next to it.

### Task queue (`/tasks`)

Both queues are one view. `/jobs` is the n8n-facing view of *agent* runs;
`/tasks` is the operator view of everything the queue holds — scanner findings,
manual repairs and agent jobs, with their status, attempts, worker and log tail.

```bash
# everything, newest first (filter with ?queue= and ?status=)
curl -s "http://localhost:8080/tasks?limit=20" \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
# queue depth, active counts, worker heartbeats, hourly budget
curl -s "http://localhost:8080/tasks/stats" -H "Authorization: Bearer $TOKEN"
# one task with its log tail
curl -s "http://localhost:8080/tasks/tsk-1a2b3c4d5e6f" \
  -H "Authorization: Bearer $TOKEN"
# queue a manual repair (still validated, locked, budgeted and audit-gated)
curl -s -X POST http://localhost:8080/tasks \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"queue":"repair","kind":"Deployment","name":"api","cause":"oom"}'
# cancel a queued task
curl -s -X POST "http://localhost:8080/tasks/tsk-1a2b3c4d5e6f/cancel" \
  -H "Authorization: Bearer $TOKEN"
```

A manual task is not a shortcut past the rails. `POST /tasks` validates the same
fields a finding has (`kind` ∈ Pod/Deployment/StatefulSet/PersistentVolumeClaim,
a DNS-label name, a namespace inside `AGENT_OPS_NAMESPACES`, a cause from
`CAUSE_TEXT`), writes an audit *event* for the submission and leaves the actual
`kubectl` call to the worker, which re-checks the namespace, the per-resource
lock and the hourly budget. `"force": true` (what the UI sends) bypasses the
cooldown; the budget still applies.

In `AGENT_QUEUE=memory` mode (local development) `/tasks` shows only
store-backed repair tasks, because the agent job queue lives in the API process
and is exposed under `/jobs`.

### Jobs API (long tasks)

A 7B model can spend several minutes on 25 steps, which is longer than a
single HTTP request should stay open. Submit asynchronously instead (this is
what the generated n8n pipeline does):

```bash
# 202 {"job_id":"...","status":"queued","poll":"/jobs/..."}
JOB=$(curl -s -X POST 'http://localhost:8080/run?async=1' \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"task":"Refactor fib.py and run the tests."}' | python3 -c 'import json,sys;print(json.load(sys.stdin)["job_id"])')

curl -s -H "Authorization: Bearer $TOKEN" "http://localhost:8080/jobs/$JOB" | python3 -m json.tool
curl -N -H "Authorization: Bearer $TOKEN" "http://localhost:8080/jobs/$JOB/events"
curl -s -X POST -H "Authorization: Bearer $TOKEN" "http://localhost:8080/jobs/$JOB/cancel"
```

| Endpoint | Purpose |
|---|---|
| `GET /auth/check` | Validate the bearer token (used by the UI before saving it) |
| `GET /provider` | Active provider (masked key) + editable options |
| `POST /provider` | Partial provider update; optional write-only `api_key` |
| `DELETE /provider/key` | Remove the stored provider API key |
| `POST /provider/test` | One tiny completion through the active provider |
| `POST /run?async=1` | Enqueue, return `202` + `job_id` |
| `GET /jobs` | Recent jobs (last 50) |
| `GET /jobs/{id}` | Status, log tail, result (includes `done`) |
| `GET /jobs/{id}/events` | Server-sent event stream |
| `POST /jobs/{id}/cancel` | Stop at the next step boundary |
| `GET /audit` | Mutation log, optionally `?incident_id=` |
| `GET /incidents`, `GET /incidents/{id}` | Incident records |
| `POST /incidents/ingest` | Idempotent incident ingest from a worker |
| `GET /policy` | The effective repair policy (mode, tiers, rules) and its source |
| `GET /approvals`, `GET /approvals/{id}` | Approval requests for repair plans the policy parked |
| `POST /approvals/{id}/approve` | Approve a pending plan and enqueue the repair (`409` if already decided) |
| `POST /approvals/{id}/reject` | Reject a pending plan; the incident goes to `NEEDS_HUMAN` |
| `GET /tasks`, `GET /tasks/{id}`, `GET /tasks/stats` | Both queues (repair + agent) |
| `POST /tasks` | Queue a manual repair, or an agent job (`queue="agent"`) |
| `POST /tasks/{id}/cancel` | Cancel a queued task |

With `AGENT_QUEUE=redis` the API only enqueues; `auton-ops` runs the jobs and a
global Redis slot keeps model calls one at a time. With `AGENT_QUEUE=memory`
(local development) a single worker thread runs them FIFO in-process.
`AGENT_MAX_QUEUE` (8) and `AGENT_JOB_TIMEOUT` (900 s) are the guard rails either
way.

### Toolset

| Area | Tools |
|---|---|
| Workspace | `list_directory`, `read_file`, `write_file`, `edit_file`, `apply_patch` |
| Search | `grep`, `glob` |
| Shell | `bash`, `job`, `run_tests` |
| Git | `git_status`, `git_diff` |
| Housekeeping | `fs` (stat/mkdir/move/delete), `todo_write` |
| Kubernetes | `k8s_get`, `k8s_describe`, `k8s_logs`, `k8s_snapshot`, `k8s_diagnose`, `k8s_events`, `k8s_wait`, `k8s_apply`, `k8s_patch`, `k8s_scale`, `k8s_rollout`, `k8s_delete`, `k8s_exec` |
| n8n | `n8n_workflows`, `n8n_activate`, `n8n_executions`, `n8n_webhook`, `n8n_create_agent_pipeline`, `n8n_create_ops_pipeline` |
| Queue | `task_queue` (list/get/stats/submit/cancel) |

`run_command` remains as a deprecated alias for `bash`. There are 34 tools in
total; descriptions are deliberately terse because 34 schemas cost prompt
tokens on a 7B model.

- `read_file` takes `start_line` / `end_line` and returns numbered lines, so a
  large file cannot eat the context window.
- `edit_file` requires a unique match by default and returns a diff. Anchor
  matching falls back to ignoring trailing whitespace, then quote style — a 7B
  model will write `'0.1.0'` against a file containing `"0.1.0"` forever
  otherwise. When an anchor really misses, the error names the closest line.
- `apply_patch` applies one or more SEARCH/REPLACE blocks atomically — this is
  the preferred edit tool:

  ```
  <<<<<<< SEARCH
  exact existing lines
  =======
  replacement lines
  >>>>>>> REPLACE
  ```

- `grep` / `glob` use ripgrep when present and respect `.gitignore`.
- `bash(background=true)` returns a `job_id` you poll with `job(job_id)`.
- `run_tests` auto-detects pytest / npm / cargo / go / make and reports
  pass/fail plus the relevant tail.

#### Kubernetes tools

| Tool | Behaviour |
|---|---|
| `k8s_get(resource, name, selector, output, namespace)` | List/get; `output` is `wide`, `yaml`, `json` or `name` |
| `k8s_describe(resource, name, namespace)` | Describe (includes events) |
| `k8s_logs(pod, container, tail, previous, namespace)` | Log tail, capped at 2000 lines |
| `k8s_snapshot(namespace)` | Compact health report: pods, deployments, PVCs, warning events, `PROBLEMS:` |
| `k8s_diagnose(pod, namespace, container)` | Evidence pack (status, events, current/previous logs) + deterministic `diagnosis`/`suggested_action` |
| `k8s_events(since, warnings_only, namespace)` | Events newer than `90s`/`15m`/`2h`/`1d` |
| `k8s_wait(resource, name, condition, timeout, namespace)` | `ready`, `available`, `complete` or `delete`, max 300 s |
| `k8s_apply(manifest, namespace)` | Apply YAML/JSON; omit `metadata.namespace` |
| `k8s_patch(resource, name, patch, patch_type, namespace)` | `strategic`, `merge` or `json` patch |
| `k8s_scale(deployment, replicas, namespace)` | Scale 0–10 |
| `k8s_rollout(action, deployment, namespace)` | `status`, `restart`, `undo` |
| `k8s_delete(resource, name, confirm, namespace)` | Refuses unless `confirm=true`; PVCs are always refused |
| `k8s_exec(pod, command, container, timeout, namespace)` | `sh -c` in the pod, audited, max 120 s |

The namespace defaults to `AGENT_NAMESPACE` but tools accept a `namespace`
argument **only** if it is in `AGENT_NAMESPACES` (`auton,auton-apps`), so a
confused model cannot target `kube-system`. Resource/name arguments are
regex-validated, and `AGENT_K8S_WRITE=0` turns every mutating tool into a
refusal even if RBAC would allow it.

Every mutation (`apply`, `patch`, `scale`, `rollout`, `delete`, `exec`) goes
through the write-ahead audit first: `audit.begin()` appends and fsyncs a
`begin` record to `$AGENT_AUDIT_DIR/audit.jsonl` (plus a SQLite index), then
kubectl runs, then `audit.finish()` records the result. If the record cannot be
written, **the mutation is refused and kubectl never runs** — "nothing without
a record" is enforced by code, not by the prompt. `GET /audit` exposes the same
records.

#### n8n tools

| Tool | Behaviour |
|---|---|
| `n8n_workflows(action, workflow_id, payload)` | `list`, `get`, `create`, `update`, `delete` |
| `n8n_activate(workflow_id, active)` | Activate/deactivate |
| `n8n_executions(workflow_id, limit)` | Recent runs |
| `n8n_webhook(path, payload)` | POST a JSON body to `/webhook/<path>` and return the response (900 s timeout) |
| `n8n_create_agent_pipeline(name, path, activate)` | Create the webhook → async job → poll → respond template and activate it |
| `n8n_create_ops_pipeline(kind, name, path, activate)` | Create the incident → Slack/Telegram notification workflow (`kind=slack\|telegram\|both`) |

`n8n_create_agent_pipeline` exists because asking a 7B model to hand-write n8n
node JSON is a losing game: the graph is fixed and generated, only the name and
webhook path vary. It starts the task with `?async=1` and polls `/jobs/{id}`
every 5 s, so the workflow is not limited by a single HTTP request timeout.

`n8n_create_ops_pipeline` generates the reporting half: a webhook that accepts
the ops notification payload and fans out to generic HTTP Request nodes. Slack
uses an incoming-webhook URL (`SLACK_OPS_WEBHOOK`); Telegram uses
`TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`; no n8n credential is needed. The
workflow is inactive until activated, and the workers post their reports to
`/webhook/auton-ops` via `N8N_OPS_WEBHOOK`.

```bash
kubectl -n auton exec deploy/auton -- python -c \
  'import n8n_tools; print(n8n_tools.n8n_create_ops_pipeline(kind="both"))'
```

### n8n webhook

```bash
curl -s -X POST http://localhost:5678/webhook/auton-agent \
  -H 'Content-Type: application/json' \
  -d '{"task":"Create hello_n8n.py that prints hi, and run it."}'

# ops report (normally sent by auton-worker)
curl -s -X POST http://localhost:5678/webhook/auton-ops \
  -H 'Content-Type: application/json' \
  -d '{"incident_id":"inc-1","severity":"critical","status":"resolved",
       "namespace":"auton-apps","resource":"pod/api-1",
       "summary":"OOMKilled","action":"raised memory 64Mi -> 96Mi",
       "verification":"ready=1/1","url":"http://auton.localhost/incidents/inc-1"}'
```

Enable the workflow first (toggle **Active** in the n8n UI, or use
`n8n_activate` / `n8n_create_*_pipeline`).

---

## Configuration

### Agent (`mcp/`)

| Variable | Default | Meaning |
|---|---|---|
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama base URL. In the pod: `http://ollama-service:11434` |
| `AGENT_MODEL` | `qwen2.5-coder:7b-instruct` | Fallback model tag when `AGENT_MODEL_DIR` has no `model` file |
| `AGENT_MODEL_DIR` | `/etc/auton-model` | Provider config root (ConfigMap `config/`, Secret `secret/`); read per request |
| `AGENT_PROVIDER` | `ollama` | Env fallback for the provider (`ollama`/`openai`/`gemini`/`anthropic`/`custom`) |
| `AGENT_LLM_BASE_URL` | *(provider default)* | Env fallback for the API base URL |
| `AGENT_LLM_MODEL` | *(provider default)* | Env fallback for the model name |
| `AGENT_LLM_API_KEY` | *(none)* | Env fallback for the API key (prefer the mounted Secret file) |
| `AGENT_LLM_MAX_TOKENS` | `1024` | Response cap for remote providers |
| `AGENT_LLM_TIMEOUT` | `120` | Remote HTTP timeout, seconds |
| `AGENT_DATA_POLICY` | `redact` | `redact` masks secrets before remote calls; `local-only` refuses them; `allow` disables redaction |
| `AGENT_PROVIDER_STORE` | `kubectl` | Write path for `POST /provider`: `kubectl` (ConfigMap/Secret) or `files` (local development) |
| `AGENT_PROVIDER_NAMESPACE` | `AGENT_NAMESPACE` | Namespace of the provider ConfigMap/Secret |
| `AGENT_PROVIDER_CONFIGMAP` | `auton-model-config` | ConfigMap the API is allowed to `patch` |
| `AGENT_PROVIDER_SECRET` | `auton-model` | Secret the API is allowed to `patch` (key only) |
| `AGENT_WORKSPACE` | `./workspace` | Root that file tools are jailed to. In the pod: `/workspace` |
| `MAX_STEPS` | `25` | Max tool-calling iterations before giving up |
| `AGENT_NUM_CTX` | `16384` | Ollama context window; raise if the model truncates long histories |
| `AGENT_MAX_OBSERVATION_CHARS` | `8000` | Cap per tool result (keeps the context alive) |
| `AGENT_MAX_HISTORY_CHARS` | `60000` | Compaction threshold for old observations |
| `AGENT_KEEP_OBSERVATIONS` | `3` | Recent tool results kept verbatim |
| `AGENT_PARSE_RETRIES` | `1` | Corrective retries for malformed tool calls |
| `AGENT_REPEAT_LIMIT` | `2` | Identical calls (or identical failures, or identical output under a new signature) tolerated before nudging, then stopping |
| `AGENT_REPEAT_PENALTY` | `1.1` | `repeat_penalty` sent to Ollama; damps verbatim loops |
| `AGENT_MAX_READ_CHARS` | `20000` | Default `read_file` cap |
| `AGENT_MAX_OUTPUT_CHARS` | `8000` | Default `bash`/kubectl/n8n output cap |
| `AGENT_MAX_QUEUE` | `8` | Pending jobs before `429` (`agent_service.py`) |
| `AGENT_JOB_TIMEOUT` | `900` | Wall-clock limit per job, in seconds |
| `AGENT_SANDBOX` | `off` | `bwrap` wraps `bash`/`run_tests`/`git`; recommended for host CLI runs |
| `AGENT_SANDBOX_NET` | `on` | With bwrap, set `off` to `--unshare-net` |
| `AGENT_ALLOW_TEST_EDITS` | `0` | `1` lets the edit tools change/remove assertions in test files (off: HARD RULE 1 is enforced) |
| `AGENT_PORT` | `8080` | HTTP listen port (`agent_service.py`) |
| `AGENT_BIND` | `0.0.0.0` | HTTP bind address (`agent_service.py`) |
| `AGENT_API_TOKEN` | *(none)* | Bearer token; required or the service refuses to start |
| `AGENT_AUTH_DISABLED` | *(unset)* | `1` disables auth — local development only |
| `AGENT_SYSTEM_EXTRA` | *(empty)* | Extra text appended to the system prompt |
| `AGENT_NAMESPACE` | `auton` | Default namespace for the Kubernetes tools |
| `AGENT_NAMESPACES` | `AGENT_NAMESPACE` | Comma list the tools may target; deployment sets `auton,auton-apps` |
| `AGENT_AUDIT_DIR` | `$AGENT_WORKSPACE/.ops` | Write-ahead audit log + SQLite index; mutations refuse if unwritable |
| `AGENT_INCIDENTS_DIR` | `$AGENT_AUDIT_DIR/incidents` | Incident JSON files (`/incidents*`) |
| `AGENT_APPROVALS_DIR` | `$AGENT_AUDIT_DIR/approvals` | Approval-request JSON files (`/approvals*`) |
| `AGENT_POLICY_DIR` | `/etc/auton-policy` | Where the projected policy ConfigMap is mounted (the API + the workers) |
| `AGENT_POLICY_MODE` | *(none)* | Mode override when no policy document is present |
| `AGENT_K8S_ENABLED` | `1` | `0` disables the Kubernetes tools |
| `AGENT_K8S_WRITE` | `0` (image) / `1` (deployment) | Allows apply/patch/scale/rollout/delete/exec |
| `N8N_API_URL` | `http://n8n:5678/api/v1` | n8n public API base |
| `N8N_WEBHOOK_BASE` | `http://n8n:5678/webhook` | Webhook base |
| `N8N_API_KEY_FILE` | `/etc/n8n-api/api-key` | Key file, read on every call |
| `N8N_API_KEY` | *(none)* | Key value fallback (overrides the file) |
| `N8N_API_TIMEOUT` | `30` | Public-API timeout, seconds |
| `N8N_WEBHOOK_TIMEOUT` | `900` | Webhook timeout, seconds (agent runs are slow) |
| `AGENT_RUN_URL` | `http://auton:8080/run` | URL baked into generated n8n pipelines |

### Queue (`redis`, scanners, workers, executor)

Every pod except Ollama/n8n reads some of these.

| Variable | Default (manifest) | Meaning |
|---|---|---|
| `AGENT_QUEUE` | `redis` | `redis` (cluster) or `memory` (local). No silent fallback |
| `AGENT_REDIS_URL` | `redis://redis.auton.svc.cluster.local:6379/0` | Queue endpoint |
| `AGENT_REDIS_PASSWORD` / `_FILE` | *(none)* | Optional auth; the manifest relies on ClusterIP only |
| `AGENT_QUEUE_PREFIX` | `auton` | Key prefix (`auton:q:*`, `auton:task:*`, …) |
| `AGENT_TASK_LEASE` | `300` | Seconds before a crashed worker's task is requeued |
| `AGENT_TASK_MAX_ATTEMPTS` | `3` | Repair retries (agent jobs are attempted once) |
| `AGENT_TASK_BACKOFF` | `30,120,300` | Seconds between repair attempts |
| `AGENT_GPU_LOCK_TTL` | `900` | TTL of the single global LLM slot |
| `AGENT_QUEUE_MAX` | `500` | Refuse new work above this depth |
| `AGENT_SCAN_INTERVAL` | `60` | Scanner pass interval |
| `AGENT_SCAN_DEDUPE_WINDOW` | `300` | Concurrent duplicate suppression window |
| `AGENT_WORKER_POLL` | `5` | Seconds between `claim()` attempts |
| `AGENT_OPS_NAMESPACES` | `auton-apps` | Namespaces a playbook may **repair** (all others are read-only) |
| `AGENT_OPS_COOLDOWN` | `1800` | Seconds before the same resource+cause is acted on again |
| `AGENT_OPS_MAX_ACTIONS_PER_HOUR` | `6` | Hourly repair budget; further findings wait (report-only findings are unaffected) |
| `AGENT_OPS_STARTUP_GRACE` | `120` | Seconds a `Pending`/`NotReady` pod is ignored after it starts (image pull/startup); `Unschedulable` is always reported, `0` disables the grace |
| `AGENT_OPS_TERMINATION_GRACE` | `120` | Seconds a pod may stay `Terminating` before the scanner calls it stuck |
| `AGENT_OPS_MAX_MEMORY` | `2Gi` | Cap for automatic OOM limit increases |
| `AGENT_OPS_MAX_INCIDENTS` | `500` | Records kept; older files are pruned |
| `AGENT_OPS_PRUNE_EVERY` | `300` | Seconds between incident prunes |
| `AGENT_OPS_DRY_RUN` | `0` | `1` records `detected` + `dry_run` without mutating |
| `AGENT_OPS_HEARTBEAT` | `/tmp/<role>-heartbeat` | File the liveness probe checks |
| `AGENT_OPS_DIR` | `AGENT_AUDIT_DIR` | Incident/audit root (shared `/workspace/.ops`) |
| `AGENT_API_URL` | `http://auton:8080` | Where incidents are ingested |
| `AGENT_API_TOKEN` | *(Secret)* | Bearer token for `/incidents/ingest` |
| `AGENT_UI_URL` | `http://auton.localhost` | Base for incident links in notifications |
| `N8N_OPS_WEBHOOK` | `/webhook/auton-ops` | Slack/Telegram relay; empty disables it |
| `N8N_OPS_TOKEN` | `AGENT_API_TOKEN` | Bearer header sent to the webhook |
| `AGENT_NAMESPACES` | `auton,auton-apps` | `k8s_tools` allowlist |
| `AGENT_K8S_WRITE` | `1` (workers/executor) / `0` (scanner) | `0` makes every playbook refuse to mutate |

The scanner never mounts the workspace: it needs no incident history, and the
task store plus the cooldown already suppress a duplicate. It keeps its private
files in `/tmp` (an emptyDir), so the `ReadWriteOnce` workspace PVC is mounted
only by the pods that write the shared audit log.

### Ollama

| Variable | Value | Meaning |
|---|---|---|
| `HSA_OVERRIDE_GFX_VERSION` | `10.3.0` | Pretend to be `gfx1030` for ROCm |
| `OLLAMA_HOST` | `0.0.0.0:11434` | Listen on all interfaces inside the pod |
| `OLLAMA_KEEP_ALIVE` | `30m` | Keep the model resident between requests |
| `OLLAMA_FLASH_ATTENTION` | `1` | Cuts attention memory, keeps a 16k context on the GPU |
| `OLLAMA_KV_CACHE_TYPE` | `q8_0` | Quantised KV cache (~half the memory of f16) |
| `OLLAMA_NUM_PARALLEL` | `1` | One request at a time: no duplicated KV cache |

An 8 GB card is shared with the desktop, so an f16 KV cache at 16k context
does **not** fit: Ollama silently splits the model to `80%/20% CPU/GPU` and
throughput collapses to ~5 tok/s. Flash attention plus a q8 KV cache keeps the
whole model resident. Verify after loading a model:

```bash
kubectl -n auton exec deploy/ollama -- ollama ps
# PROCESSOR should say 100% GPU, not "80%/20% CPU/GPU"
```

### n8n

| Variable | Value | Meaning |
|---|---|---|
| `N8N_PORT` | `5678` | Listen port |
| `N8N_PROTOCOL` | `http` | No TLS in-cluster |
| `N8N_SECURE_COOKIE` | `false` | Allow the UI over plain HTTP |
| `N8N_RUNNERS_ENABLED` | `true` | Enable task runners |
| `GENERIC_TIMEZONE` / `TZ` | `Europe/Istanbul` | Timezone |
| `N8N_HOST` | `n8n.localhost` | Host used in generated URLs |
| `WEBHOOK_URL` | `http://n8n.localhost/` | Base for displayed/registered webhooks |
| `N8N_EDITOR_BASE_URL` | `http://n8n.localhost/` | Editor links |
| `N8N_BLOCK_ENV_ACCESS_IN_NODE` | `false` | Lets workflow expressions read `$env.AGENT_API_TOKEN` (and the ops targets) |
| `AGENT_API_TOKEN` | from Secret | Token the workflow sends to `auton` |
| `SLACK_OPS_WEBHOOK` | Secret `n8n-notify` (optional) | Slack incoming-webhook URL for ops reports |
| `TELEGRAM_BOT_TOKEN` | Secret `n8n-notify` (optional) | Telegram bot token for ops reports |
| `TELEGRAM_CHAT_ID` | Secret `n8n-notify` (optional) | Telegram chat/channel id |

Create only the notification keys you need; the `secretKeyRef`s are
`optional: true`, so n8n starts regardless:

```bash
kubectl -n auton create secret generic n8n-notify \
  --from-literal=slack-webhook='https://hooks.slack.com/services/...' \
  --from-literal=telegram-bot-token='123:ABC...' \
  --from-literal=telegram-chat-id='-1001234567890'
kubectl -n auton rollout restart deploy/n8n
```

---

## Local development (no cluster)

```bash
cd mcp
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# in another shell: expose the cluster's Ollama on localhost
kubectl -n auton port-forward svc/ollama-service 11434:11434

OLLAMA_HOST=http://localhost:11434 AGENT_WORKSPACE="$PWD/workspace" \
  python agent_client.py "Write greet.py and run it."
```

To run the HTTP front-end outside Kubernetes:

```bash
OLLAMA_HOST=http://localhost:11434 AGENT_WORKSPACE="$PWD/workspace" \
AGENT_AUTH_DISABLED=1 python agent_service.py
```

`AGENT_AUTH_DISABLED=1` is what makes local runs work without a token; never
set it in the deployment.

The Kubernetes tools use the caller's kubeconfig on the host (`kubectl` must be
installed and the k3d context merged) and `AGENT_NAMESPACE` (default `auton`).
The n8n tools need `N8N_API_URL`/`N8N_WEBHOOK_BASE` pointed at a port-forward
and a key in `N8N_API_KEY` or `N8N_API_KEY_FILE`.

Run the test suite (no cluster or GPU needed):

```bash
cd mcp
pip install pytest
python -m pytest tests -q
```

The suite covers workspace escape, unique-match editing, atomic patches,
gitignore-aware search, background jobs, git tools, the context-compaction
helpers, the HTTP jobs API, bearer auth, the kubectl wrapper (namespace pinning,
validation, write gate) and the n8n client (mocked HTTP). If `import mcp` fails,
the local `.venv` is stale after a Python upgrade — delete it and recreate it.

### Eval harness

`mcp/eval/run_eval.py` runs real tasks end-to-end to check whether the toolset
actually helps. It needs a live Ollama with the model pulled (it calls the model,
unlike the unit tests):

```bash
cd mcp
python eval/run_eval.py --list          # show the tasks
python eval/run_eval.py                 # all runnable tasks
python eval/run_eval.py --only 3 --verbose
```

Each task sets up a scratch workspace, runs one prompt through
`agent_core.run_agent()`, then checks the resulting files (or runs them). Exit
code is `0` only if every selected task passes. The workspace defaults to
`mcp/eval-workspace` and is wiped between tasks; override with `--workspace` or
`AGENT_WORKSPACE`. Tasks: create-and-run, fix-failing-python, targeted-edit,
multi-file-create, refactor-rename, add-cli-flag, cleanup-dead-file,
fix-and-keep-tests, plus four optional live tasks (`k8s-inspect`, `n8n-list`,
`pod-triage`, `deploy-health`) that are **skipped** when no cluster / n8n API key
is reachable. `pod-triage` deploys a bad-image Deployment into `auton-apps`,
expects a written triage report naming the pod and saying a human is needed, and
deletes the Deployment afterwards; `deploy-health` checks a healthy Deployment
is read from the snapshot instead of invented.

The checks are behavioural: they execute the result, and the two test-fixing
tasks fail if the model rewrote the test file instead of the implementation
(a cheap way for a 7B model to "pass").

Cross-check: does Ollama use the GPU? `ollama ps` prints a `PROCESSOR` column
(`100% GPU` = full offload). If it says `CPU`, the Vulkan/ROCm path is broken.

---

## Access without port mappings

This is the safe default for the current cluster (no published NodePorts;
everything is ClusterIP):

```bash
# agent (must match the Deployment label/name)
kubectl -n auton port-forward --address 127.0.0.1 svc/auton 8080:8080
curl -s http://localhost:8080/health

# ollama
kubectl -n auton port-forward svc/ollama-service 11434:11434

# n8n
kubectl -n auton port-forward svc/n8n 5678:5678
```

To get permanent host access instead, recreate the cluster once with the
`80:80@loadbalancer` mapping from [step 1](#1-create-the-cluster) and use the
ingress hosts.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `context was not found for specified context: k3d-ai-cluster`, or every `kubectl` call fails | The k3d node containers stopped (`docker ps -a` shows `Exited (137)`) | `k3d cluster start ai-cluster` then `k3d kubeconfig merge ai-cluster --kubeconfig-merge-default --kubeconfig-switch-context`; PVC data survives a restart |
| `amd.com/gpu` missing from node capacity | Device plugin not running / `/dev/kfd` not mounted | `kubectl -n kube-system logs ds/amdgpu-device-plugin-daemonset`; recreate the cluster with `-v /dev/kfd:/dev/kfd -v /dev/dri:/dev/dri` |
| Ollama pod `Pending`, `0/1 nodes ... amd.com/gpu` | Node has no GPU resource, or another Ollama holds it | Complete the device-plugin step; scale down the other Ollama (`kubectl -n <old-ns> scale deploy/ollama --replicas=0`) |
| `ollama ps` shows `CPU` | Vulkan/ROCm not available in the container | Confirm `-v /dev/dri` passthrough and host Mesa/RADV; check `kubectl logs deploy/ollama` |
| `ollama ps` shows `80%/20% CPU/GPU`, ~5 tok/s | 16k context + f16 KV cache does not fit in 8 GB shared with the desktop | Keep `OLLAMA_FLASH_ATTENTION=1` and `OLLAMA_KV_CACHE_TYPE=q8_0`, or lower `AGENT_NUM_CTX` |
| Agent repeats the same tool call until `max_steps` | Model loop (small models do this) | The loop guard nudges then stops with `stop_reason=repeat`; lower `AGENT_REPEAT_LIMIT` to stop sooner |
| Agent rewrites a test assertion instead of the bug | 7B models "fix" red output by editing the test | The edit tools refuse to remove/change assertions in test files (`AGENT_ALLOW_TEST_EDITS=1` disables it); fix the implementation |
| `401 unauthorized` from `/run` | Missing/wrong bearer token | Send `Authorization: Bearer <token>`; read it from Secret `auton-api-token` |
| Agent exits at startup: `refusing to start: AGENT_API_TOKEN is not set` | Secret missing or not mounted | Create `auton-api-token` and re-apply the deployment |
| `503 agent has no AGENT_API_TOKEN configured` | Service started with an empty token | Same as above; never rely on "no token means open" |
| Kubernetes tool returns `Forbidden` | RBAC does not allow that verb/resource | Check `kubectl auth can-i --as=system:serviceaccount:auton:auton-agent <verb> <resource> -n auton`; secrets are intentionally denied |
| Kubernetes tool fails with `401` and `localhost:8080` in the URL | kubectl dropped the in-cluster config (it does that when `--request-timeout` is set) and hit the agent's own API | Fixed in `k8s_tools.py` by writing an explicit in-cluster kubeconfig; rebuild the image if you see it |
| `n8n tools: no n8n API key available` | Secret `n8n-api` missing, or the kubelet has not synced the optional volume yet | Create the Secret, wait ~1 min or `rollout restart deploy/auton`; verify `kubectl -n auton exec deploy/auton -- cat /etc/n8n-api/api-key` |
| n8n tool returns `401`/`403` | API key wrong, revoked, or missing scopes | Create a new key with the scopes listed above and run `scripts/rotate-n8n-api-key.sh --key-file <file>` |
| Rotated key, agent still uses the old one | Projected Secret volumes refresh asynchronously (no instant signal) | The rotation script waits for it and falls back to `rollout restart`; check the file with `kubectl exec` |
| n8n webhook returns `404` | Workflow is not active, or the path is wrong | Activate it (`n8n_activate` / UI); paths are `/webhook/<path>` |
| Workflow returns `401` when calling the agent | n8n pod lacks `AGENT_API_TOKEN`, or `N8N_BLOCK_ENV_ACCESS_IN_NODE` is true | Check the deployment env and re-apply |
| `model "qwen2.5-coder:7b-instruct" not found` | Model never pulled, or new PVC | `kubectl -n auton exec deploy/ollama -- ollama pull qwen2.5-coder:7b-instruct` |
| New pods stuck `ContainerCreating` on a volume | RWO PVC still mounted by the old pod | Deployments already use `Recreate`; if it happens anyway, `kubectl -n auton delete pod <old-pod>` |
| n8n UI login loop over HTTP | Secure-cookie enforcement | `N8N_SECURE_COOKIE=false` (already set) |
| `*.localhost` returns 404 | Ingress not applied / no `80:80@loadbalancer` | Apply `yamls/ingress.yaml` and recreate the cluster with the loadbalancer mapping |
| Agent always returns a JSON blob as `final` | Model emitted a tool call the parser could not match | The loop already retries JSON in text and fenced blocks once; check the log for `malformed tool call` |
| `apply_patch` returns `Block N failed: not found` | The SEARCH block does not match the file exactly | Re-read the range with `read_file` and copy exact lines (trailing whitespace and quote style are tolerated, indentation is not) |
| `429 queue is full` | More than `AGENT_MAX_QUEUE` tasks pending | Wait, raise `AGENT_MAX_QUEUE`, or use the async API and poll |
| Long task dies at ~300 s in n8n | n8n HTTP timeout, not the agent | Use `POST /run?async=1` + `/jobs/{id}` (generated pipelines already poll), or raise the workflow timeout |
| Agent forgets earlier steps | Ollama is truncating the prompt | Raise `AGENT_NUM_CTX` (and check available VRAM); lower `AGENT_MAX_OBSERVATION_CHARS` if needed |
| `git is not installed in this image` | Image predates the tool upgrade | Rebuild + import the image and `rollout restart` |
| `kubectl: not found` inside the pod | Image built before kubectl was added | Rebuild `auton:local` and re-import |
| Agent stops early: `(stopped after N steps)` | Task too large for one session | Raise `MAX_STEPS`, or split the task |
| `k3d image import` succeeded but pod uses old code | `rollout restart` not done | `kubectl -n auton rollout restart deploy/auton` |
| Second Ollama appears in `default` | Someone applied `ollama-deployment.yaml.orig` | `kubectl -n default delete deploy/ollama svc/ollama-service --ignore-not-found` |
| Ops incident says `image`/`config`/`scheduling` and `needs_human` | Those causes are deliberately not auto-repaired | Fix the image/ConfigMap/capacity; the report names the exact deployment |
| The same OOM deployment is bumped every 30 min | The app leaks or allocates more than the cap | Raise `AGENT_OPS_MAX_MEMORY` or fix the app; the cooldown is per resource+cause |
| `auton-scanner`/`auton-worker`/`auton-ops` restart / liveness fails | A pass is wedged or the heartbeat file is not writable | `kubectl -n auton logs deploy/<name> --tail=100`; `/tmp` must be writable (it is an emptyDir) |
| The API CrashLoopBackOffs with `Redis is unreachable` | `AGENT_QUEUE=redis` and no Redis, and there is no memory fallback | `kubectl apply -f yamls/redis.yaml`; check `kubectl -n auton get pods,pvc` |
| `/tasks` answers `503` | The queue backend is down or unwritable | `kubectl -n auton exec deploy/redis -- redis-cli ping`; `maxmemory-policy noeviction` refuses writes when full |
| A task stays `claimed` after a worker died | The lease has not expired yet | Wait `AGENT_TASK_LEASE` (300 s); `recover()` requeues it automatically |
| A repair task ends `skipped` | Dry run, cooldown or the hourly budget | Check the log line and `/tasks/stats` (`budget_used`) |
| Two scanners report the same finding twice | Dedupe window shorter than the restart interval | Raise `AGENT_SCAN_DEDUPE_WINDOW` (default 300 s) |
| `/incidents` answers `503` | The shared workspace volume is not writable | Check `AGENT_AUDIT_DIR`/`AGENT_INCIDENTS_DIR` and the `agent-workspace` PVC |
| Ops webhook returns `404` | The `auton-ops` workflow is not active | `n8n_create_ops_pipeline(...)` or activate it in the UI |
| Slack/Telegram messages do not arrive but the webhook is 200 | `n8n-notify` Secret / env values are missing | Create the Secret, `rollout restart deploy/n8n`, check the workflow execution in n8n |

Useful checks:

```bash
kubectl -n auton get pods,pvc,svc
kubectl -n auton logs deploy/auton --tail=100
kubectl -n auton logs deploy/auton-scanner --tail=100
kubectl -n auton logs deploy/auton-worker --tail=100
kubectl -n auton logs deploy/auton-ops --tail=100
kubectl -n auton logs deploy/ollama --tail=100
kubectl -n auton exec deploy/redis -- redis-cli ping
kubectl get nodes -o jsonpath='{.items[0].status.capacity}{"\n"}'
```

Queue, incident and RBAC checks (with `TOKEN` and a port-forward on `:8080`):

```bash
curl -s "http://localhost:8080/tasks?limit=5" -H "Authorization: Bearer $TOKEN"
curl -s "http://localhost:8080/tasks/stats" -H "Authorization: Bearer $TOKEN"
curl -s "http://localhost:8080/incidents?limit=5" -H "Authorization: Bearer $TOKEN"
curl -s "http://localhost:8080/policy" -H "Authorization: Bearer $TOKEN"
curl -s "http://localhost:8080/approvals?status=pending" -H "Authorization: Bearer $TOKEN"
curl -s "http://localhost:8080/audit?incident_id=<id>" -H "Authorization: Bearer $TOKEN"
kubectl -n auton exec deploy/auton-worker -- \
  sh -c 'kubectl auth can-i patch deployments -n auton-apps;
         kubectl auth can-i patch deployments -n auton;
         kubectl auth can-i get secrets -n auton-apps'
```

---

## Safety and limitations

- **The agent can change its own namespace.** `AGENT_K8S_WRITE=1` plus the
  `auton-agent` Role allow CRUD on workloads, services, configmaps, PVCs and
  jobs in `auton` and `auton-apps`. That is deliberate ("Auton controls
  Auton"). The Role does **not** grant secrets, serviceaccounts,
  roles/rolebindings or PVC delete, and cluster-scoped access is read-only;
  ingresses can be created/updated but not deleted. Note that creating pods is
  inherently powerful: a pod spec can reference a Secret by name, so do not put
  long-lived credentials in these namespaces that the agent should never see.
- **Four identities, one per pod role.** The API (`auton-api`) can only
  `patch` the provider ConfigMap, the repair-policy ConfigMap and the provider
  Secret; the scanners (`auton-scanner`) are
  read-only; the workers (`auton-worker`) may delete pods and patch/scale
  Deployments only in `auton-apps`; the LLM executor (`auton-agent`) has repair
  rights in `auton` and `auton-apps` plus the read-only cluster view its
  `kubectl` tools need. None of them can read secrets, manage serviceaccounts
  or Roles/RoleBindings, delete a PVC or a namespace, and cluster scope is
  read-only. RBAC is the boundary, not the Python code. Image, config,
  scheduling, PVC and StatefulSet problems stay report-only.
- **Every repair is planned and policy-checked first.** A repair only reaches
  the cluster through a registered playbook, and only after the policy engine
  has allowed it. The modes are `disabled` (nothing runs), `observe-only`
  (plans are recorded, nothing is executed), `safe-auto` (the low-risk tiers
  run unattended) and `approval-required` (every mutating tier waits for a
  human). An unreadable or invalid policy **fails closed** — it denies, it
  never falls back to a permissive default. The LLM is not in this path: it
  cannot choose a repair, a command or a tier.
- **A bug in the HTTP layer cannot change the cluster.** `deployment/auton`
  runs as `auton-api` and only enqueues work; every mutation happens in a
  worker pod with its own narrow identity. The agent loop also runs in its own
  pod (`auton-ops`), so a long model call cannot take `/run`, `/tasks` or the
  UI down with it.
- **Nothing runs twice on the same resource.** A repair task takes
  `auton:lock:{namespace}/{resource}` before it runs: two workers repair
  *different* workloads in parallel and never the same one. A cooldown (30 min
  per resource+cause) and an hourly action budget (6, atomic in Redis) stop a
  flapping workload from being patched forever. `AGENT_OPS_DRY_RUN=1` records
  `detected` + `dry_run` and changes nothing.
- **At-least-once, visibly.** A worker that dies leaves its task `claimed` with
  a lease; `recover()` requeues it after `AGENT_TASK_LEASE` (300 s) instead of
  losing it. Repair playbooks are idempotent; agent jobs are not retried (a
  crash is recovered, a failure is reported).
- **The queue holds no secrets.** Redis stores task records, locks, cooldowns,
  counters and heartbeats — never a provider key or a token. `noeviction` means
  a full Redis refuses the write and the worker fails closed instead of
  dropping a repair.
- **The shared workspace is the audit trail.** The workers and the executor
  mount the same `ReadWriteOnce` PVC on single-node k3d, which is what makes
  `/audit` and `/incidents` show every mutation. On a multi-node cluster, give
  each writer its own volume and point `AGENT_INCIDENTS_DIR` at it; the ingest
  endpoint keeps the API view complete. On such a cluster also give the queue a
  real HA Redis (Sentinel/Cluster) — the single instance here is a deliberate
  single-node simplification.
- **A remote model provider sees cluster evidence leave the host.** With
  `provider=ollama` nothing leaves the machine. With OpenAI/Gemini/Claude or a
  custom endpoint, prompts and tool outputs (manifests, events, log excerpts)
  are sent to that endpoint. `data_policy=redact` masks tokens/passwords/keys
  first, but it is a pattern filter, not a guarantee; use `local-only` if the
  data must not leave the cluster at all.
- **The API has bearer auth.** `/health` is open; everything else needs
  `Authorization: Bearer $AGENT_API_TOKEN`. The token lives in Secret
  `auton-api-token`; the service fails closed without it. Do not expose port
  8080 beyond localhost/ingress without TLS in front.
- **`bash` is a real shell.** It runs with the container's permissions and the
  workspace as its working directory. The agent can execute anything the image
  allows — a slim Python image plus `git`/`ripgrep`/`bwrap`/`kubectl` in the pod,
  your whole machine on the host.
- **File tools are jailed**, not the shell. `list_directory`, `read_file`,
  `write_file`, `edit_file`, `apply_patch`, `grep`, `glob` and `fs` resolve paths
  under `AGENT_WORKSPACE` and refuse anything that escapes it. A shell command
  can still reach absolute paths.
- **`AGENT_SANDBOX=bwrap`** wraps `bash` / `run_tests` / `git` in bubblewrap
  (read-only root, writable workspace, `/tmp` tmpfs, optional `--unshare-net`).
  Use it for host CLI runs. It ships in the image but defaults to `off` in the
  pod, where the container is already the boundary — unprivileged bwrap is not
  reliable under k3s seccomp.
- **One job at a time.** `agent_service.py` runs a single FIFO worker (one GPU),
  so concurrent requests queue rather than fight for VRAM.
- **State lives in the workspace.** `todo_write` and background job logs are
  written under `.agent/`, so they persist with the `agent-workspace` PVC.
- **Data is node-local.** `local-path` PVCs do not survive `k3d cluster delete`.
  The model will need re-pulling after a recreate.
- **Single node, single GPU.** No scheduling strategy for multiple agents
  competing for the one `amd.com/gpu` resource.

---

## See also

- [`mcp/README.md`](mcp/README.md) — the coding agent in depth: MCP tools,
  dual-path tool calling, Kubernetes/n8n tools, auth, CLI vs HTTP usage.
- [Ollama](https://ollama.com) · [k3d](https://k3d.io) · [MCP](https://modelcontextprotocol.io)
  · [n8n](https://n8n.io)

## License

Not specified. Add one before distributing.
