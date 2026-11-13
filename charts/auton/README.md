# Auton Helm chart

Installs the Auton operation agent as a queue-backed system: two read-only
scanners, two repair workers, one LLM executor, the HTTP API/UI and a single
Redis instance. The RBAC boundary, the audit workspace PVC and the namespace
split are all created by the chart. Ollama, n8n and the ingress are optional so
the chart also fits a cluster that already runs them.

```
charts/auton/
├── Chart.yaml
├── values.yaml
├── README.md
└── templates/
    ├── _helpers.tpl
    ├── validate.yaml        # fail fast on a queue without Redis
    ├── namespace.yaml       # auton + auton-apps, quota, limit range
    ├── rbac.yaml            # 4 identities + their Roles/Bindings
    ├── secrets.yaml         # API token, provider key, model ConfigMap
    ├── pvc.yaml             # workspace, optional ollama/n8n claims
    ├── redis.yaml           # queue + AOF PVC + Service
    ├── deployment-agent.yaml   # API + UI (producer only)
    ├── deployment-scanner.yaml # read-only detection ×2
    ├── deployment-worker.yaml  # repair playbooks ×2 (the only writers)
    ├── deployment-ops.yaml     # LLM executor ×1
    ├── ollama.yaml          # optional Ollama + model-pull Job
    ├── n8n.yaml             # optional n8n + notification Secret
    └── ingress.yaml         # optional Traefik/standard Ingress
```

## Pods and identities

| Pod | Replicas | ServiceAccount | May do |
|---|---|---|---|
| `auton` | 1 | `auton-api` | `patch` the named provider ConfigMap + Secret only |
| `auton-scanner` | 2 | `auton-scanner` | `get/list/watch` in `auton-apps` |
| `auton-worker` | 2 | `auton-worker` | read + delete pods + patch/scale Deployments in `auton-apps` |
| `auton-ops` | 1 | `auton-agent` | repair in both namespaces + cluster read-only |
| `redis` | 1 | none | queue state, locks, cooldown, budget |

The split is the point: a bug in the HTTP layer cannot delete a pod, a bug in
the scanner cannot mutate anything, and a repair playbook cannot touch the API,
Redis or its own Deployment.

## Install

Images are published to Docker Hub as `docker.io/d3vhex/auton` (see
`scripts/publish.sh`). The chart itself is published as an OCI artifact under
`oci://registry-1.docker.io/d3vhex/charts`.

Until the first push, test the chart on k3d with the locally imported image:

```bash
docker build -t auton:local ./mcp && k3d image import auton:local -c ai-cluster
helm install auton ./charts/auton -n auton --create-namespace \
  --set image.repository=auton --set image.tag=local \
  --set agent.auth.token="$(openssl rand -hex 24)"
```

```bash
# from this repository
helm install auton ./charts/auton \
  --namespace auton --create-namespace \
  --set agent.auth.token="$(openssl rand -hex 24)"

# or straight from Docker Hub
helm install auton oci://registry-1.docker.io/d3vhex/charts/auton \
  --version 0.2.0 --namespace auton --create-namespace \
  --set agent.auth.token="$(openssl rand -hex 24)"
```

The token is only needed on the first install; on upgrade the chart reads the
existing `auton-api-token` Secret and keeps it.

Open the UI:

```bash
kubectl -n auton port-forward svc/auton 8080:8080
# http://localhost:8080/  -> "API access" with the value above
```

## Common setups

**Existing Ollama** (the k3d runbook, or a remote provider):

```bash
helm install auton ./charts/auton -n auton --create-namespace \
  --set agent.auth.token="$TOKEN" \
  --set provider.name=ollama \
  --set provider.baseUrl=http://ollama-service:11434
```

**Remote provider** (`openai`, `gemini`, `anthropic`, `custom`). The local model
is never contacted when the provider is not `ollama`:

```bash
helm install auton ./charts/auton -n auton --create-namespace \
  --set agent.auth.token="$TOKEN" \
  --set provider.name=openai \
  --set provider.baseUrl=https://api.openai.com/v1 \
  --set provider.model=gpt-4o-mini \
  --set provider.apiKey="$OPENAI_API_KEY"
```

