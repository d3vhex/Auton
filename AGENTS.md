# AGENTS.md

Infra/runbook repo for a single-host k3d + AMD GPU AI lab. There is no build
system, linter or CI. The one automated check is the pytest suite under
`mcp/tests/`. The root `README.md` is the authoritative runbook;
`mcp/README.md` covers the agent internals. When they disagree with code or
manifests, trust the code/manifests.

## Cluster shape

- k3d cluster `ai-cluster`, application namespaces `auton` (agent, Ollama, n8n)
  and `auton-apps` (workloads the operation agent may repair without being
  asked); the AMD device plugin lives in `kube-system` and must exist before
  pods can request `amd.com/gpu`.
- Workloads/services all named `auton`: Deployment `auton`, Service `auton:8080`,
  image `auton:local`. Plus `ollama-service:11434`, `n8n:5678` and
  `redis:6379`. **All Services are ClusterIP**; Traefik hosts are
  `auton`/`agent`/`ollama`/`n8n`.localhost.
- Five pod roles, four ServiceAccounts (`yamls/auton-rbac.yaml`):
  - `auton` — HTTP API + UI, **producer only** under `auton-api` (may only
    `patch` the named ConfigMap `auton-model-config` and Secret `auton-model`).
  - `auton-scanner` ×2 — read-only detection under `auton-scanner`; snapshots
    `auton-apps` and submits repair tasks to the queue.
  - `auton-worker` ×2 — the playbooks, the only writers, under `auton-worker`
    (read + pod delete + Deployment patch/scale in `auton-apps` only).
  - `auton-ops` ×1 — the LLM executor (`agent_worker.py`) under `auton-agent`
    (repair in both namespaces + cluster read-only).
  - `redis` ×1 — one instance, AOF on `redis-data`, no ServiceAccount, no
    password by default (ClusterIP only). It holds task hashes, streams, locks,
    cooldowns, the hourly budget and the GPU slot — never a Secret value.
- `AGENT_QUEUE` selects the backend: `memory` (default for local runs/tests) or
  `redis` (the cluster manifests). **No silent fallback** — with `redis` and an
  unreachable server the API refuses to start. The scanners/workers/executor
  also fail closed rather than dropping work.
- Two queues: `repair` (critical/normal streams) and `agent`. Different
  resources run in parallel, the same resource is serialised by
  `auton:lock:{ns}/{resource}`; a task lease (300 s) makes a crashed worker's
  task recoverable (`TaskStore.recover()`), and repair retries reuse the same
  incident.
- `auton-worker`/`auton-ops` share the `agent-workspace` PVC (single-node RWO)
  so their audit log and incident files are the ones the API reads. The scanner
  deliberately does **not** mount it (`/tmp` emptyDir + a private incident dir).
- Manifests live in `yamls/` (not the repo root).
- PVCs use `local-path`: data survives pod restarts and a `k3d cluster
  stop`/`start` but **not** `k3d cluster delete`. The model is pulled onto the
  PVC, not baked into an image.
- k3d runs the cluster as Docker containers. If they are gone/stopped (host
  suspend, Docker restart, external SIGKILL), `kubectl` reports
  `context was not found for specified context: k3d-ai-cluster`. Recover with
  `k3d cluster start ai-cluster` and then re-merge the context:
  `k3d kubeconfig merge ai-cluster --kubeconfig-merge-default --kubeconfig-switch-context`.
  Re-running pulls is unnecessary — the model and n8n data are still on the PVCs.
- k3d publishes only ports declared at cluster creation. Check with
  `docker port k3d-ai-cluster-server-0`; if it is empty, use
  `kubectl -n auton port-forward svc/<name> <local>:<remote>` instead of
  expecting `localhost:8080` to work. The current cluster has no NodePort
  mappings, so port-forward is the safe default.
- If `kubectl` tries `localhost:8080`, the k3d context is not merged:
  `k3d kubeconfig merge ai-cluster --kubeconfig-merge-default --kubeconfig-switch-context`.
