# Auton Helm chart

Installs the Auton operation agent as a queue-backed system: two read-only
scanners, two repair workers, one LLM executor, the HTTP API/UI and a single
Redis instance. The RBAC boundary, the audit workspace PVC and the namespace
split are all created by the chart. Ollama, n8n and the ingress are optional so
the chart also fits a cluster that already runs them.

**This chart is the source of truth for every Kubernetes object.** The raw
manifests in `yamls/` are generated from it (see
[Generated manifests](#generated-manifests)); there is no second, hand-maintained
copy of a Deployment.

```
charts/auton-chart/
├── Chart.yaml
├── values.yaml            # defaults (the published install)
├── values-k3d.yaml        # the lab overlay that produces yamls/
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
    └── ingress.yaml         # optional Ingress or Traefik IngressRoute
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

Images are published to Docker Hub as `docker.io/d3vhec/auton` (see
`scripts/publish.sh`). The chart itself is published as an OCI artifact under
`oci://registry-1.docker.io/d3vhec/auton-chart`.

Until the first push, test the chart on k3d with the locally imported image:

```bash
docker build -t auton:local ./mcp && k3d image import auton:local -c ai-cluster
helm install auton ./charts/auton-chart -n auton --create-namespace \
  --set image.repository=auton --set image.tag=local \
  --set agent.auth.token="$(openssl rand -hex 24)"
```

```bash
# from this repository
helm install auton ./charts/auton-chart \
  --namespace auton --create-namespace \
  --set agent.auth.token="$(openssl rand -hex 24)"

# or straight from Docker Hub
helm install auton oci://registry-1.docker.io/d3vhec/auton-chart \
  --version 0.3.0 --namespace auton --create-namespace \
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
helm install auton ./charts/auton-chart -n auton --create-namespace \
  --set agent.auth.token="$TOKEN" \
  --set provider.name=ollama \
  --set provider.baseUrl=http://ollama-service:11434
```

**Remote provider** (`openai`, `gemini`, `anthropic`, `custom`). The local model
is never contacted when the provider is not `ollama`:

```bash
helm install auton ./charts/auton-chart -n auton --create-namespace \
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
helm install auton ./charts/auton-chart -n auton --create-namespace \
  --set agent.auth.token="$TOKEN" \
  --set ollama.enabled=true \
  --set n8n.enabled=true \
  --set ingress.enabled=true
```

**Report-only first** (scanners queue, workers record `dry_run`):

```bash
helm install auton ./charts/auton-chart -n auton --create-namespace \
  --set agent.auth.token="$TOKEN" \
  --set worker.dryRun=true
```

**Read-only discovery only** (no repair workers at all):

```bash
helm install auton ./charts/auton-chart -n auton --create-namespace \
  --set agent.auth.token="$TOKEN" \
  --set worker.enabled=false
```

**Without the Learn stage** (the platform keeps its fixed behaviour: no
outcomes, no adaptation):

```bash
helm install auton ./charts/auton-chart -n auton --create-namespace \
  --set agent.auth.token="$TOKEN" \
  --set learning.enabled=false
```

**Managed Redis** instead of the chart's single instance:

```bash
helm install auton ./charts/auton-chart -n auton --create-namespace \
  --set agent.auth.token="$TOKEN" \
  --set redis.enabled=false \
  --set queue.redisUrl=redis://my-redis:6379/0 \
  --set queue.redisExistingSecret=my-redis
```

The managed Redis must require a password too: the clients authenticate with
the Secret `queue.redisExistingSecret` (key `password`). For a
*passwordless* external Redis set `queue.redisAuth=false` — the chart then
sends no password at all.

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
helm install auton ./charts/auton-chart -n auton --take-ownership \
  --set agent.workspace.existingClaim=agent-workspace
```

Existing Secrets (`auton-api-token`, `auton-model`, `n8n-api`) are reused: the
token helper reads the current value so n8n and the workers keep working.

## Values

| Key | Default | Meaning |
|---|---|---|
| `image.repository` | `docker.io/d3vhec/auton` | Shared image for every pod |
| `image.tag` | chart `appVersion` | Pin a release |
| `agent.auth.token` | `""` | Bearer token; generated once when empty |
| `agent.allowWrites` | `false` | Give the LLM executor write rights (Phase 4). `false` = read-only RBAC + `AGENT_K8S_WRITE=0` |
| `agent.kubernetes.direct` | `true` | Talk to the API server directly over HTTPS (D6). `false` = `AGENT_K8S_DIRECT=0`, the kubectl path |
| `agent.auth.existingSecret` | `""` | Use a Secret you manage instead |
| `agent.workspace.existingClaim` | `""` | Adopt an existing workspace PVC |
| `agent.workspace.size` / `.storageClass` | `5Gi` / cluster default | New workspace PVC |
| `agent.env` | `{}` | Extra API env |
| `queue.backend` | `redis` | `redis` (cluster) or `memory` (local) |
| `queue.prefix` | `auton` | Redis key prefix |
| `queue.redisUrl` | derived | Override the queue URL |
| `queue.redisAuth` | `true` | The clients authenticate to the queue (`false` only for an external *passwordless* Redis) |
| `queue.redisPassword` | `""` | Inline queue password; empty = generated once and kept in the Secret |
| `queue.redisExistingSecret` | `""` | Secret with key `password` |
| `queue.redisSecretName` | `auton-redis` | Name of the generated Secret |
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
| `scanner.watch.enabled` | `true` | Kubernetes watch instead of polling |
| `scanner.watch.fastInterval` | `60` | Fast tier: reads the watch cache (seconds) |
| `scanner.watch.fullInterval` | `300` | Full tier: takes a real snapshot (seconds) |
| `scanner.watch.timeout` | `300` | Seconds one watch stream stays open |
| `scanner.watch.minGap` | `5` | Smallest gap between two event-triggered scans |
| `scanner.watch.eventTtl` | `900` | Seconds a watched Event stays in the cache (events are high churn) |
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
| `learning.enabled` | `true` | The Learn stage: automatic, bounded adaptation from the repair outcomes |
| `learning.dir` | `/workspace/.ops/learning` | Outcomes + profiles (shared PVC: the worker writes, the API reads) |
| `learning.minSamples` | `3` | Repairs of the same cause before anything is adapted |
| `learning.maxStep` | `1.0` | Hard cap of the learned memory bump (the limit may at most double) |
| `learning.cooldownMax` | `21600` | Hard cap of a learned cooldown (6 h) |
| `learning.budgetMin` | `1` | Floor of a learned per-resource action budget (per hour) |
| `learning.maxEscalations` | `3` | Consecutive failures before a resource is quarantined |
| `learning.maxOutcomes` | `500` | Outcome records kept (the oldest are pruned) |
| `ollama.enabled` | `false` | Deploy Ollama + model-pull Job |
| `ollama.env` | see `values.yaml` | Extra env for the Ollama container (`HSA_OVERRIDE_GFX_VERSION`, flash attention, q8 KV cache, `LLAMA_ARG_FIT_TARGET`) |
| `n8n.enabled` | `false` | Deploy n8n |
| `ingress.enabled` | `false` | Create the ingress object |
| `ingress.kind` | `Ingress` | `Ingress` (portable) or `IngressRoute` (Traefik CRD) |
| `ingress.name` | `auton` | Name of the ingress object |
| `ingress.entryPoints` | `[web]` | Traefik entry points (`IngressRoute` only) |
| `rbac.clusterReader` | `true` | Cluster read-only role for the executor |

### The metrics endpoint

The API pod serves a **Prometheus exposition** at `GET /metrics` (text format
`text/plain; version=0.0.4`), written by hand with the standard library only.
The endpoint needs the **bearer token**, so a Prometheus scrape must carry it:

```yaml
scrape_configs:
  - job_name: auton
    scheme: http
    metrics_path: /metrics
    static_configs:
      - targets: ["auton.auton.svc.cluster.local:8080"]
    authorization:
      type: Bearer
      credentials_file: /etc/prometheus/secrets/auton-api-token
```

The chart version is passed to the API as `AUTON_VERSION` and is exposed as the
`version` label of `auton_build_info`. There is no metrics-specific value: the
endpoint has no switch — it is always on, and it never exposes a secret.

### Direct Kubernetes access

Every pod reaches the Kubernetes API server **directly over HTTPS**, with the
standard library only — no `kubernetes` client library, no forked `kubectl` per
call. kubectl stays in the image as a **logged, counted fallback**: when the
direct path is unavailable or fails, the call falls back to kubectl and says so
(`[k8s] using kubectl for …` in the pod log; the counters are visible at
`GET /tasks/stats` and in `auton_k8s_transport_total`). An RBAC refusal is
never retried with kubectl — the API server's own message is the answer.

`agent.kubernetes.direct=false` sets `AGENT_K8S_DIRECT=0` on every pod and
forces the kubectl path, which is how the two are compared.

The chart grants no extra permission for this: the direct client can do
exactly what the ServiceAccount may do, and nothing more. RBAC is still the
boundary.

## Generated manifests

The repository's `yamls/` directory is **generated** from this chart, so the
chart and the raw manifests can never disagree:

```bash
./scripts/sync-yamls.sh          # rewrite yamls/ from the chart (needs helm)
./scripts/sync-yamls.sh --check  # fail if yamls/ drifted from the chart
```

`charts/auton-chart/values-k3d.yaml` is the overlay that reproduces the k3d lab
(`auton:local`, the `local-path` storage class, Ollama/n8n/ingress enabled,
the lab's resource requests and the Traefik `IngressRoute`). The two Secrets
that hold random values — the API token and the queue password — are **not**
rendered into it: `scripts/install.sh` creates them, so no generated manifest
ever carries a secret value.

The generated files carry a `GENERATED — DO NOT EDIT` banner and
`yamls/.chart-stamp` records the checksum of the chart inputs and of every
generated file. `mcp/tests/test_manifests.py` fails the test suite on drift and
`scripts/install.sh` refuses to install manifests that no longer match the
chart.

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
- `auton-scanner`: read-only in `auton-apps` (`get`/`list`/`watch` only — the
  watch verb is what makes detection event-driven).
- `auton-agent` (the LLM executor): **read-only** by default — `get/list/watch`
  only, no `pods/exec`. `agent.allowWrites=true` adds a separate
  `auton-agent-write` Role with the write verbs; every action stays
  audit-gated.
- `auton-worker`: delete pods and patch/scale Deployments in `auton-apps` only;
  no create, no configmaps, no exec, no secrets, no PVC verbs.
- Every mutation goes through the audit gate (`AGENT_AUDIT_DIR` on the kept
  PVC). If the log is not writable the mutation is refused.
- The Learn stage may only make the platform *more cautious*: a bigger (still
  bounded) OOM bump, a higher tier, a longer cooldown, a smaller per-resource
  budget or a quarantine. It never widens a permission, never lowers a tier,
  never changes the action and never runs without an audit record.
- Redis holds task state, locks, cooldowns and counters — never a Secret value.
- Redis **requires a password** (`queue.redisAuth=true`): the value lives in
  the Secret `auton-redis`, the clients read it from a mounted file and the
  server writes it into a config file — never in a manifest, a pod spec, an
  environment variable or a command line.
- PVCs and the API-token Secret carry `helm.sh/resource-policy: keep`, so
  `helm uninstall` does not destroy the audit history. The queue Secret does
  too: a regenerated password would lock every client out.

## Uninstall

```bash
helm uninstall auton -n auton
```

Namespaces, PVCs and the token Secret are kept by design. Delete them manually
if you want a clean slate:

```bash
kubectl delete namespace auton auton-apps
```
