# Auton Helm chart

Installs the Auton operation agent, its autonomous ops controller, the RBAC
boundary and the audit/incident PVC. Ollama, n8n and the ingress are optional so
the chart also fits a cluster that already runs them.

```
charts/auton/
├── Chart.yaml
├── values.yaml
├── README.md
└── templates/
    ├── _helpers.tpl
    ├── namespace.yaml       # auton + auton-apps, quota, limit range
    ├── serviceaccounts.yaml
    ├── rbac.yaml            # agent (repair), ops (narrow), cluster read-only
    ├── secrets.yaml         # API token, provider key, model ConfigMap
    ├── pvc.yaml             # workspace, optional ops/ollama/n8n claims
    ├── deployment-agent.yaml# API + UI, hardened, chown init container
    ├── deployment-ops.yaml  # autonomous controller + heartbeat probe
    ├── ollama.yaml          # optional Ollama + model-pull Job
    ├── n8n.yaml             # optional n8n + notification Secret
    └── ingress.yaml         # optional Traefik/standard Ingress
```

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
  --version 0.1.0 --namespace auton --create-namespace \
  --set agent.auth.token="$(openssl rand -hex 24)"
```

The token is only needed on the first install; on upgrade the chart reads the
existing `auton-api-token` Secret and keeps it.

Open the UI:

```bash
kubectl -n auton port-forward svc/auton 8080:8080
# http://localhost:8080/  -> "Set token" with the value above
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

**Read-only / dry-run first**:

```bash
helm install auton ./charts/auton -n auton --create-namespace \
  --set agent.auth.token="$TOKEN" \
  --set agent.env.AGENT_K8S_WRITE=0 \
  --set ops.dryRun=true
```

**Multi-node**: the workspace is `ReadWriteOnce`. Give the ops controller its
own claim and keep the ingest endpoint as the single view:

```bash
helm install auton ./charts/auton -n auton --create-namespace \
  --set agent.auth.token="$TOKEN" \
  --set ops.workspace.create=true
```

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
token helper reads the current value so n8n and the ops controller keep working.

## Values

| Key | Default | Meaning |
|---|---|---|
| `image.repository` | `docker.io/d3vhex/auton` | Agent/ops image |
| `image.tag` | chart `appVersion` | Pin a release |
| `agent.auth.token` | `""` | Bearer token; generated once when empty |
| `agent.auth.existingSecret` | `""` | Use a Secret you manage instead |
| `agent.workspace.existingClaim` | `""` | Adopt an existing workspace PVC |
| `agent.workspace.size` / `.storageClass` | `5Gi` / cluster default | New workspace PVC |
| `agent.env` | `{}` | Extra agent env, e.g. `AGENT_K8S_WRITE: "0"` |
| `ops.enabled` | `true` | Run the autonomous controller |
| `ops.interval` / `.cooldown` | `60` / `1800` | Pass interval and per-resource+cause cooldown (seconds) |
| `ops.maxActionsPerHour` | `6` | Hourly repair budget |
| `ops.maxMemory` / `.maxIncidents` | `2Gi` / `500` | OOM limit cap and incident retention |
| `ops.dryRun` | `false` | Detect and report without mutating |
| `ops.startupGrace` | `120` | Seconds a `Pending`/`NotReady` pod is ignored after it starts |
| `ops.terminationGrace` | `120` | Seconds a pod may stay `Terminating` before it counts as stuck |
| `ops.namespaces` | `[auton-apps]` | Namespaces the controller may repair |
| `ops.heartbeat` / `.apiUrl` / `.uiUrl` | `/tmp/ops-heartbeat` / `http://auton:8080` / `http://auton.localhost` | Liveness file and links used in reports |
| `ops.n8nWebhook` | `""` | n8n webhook for Slack/Telegram fan-out (empty disables) |
| `ops.workspace.create` | `false` | Give ops its own PVC (multi-node) |
| `provider.name` / `.baseUrl` / `.model` | `ollama` / `http://ollama-service:11434` / `qwen2.5-coder:7b-instruct` | Model provider |
| `provider.apiKey` | `""` | Creates Secret `auton-model` |
| `provider.dataPolicy` | `redact` | `redact` / `allow` / `local-only` |
| `ollama.enabled` | `false` | Deploy Ollama + model-pull Job |
| `ollama.gpu.resourceName` / `.count` | `amd.com/gpu` / `1` | GPU request |
| `n8n.enabled` | `false` | Deploy n8n |
| `n8n.apiKey.value` | `""` | Creates Secret `n8n-api` |
| `n8n.notify.*` | empty | Slack/Telegram targets for ops reports |
| `ingress.enabled` | `false` | Standard Ingress (`auton.localhost`) |
| `rbac.clusterReader` | `true` | Cluster read-only role for diagnosis |

## Safety

- `auton-agent`: repair rights in `auton` and `auton-apps`; **no** secrets,
  serviceaccounts, roles, namespace delete and **no** PVC delete.
- `auton-ops`: delete pods and patch/scale Deployments in `auton-apps` only;
  read-only in `auton`; same cluster read-only role.
- Every mutation goes through the audit gate (`AGENT_AUDIT_DIR` on the kept
  PVC). If the log is not writable the mutation is refused.
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