- The agent runs as ServiceAccount `auton-agent` with a Role in **both**
  `auton` and `auton-apps` (`yamls/auton-rbac.yaml`). RBAC, not the tool code,
  is the security boundary: no secrets, serviceaccounts, roles/rolebindings,
  no PVC delete, no namespace delete. Cluster scope is read-only via the
  `auton-cluster-reader` ClusterRole (nodes, PVs, CRDs, storage classes,
  apiservices, metrics). `pods/exec` is granted because diagnosis needs it.
- `auton-api` is the API pod's *narrow* identity (patch only ConfigMap
  `auton-model-config` and Secret `auton-model`, by `resourceNames`). The API
  Deployment now uses it: tool execution moved to `deployment/auton-ops`, so a
  bug in the HTTP/UI layer cannot delete a pod. `tests/test_rbac.py` enforces
  the shape of all four identities.

## Deploying changes

- Apply from the repo root with `kubectl apply -f yamls/<file>`. Apply
  `yamls/namespace.yaml` and `yamls/auton-apps.yaml` before namespaced
  resources.
- Bootstrap order: `namespace.yaml` → `auton-apps.yaml` → Secret
  `auton-api-token` → `auton-rbac.yaml` → `auton-model.yaml` →
  `ollama-deployment.yaml` → pull the model → build/import `auton:local` →
  `redis.yaml` → `agent-deployment.yaml` → `auton-ops-deployment.yaml` →
  `auton-scanner-deployment.yaml` → `auton-worker-deployment.yaml` →
  `n8n-deployment.yaml` → `ingress.yaml`.
  Redis comes **before** the API: `AGENT_QUEUE=redis` with no reachable Redis
  makes the service refuse to start.
- The agent image is local-only, not from a registry:
  ```bash
  docker build -t auton:local ./mcp
  k3d image import auton:local -c ai-cluster
  kubectl apply -f yamls/redis.yaml
  kubectl apply -f yamls/agent-deployment.yaml yamls/auton-ops-deployment.yaml \
    yamls/auton-scanner-deployment.yaml yamls/auton-worker-deployment.yaml
  kubectl -n auton rollout restart deploy/auton deploy/auton-ops \
    deploy/auton-scanner deploy/auton-worker   # after any rebuild
  ```
  `imagePullPolicy: IfNotPresent` is why the imported image is used.
- `scripts/install.sh` is the executable runbook (cluster → GPU plugin → Ollama
  → model → image → Redis → API/executor/scanners/workers → n8n → ingress, then
  a verification pass). It is idempotent, keeps an existing token/PVC, and
  `--verify` re-runs only the checks (11 of them). Use `AUTON_IMAGE` to deploy a
  registry image instead of `auton:local`.
- `mcp/Dockerfile` explicitly `COPY`s the Python modules (currently
  `agent_core.py agent_server.py agent_client.py agent_service.py
  agent_worker.py audit.py incidents.py k8s_tools.py llm.py n8n_tools.py
  ops_controller.py ops_queue.py ops_scanner.py ops_worker.py playbooks.py
  provider_admin.py task_store.py`) **and** the `ui/` directory. Any new
  module/page must be added there or it will be missing inside the pod. The
  image also installs `git`, `ripgrep`, `bubblewrap` and **kubectl v1.35.5**
  (matches the server minor); `tests/` and `.venv/` are excluded via
  `.dockerignore`.
- Never apply `yamls/ollama-deployment.yaml.orig`; it creates a second Ollama in
  the `default` namespace that competes for the GPU.
- Do not change the Deployments' `strategy: Recreate` while PVCs are
  `ReadWriteOnce`; a rolling update would deadlock on the mounted volume.
- The UI is `mcp/ui/index.html`, served by `agent_service.py` at `/` and `/ui`.
  It is deliberately one file: no bundler, no CDN, no external assets (the pod
  may have no egress). It keeps the bearer token in `localStorage` and talks to
  the same origin only. Keep it that way; putting data in the page would make
  the open route a leak. Auth is explained, not hidden: the **API access**
  modal says why a token exists, prints the `kubectl get secret` command, and
  `GET /auth/check` validates before saving. A banner stays while no valid
  token is stored and any `401` re-opens the modal — do not go back to a bare
  `prompt()`. Only one component may write the health chip (`refreshHealth`);
  the old duplicate writes produced a stale “API unreachable” next to “API ok”.