`provider.dataPolicy=redact` (default) masks tokens/passwords/keys before
anything leaves the cluster; `local-only` refuses a remote provider.

**Everything in one release** (Ollama with the GPU resource, n8n and ingress):

```bash
helm install auton ./charts/auton -n auton --create-namespace \
  --set agent.auth.token="$TOKEN" \
  --set ollama.enabled=true \
  --set n8n.enabled=true \
  --set ingress.enabled=true
```

**Report-only first** (scanners queue, workers record `dry_run`):

```bash
helm install auton ./charts/auton -n auton --create-namespace \
  --set agent.auth.token="$TOKEN" \
  --set worker.dryRun=true
```

**Read-only discovery only** (no repair workers at all):

```bash
helm install auton ./charts/auton -n auton --create-namespace \
  --set agent.auth.token="$TOKEN" \
  --set worker.enabled=false
```

**Managed Redis** instead of the chart's single instance:

```bash
helm install auton ./charts/auton -n auton --create-namespace \
  --set agent.auth.token="$TOKEN" \
  --set redis.enabled=false \
  --set queue.redisUrl=redis://my-redis:6379/0
```

**Multi-node**: the workspace is `ReadWriteOnce`. On a multi-node cluster give
every writer its own volume by setting `AGENT_AUDIT_DIR`/`AGENT_INCIDENTS_DIR`
through `worker.env` / `ops.env` and rely on `POST /incidents/ingest` for the
single view in the UI.

## Adopting an existing k3d install

`helm install` refuses resources it does not own. With Helm >= 3.17 use
`--take-ownership`; on the repository's k3d cluster also point the chart at the
existing PVC (otherwise the chart would try to clear its immutable
`storageClassName`):

```bash
helm install auton ./charts/auton -n auton --take-ownership \
  --set agent.workspace.existingClaim=agent-workspace
```

Existing Secrets (`auton-api-token`, `auton-model`, `n8n-api`) are reused: the
token helper reads the current value so n8n and the workers keep working.

## Values

| Key | Default | Meaning |
|---|---|---|
| `image.repository` | `docker.io/d3vhex/auton` | Shared image for every pod |
| `image.tag` | chart `appVersion` | Pin a release |
| `agent.auth.token` | `""` | Bearer token; generated once when empty |
| `agent.auth.existingSecret` | `""` | Use a Secret you manage instead |
| `agent.workspace.existingClaim` | `""` | Adopt an existing workspace PVC |
| `agent.workspace.size` / `.storageClass` | `5Gi` / cluster default | New workspace PVC |
| `agent.env` | `{}` | Extra API env |
| `queue.backend` | `redis` | `redis` (cluster) or `memory` (local) |
| `queue.prefix` | `auton` | Redis key prefix |
| `queue.redisUrl` | derived | Override the queue URL |
| `queue.redisExistingSecret` | `""` | Secret with key `password` |
| `queue.lease` | `300` | Seconds before a crashed worker's task is requeued |
| `queue.maxAttempts` / `.backoff` | `3` / `30,120,300` | Repair retries |
| `queue.gpuLockTtl` | `900` | Global LLM slot TTL |
| `redis.enabled` | `true` | Run the single Redis instance |
| `redis.maxMemory` / `.appendFsync` | `256mb` / `everysec` | `noeviction` is fixed |
| `redis.persistence.size` | `1Gi` | AOF volume |
| `scanner.enabled` / `.replicas` | `true` / `2` | Read-only detection |
| `scanner.interval` | `60` | Pass interval (seconds) |
| `scanner.dedupeWindow` | `300` | Concurrent duplicate suppression |
| `scanner.cooldown` | `1800` | Per-resource+cause suppression |
| `scanner.startupGrace` | `120` | Ignore young `Pending`/`NotReady` pods |
| `scanner.terminationGrace` | `120` | Seconds before `Terminating` counts as stuck |
| `worker.enabled` / `.replicas` | `true` / `2` | Repair playbooks |
| `worker.cooldown` | `1800` | Same cooldown, enforced by the worker too |
| `worker.maxActionsPerHour` | `6` | Hourly repair budget |
| `worker.maxMemory` / `.maxIncidents` | `2Gi` / `500` | OOM cap and retention |
| `worker.dryRun` | `false` | Record `detected` + `dry_run`, never mutate |
| `worker.namespaces` | `[auton-apps]` | Namespaces a playbook may repair |
| `worker.n8nWebhook` | `""` | n8n webhook for Slack/Telegram fan-out |
| `ops.enabled` / `.replicas` | `true` / `1` | LLM executor |
| `ops.poll` / `.heartbeat` | `5` / `/tmp/agent-heartbeat` | Queue poll and liveness |
| `provider.name` / `.baseUrl` / `.model` | `ollama` / `http://ollama-service:11434` / `qwen2.5-coder:7b-instruct` | Model provider |
| `provider.apiKey` | `""` | Creates Secret `auton-model` |
| `provider.dataPolicy` | `redact` | `redact` / `allow` / `local-only` |
| `policy.create` | `true` | Create ConfigMap `auton-policy` and mount it into the API + workers |
| `policy.mode` | `safe-auto` | `disabled` / `observe-only` / `safe-auto` / `approval-required` |
| `policy.autoApproveTiers` / `.approvalTiers` / `.denyTiers` | `[0,1,2]` / `[3]` / `[]` | Risk tiers that run unattended, that wait for a human, that are refused |
| `policy.protectedNamespaces` / `.protectedLabels` | `kube-system`, … / `auton.io/protect=true` | Targets the platform may never repair |
| `policy.rules` | `[]` | Ordered rules; the first match wins (`match` + `action`/`tier`) |
| `policy.approvalTimeoutSeconds` | `3600` | How long a pending approval stays decidable |
| `ollama.enabled` | `false` | Deploy Ollama + model-pull Job |
| `n8n.enabled` | `false` | Deploy n8n |
| `ingress.enabled` | `false` | Standard Ingress (`auton.localhost`) |
| `rbac.clusterReader` | `true` | Cluster read-only role for the executor |

## Upgrading from 0.1.0

0.1.0 had one `auton-ops` pod running `ops_controller.py` and a single
`auton-agent` identity. 0.2.0 replaces it with the scanner/worker/executor split
and three new ServiceAccounts. `ops.interval`, `ops.cooldown`,
`ops.maxMemory`, `ops.maxIncidents`, `ops.dryRun`, `ops.namespaces`,
`ops.n8nWebhook` and `ops.workspace.*` moved to `scanner.*` / `worker.*`; the
Deployment `auton-ops` now runs `agent_worker.py`. Rename the values in your
`-f` file before upgrading, and make sure the image contains the new modules
(`agent_worker.py`, `ops_scanner.py`, `ops_worker.py`, `ops_queue.py`,
`task_store.py`).

## Safety

- `auton-api`: `patch` on exactly `configmaps/auton-model-config` and
  `secrets/auton-model`; **no** read or list of anything else.
- `auton-scanner`: read-only in `auton-apps`.
- `auton-worker`: delete pods and patch/scale Deployments in `auton-apps` only;
  no create, no configmaps, no exec, no secrets, no PVC verbs.
- `auton-agent`: repair rights in `auton` and `auton-apps`; **no** secrets,
  serviceaccounts, roles, namespace delete and **no** PVC delete.
- Every mutation goes through the audit gate (`AGENT_AUDIT_DIR` on the kept
  PVC). If the log is not writable the mutation is refused.
- Redis holds task state, locks, cooldowns and counters — never a Secret value.
- PVCs and the API-token Secret carry `helm.sh/resource-policy: keep`, so
  `helm uninstall` does not destroy the audit history.

## Uninstall

```bash
helm uninstall auton -n auton
```

Namespaces, PVCs and the token Secret are kept by design. Delete them manually
if you want a clean slate:

```bash
kubectl delete namespace auton auton-apps
```