- The Provider tab is a **write** form (`POST /provider`,
  `DELETE /provider/key`) backed by `mcp/provider_admin.py`: validate first,
  `audit.begin("provider_update", …)` before the write, key value never in the
  audit record, `400` for anything the agent would refuse at request time
  (remote without key, `local-only` + remote, `custom` without base_url).
- `charts/auton` is the Helm equivalent of `yamls/` (API + scanner + worker +
  LLM executor + Redis + RBAC + PVCs, optional Ollama/n8n/ingress). Resource
  names are intentionally fixed (`auton`, `auton-ops`, `auton-scanner`,
  `auton-worker`, `redis`) because docs and generated n8n workflows use
  `auton:8080`; do not add a release-name prefix without changing them. The
  chart's `validate.yaml` fails the render on `queue.backend=redis` without
  Redis. `scripts/publish.sh` builds/pushes `docker.io/d3vhex/auton` and the OCI
  chart (`oci://registry-1.docker.io/d3vhex/charts/auton`); `--no-push` is the
  local check. PVCs and the API-token Secret carry
  `helm.sh/resource-policy: keep`. On this k3d cluster adopt the existing
  install with `--take-ownership` and
  `--set agent.workspace.existingClaim=agent-workspace` (the bound PVC's
  `storageClassName` is immutable).
- `yamls/gpu-test.yaml` is a one-shot `rocminfo` smoke test. Delete the pod after
  use.
- The model is not shipped in the Ollama image. After a fresh PVC or cluster
  recreate, pull it again:
  `kubectl -n auton exec deploy/ollama -- ollama pull qwen2.5-coder:7b-instruct`.
- Secrets:
  - `auton-api-token` key `token` — bearer token for the agent API; n8n gets the
    same value via its own `AGENT_API_TOKEN` env var.
  - `n8n-api` key `api-key` — n8n public-API key, mounted read-only at
    `/etc/n8n-api/api-key` (volume is `optional: true`). It is read per call, so
    rotating the Secret needs no pod restart — but the mount is a *projected*
    Secret volume, so kubelet refreshes it asynchronously (no immediate signal).
    Rotate with `scripts/rotate-n8n-api-key.sh --key-file <file>`: it applies the
    Secret, polls the file inside the pod, restarts `deploy/auton` only as a
    fallback, then verifies the key through `n8n_workflows("list")`.
  - `auton-model` key `api-key` — API key for the active remote provider
    (empty for `ollama`). Written by `scripts/set-provider.sh` from a file or
    stdin, never from argv, **or** from the UI through `POST /provider`
    (`mcp/provider_admin.py`). Read per request, so a key rotation needs no
    restart. `yamls/auton-model.yaml` also defines ConfigMap
    `auton-model-config` (provider/base_url/model/max_tokens/data_policy).
    Only the `auton-api` ServiceAccount may `patch` these two named objects;
    the audit record says “key set/cleared”, never the value.

## Agent code (`mcp/`)

- Flow: `agent_service.py` (stdlib HTTP) -> FIFO job queue -> one worker thread
  runs `agent_core.run_agent()` -> spawns `agent_server.py` over MCP stdio ->
  `llm.chat()` talks to the configured provider (local Ollama by default).
  `agent_client.py` is the CLI equivalent.
- `llm.py` is the provider layer: `ollama` (default), `openai`, `gemini`,
  `anthropic`/`claude`, and `custom` (any OpenAI-compatible server: vLLM, LM
  Studio, llama.cpp). Configuration is read **per request** from
  `/etc/auton-model/config/{provider,base_url,model,max_tokens,data_policy}`
  (ConfigMap `auton-model-config`) and `/etc/auton-model/secret/api-key`
  (Secret `auton-model`, `optional: true`), with env vars as the local-dev
  fallback. Switch with `scripts/set-provider.sh`; it waits for the projected
  volume and prints `llm.provider_info()`, restarting the pod only if the
  projection stalls. Remote tool calls are serialised into the JSON-text shape
  the loop already parses, so there is one history format. **No fallback**: when
  the provider is not `ollama`, the local model is never contacted; a missing
  key or unreachable endpoint is an error, not a silent downgrade.
  `data_policy=redact` (default) masks tokens/passwords/keys/bearer headers
  before anything leaves the cluster; `local-only` refuses remote providers.
  `GET /provider` (masked) and `POST /provider/test` expose the state.
- `agent_core.py` accepts both native Ollama `tool_calls` and JSON tool calls
  parsed from message text (`extract_tool_call`), repairs odd argument shapes
  with `coerce_args()` (string / `[]` / bare value), and compacts old
  observations. Loop guards stop a model that repeats the same call, retries
  the same failing call, returns the **same output twice in a row under a new
  signature** (e.g. `depth=13,14,15…` all clamp to the same listing),
  re-emits the observation as its answer, or stops at a plan: nudge once, then
  `stop_reason` `repeat` / `parse_failed`.
- Edit anchors tolerate trailing whitespace and quote style (`'` vs `"`) and a
  miss reports the closest line verbatim. Do not "fix" this by making anchors
  fuzzy beyond quotes: silent wrong-place edits are worse than a retry.
- `agent_server.py` uses `mcp.server.mcpserver.MCPServer`, a **v2-only** API.
  Keep `requirements.txt` at `mcp>=2,<3`; do not downgrade to MCP v1.
  34 tools total: workspace (`list_directory`, `read_file`, `write_file`,
  `edit_file`, `apply_patch`), search (`grep`, `glob`), shell (`bash`, `job`,
  `run_tests`), git (`git_status`, `git_diff`), housekeeping (`fs`,
  `todo_write`), Kubernetes (`k8s_get`, `k8s_describe`, `k8s_logs`,
  `k8s_snapshot`, `k8s_diagnose`, `k8s_events`, `k8s_wait`, `k8s_apply`,
  `k8s_patch`, `k8s_scale`, `k8s_rollout`, `k8s_delete`, `k8s_exec`), n8n
  (`n8n_workflows`, `n8n_activate`, `n8n_executions`, `n8n_webhook`,
  `n8n_create_agent_pipeline`, `n8n_create_ops_pipeline`) and the queue
  (`task_queue`). `run_command` is a deprecated alias for `bash`.
- `audit.py` is the gate for **every** mutation: `k8s_tools._audited()` calls
  `audit.begin()` (JSONL append + fsync + SQLite index under `AGENT_AUDIT_DIR`,
  default `/workspace/.ops`) *before* kubectl runs, and returns an error without
  running kubectl if the record cannot be written. `audit.finish()` records the
  outcome. Keep the fail-closed order: `begin` -> kubectl -> `finish`. Records
  carry `audit_id`/`incident_id`; `GET /audit` exposes them and validates auth.
- `k8s_tools.py`: the target namespace is an argument but must be in the
  `AGENT_NAMESPACES` allowlist (`auton,auton-apps`); `AGENT_NAMESPACE` is the
  default. Resource/name/selector are regex-validated; `AGENT_K8S_WRITE=0`
  refuses every mutation. PVC delete is refused in code *and* RBAC.
  `k8s_snapshot`/`k8s_diagnose` collect evidence in code and return a compact
  text report plus a deterministic `diagnosis`/`suggested_action`, so the 7B
  model reasons over a summary instead of raw JSON. `_kubeconfig_args()` writes
  an explicit in-cluster kubeconfig because kubectl silently drops the
  in-cluster config when `--request-timeout` is non-zero and falls back to
  `localhost:8080` (the agent's own API). **Do not remove that workaround.**
- `n8n_tools.py`: stdlib HTTP only; key read from `N8N_API_KEY_FILE` on every
  call. `n8n_create_agent_pipeline()` generates the webhook -> async job ->
  poll `/jobs/{id}` -> respond graph instead of asking the 7B model to write node
  JSON. The polling loop (Wait 5 s -> Get Job -> IF status in done/error/
  cancelled, false branch back to Wait) is why slow runs do not depend on one
  long-lived HTTP request. `n8n_create_ops_pipeline(kind="slack"|"telegram"|
  "both")` generates webhook -> Slack/Telegram HTTP nodes using
  `SLACK_OPS_WEBHOOK` / `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` from `$env`
  (no n8n credential setup).
- **Autonomous ops** (`ops_controller.py`, `playbooks.py`, `incidents.py`):
  `ops_controller` snapshots `AGENT_OPS_NAMESPACES` (default `auton-apps`) every
  `AGENT_OPS_INTERVAL` (60 s) using the same `k8s_tools._collect` the model
  uses, maps pod/deployment/PVC symptoms to deterministic causes (`oom`,
  `crashloop`, `stuck`, `rollout`, `replicas_zero`), and runs the matching
  playbook from `playbooks.py`. The LLM is **not** in this path: a 7B model
  explaining an OOMKill is useful, a 7B model choosing a kubectl patch is not.
  Image/config/scheduling/PVC problems are `report_only` -> `needs_human`.
  Each repair is wrapped in `k8s_tools.incident_scope(id)` so audit records
  carry the incident id; the playbook verifies its own result with a fresh
  read. `incidents.py` stores one atomic JSON file per incident under
  `AGENT_AUDIT_DIR/incidents` (shared with the API), statuses
  `detected/acting/resolved/needs_human/failed`, plus a local retry queue.
  Cooldown (`AGENT_OPS_COOLDOWN`, 1800 s) and an hourly action budget
  (`AGENT_OPS_MAX_ACTIONS_PER_HOUR`, 6) stop a flapping workload from being
  patched forever; `AGENT_OPS_DRY_RUN=1` reports without mutating.
  `AGENT_OPS_STARTUP_GRACE` (120 s) suppresses `Pending`/`NotReady` for a young
  pod (image pull/container start) and also keeps its Deployment from being
  reported as unavailable; `Unschedulable` is always reported immediately.
  `AGENT_OPS_TERMINATION_GRACE` (120 s) keeps a normal `kubectl delete` from
  being reported as a stuck pod. A pod with several reasons gets **one**
  finding (priority in `REASON_PRIORITY`; OOM beats CrashLoopBackOff) because
  two playbooks on the same pod race (the crashloop playbook deletes it while
  the OOM rollout is being verified).
  Reports go to `POST /incidents/ingest` and to `N8N_OPS_WEBHOOK`. Resources
  labelled `auton.io/protect=true` are never touched. Detection rules are in
  `ops_controller.REASONS` / `REPORT_ONLY`; add a playbook and a test before
  adding a cause there.
- File/search tools are jailed to `AGENT_WORKSPACE` (default `./workspace`;
  container uses `/workspace`). `bash`/`run_tests`/`git` are deliberately not
  jailed and run a real shell; `AGENT_SANDBOX=bwrap` optionally wraps them.
- The pod is hardened: `runAsNonRoot` UID/GID 10001, read-only root filesystem,
  `drop: ["ALL"]`, `/tmp` as an emptyDir (`HOME`/`TMPDIR=/tmp`,
  `PYTHONDONTWRITEBYTECODE=1`), and a root init container that `chown`s the
  workspace. `local-path` does not reliably apply `fsGroup`, so removing that
  init container leaves `/workspace` unwritable for the agent.
- Auth: `/health` (and `/`, `/ui`, which serve the bundled page) are open;
  `/run`, `/jobs*`, `/audit`, `/provider*` and `/incidents*` require
  `Authorization: Bearer $AGENT_API_TOKEN`. The service **fails closed**: it
  refuses to start without a token unless `AGENT_AUTH_DISABLED=1`, and an empty
  token answers `503`. No rate limiting. Do not expose the service publicly.
- `POST /run` stays synchronous for back-compat and waits on the worker;
  `?async=1` returns `202` + `job_id`, polled via `/jobs/{id}` (SSE at
  `/jobs/{id}/events`, cancel at `/jobs/{id}/cancel`). The generated n8n
  pipeline uses the async path.
- The system prompt opens with a **HARD RULES** block (never edit/weaken tests,
  never fake success, verify before claiming done) and `run_tests()` appends a
  `[hint]` on failure repeating "fix the implementation, not the test". Both exist
  because the 7B model rewrites assertions when it sees red output. Keep them
  near the top / at the failure site — a later "also, don't cheat" line is
  ignored.
- Prose alone was not enough: `write_file`/`edit_file`/`apply_patch` also refuse
  to remove or change an `assert` line in `test_*.py` / `*_test.py` /
  `conftest.py` (returns a JSON error, writes nothing). Creating a test file or
  *adding* assertions is allowed; `AGENT_ALLOW_TEST_EDITS=1` disables the guard
  for tasks that really do change tests. `bash` can still bypass it — the guard
  is for the model's default failure mode, not a security boundary.
- Any new tool must also be added to `EXPECTED` in `mcp/tests/test_server_stdio.py`
  so the transport test covers it.
- After changing an n8n workflow, remember the imported files are inactive and
  hardcode `http://auton:8080/run?async=1` + `/jobs/{id}` with
  `Authorization: Bearer {{ $env.AGENT_API_TOKEN }}`; activate the webhook
  workflow in the UI. Keep per-request HTTP timeouts short (generated nodes use
  30 s) and let the polling loop, not a long request, absorb slow 7B runs.

## Local development

```bash
cd mcp
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

# Use the cluster's Ollama (all Services are ClusterIP):
kubectl -n auton port-forward svc/ollama-service 11434:11434

OLLAMA_HOST=http://localhost:11434 AGENT_WORKSPACE="$PWD/workspace" \
  python agent_client.py "Write greet.py and run it."

# Same env for the HTTP front-end (auth off only for local runs):
OLLAMA_HOST=http://localhost:11434 AGENT_WORKSPACE="$PWD/workspace" \
AGENT_AUTH_DISABLED=1 python agent_service.py
```

Host runs of the Kubernetes tools use your kubeconfig and `AGENT_NAMESPACE`
(default `auton`); the n8n tools need `N8N_API_URL`/`N8N_WEBHOOK_BASE` pointed at
a port-forward and a key in `N8N_API_KEY` or `N8N_API_KEY_FILE`.

If the local `.venv` cannot import `mcp`, delete and recreate it: its symlinks
point at the system Python, so a Python upgrade leaves it broken.

## Verification

- Run the agent test suite before rebuilding the image:
  `cd mcp && python -m pytest tests -q` (no cluster or GPU needed; expect
  `246 passed`). `tests/test_rbac.py` parses `yamls/` and fails if a Role ever
  grants secrets or PVC delete.
- To measure real model behaviour, run the eval harness against a live Ollama:
  `cd mcp && python eval/run_eval.py` (or `--list`, `--only N`, `--verbose`).
  It wipes `eval-workspace/` per task; exit code 0 means every task passed.
  Expect ~6-7/8 for the 7B model on the coding tasks; the failures move between
  runs. Checks run the produced code and fail if a test file was rewritten
  instead of the implementation. Tasks 9-12 (`k8s-inspect`, `n8n-list`,
  `pod-triage`, `deploy-health`) are skipped unless a live cluster / n8n API key
  is reachable; `pod-triage` creates and deletes a bad-image Deployment in
  `auton-apps`.
- GPU check after loading a model: `ollama ps` must say `100% GPU`. If it says
  `80%/20% CPU/GPU`, `OLLAMA_FLASH_ATTENTION=1` and
  `OLLAMA_KV_CACHE_TYPE=q8_0` are missing (see
  `yamls/ollama-deployment.yaml`).
- After a manifest change, run `kubectl -n auton rollout status deploy/<name>`
  and inspect `kubectl -n auton get pods,pvc,svc`.
- Agent: `curl -s <endpoint>/health` (open); `POST /run` with
  `{"task":"..."}` plus `Authorization: Bearer <token>` and expect
  `{"final": ..., "steps": [...], "stop_reason": ...}`. Long tasks:
  `POST /run?async=1`, then poll `/jobs/{id}`. Without a token expect `401`;
  with no token configured expect `503`.
- UI: `curl -s http://localhost:8080/ | head` returns the HTML page and
  `curl -s http://localhost:8080/ui` the same (both open); `/health` must still
  return JSON. In the browser, Chat → Incidents → Audit → Provider must each
  load with the token and show `401` feedback without one.
- Helm: `helm lint charts/auton` and
  `helm template auton charts/auton -n auton | kubectl apply --dry-run=server -f -`.
  On an existing install the agent PVC must be adopted
  (`--set agent.workspace.existingClaim=agent-workspace`) or the dry run fails on
  the immutable `storageClassName`.
- `scripts/install.sh --verify` runs the token/RBAC/provider/incidents checks
  and must print six `ok` lines on a healthy install.
- Provider: `GET /provider` (auth) returns the active provider with a masked
  key; `POST /provider/test` sends one tiny completion and returns `ok`/`502`.
  `scripts/set-provider.sh --provider <name> ...` switches without a restart —
  confirm the printed agent view and `local_model_enabled`. A remote provider
  must have `local_model_enabled: false` and a missing key must fail the run
  (no local fallback).
- Kubernetes tools: from inside the pod,
  `kubectl -n auton exec deploy/auton -- python -c 'import k8s_tools;
  print(k8s_tools.k8s_get("pods"))'` lists pods, while
  `k8s_get("secrets")` returns `Forbidden` and
  `k8s_get("pods", namespace="kube-system")` returns `not allowed` (RBAC +
  allowlist are working). `k8s_snapshot()` must show a `PROBLEMS:` section and
  `k8s_diagnose(<pod>)` a `diagnosis:`/`suggested_action:` pair.
- Audit: every mutation writes two lines to `/workspace/.ops/audit.jsonl`
  (`begin` then `end`); `GET /audit` with the bearer token returns them.
  `GET /audit?incident_id=inc-...` returns the pair for one ops repair.
  Simulate a broken log by pointing `AGENT_AUDIT_DIR` at a file — the mutation
  must be refused with `refused ... audit log is not writable` and kubectl must
  not run.
- Ops controller: `kubectl -n auton logs deploy/auton-ops` must show
  `pass complete: {... "findings": N ...}` every interval (empty `auton-apps`
  -> `findings: 0`). `GET /incidents` lists records and
  `GET /incidents/{id}` one record. Live checks:
  - OOM: create a Deployment in `auton-apps` that allocates more than its
    `limits.memory`; expect an `incident ... oom` log, the limit raised by
    25% (minimum 32Mi) in `kubectl get deploy -o jsonpath`, the pod Available,
    the record `resolved`, and `/audit?incident_id=` returning the
    `k8s_patch` begin/end pair.
  - Report-only: a Deployment with a missing image must become
    `needs_human` with action `report_only` and **no** audit rows.
  - `AGENT_OPS_DRY_RUN=1` must record `detected` + `dry_run` without changing
    the Deployment.
  - From the ops pod, `kubectl auth can-i` must say `no` for
    `delete persistentvolumeclaims`, `get secrets`, and
    `patch deployments -n auton`, and `yes` for `patch deployments` in
    `auton-apps`.
- n8n ops reports: `n8n_create_ops_pipeline(kind="both")` creates and activates
  `/webhook/auton-ops`. Set `SLACK_OPS_WEBHOOK` and/or
  `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` in n8n; POSTing the notification
  payload must answer `200` immediately even if a downstream node fails.
- n8n tools: `kubectl -n auton exec deploy/auton -- python -c 'import
  n8n_tools; print(n8n_tools.n8n_workflows("list"))'`.
- End-to-end: activate the generated webhook workflow and call
  `http://n8n:5678/webhook/auton-agent` with `{"task": "..."}`; a 200 with the
  agent's JSON result proves n8n → agent → Ollama.
- GPU: `kubectl get nodes -o jsonpath='{.items[0].status.capacity.amd\.com/gpu}'`
  should print `1`; `kubectl -n auton exec deploy/ollama -- ollama ps` should
  show `100% GPU` once a model is loaded.
