# AGENTS.md

Infra/runbook repo for a single-host k3d + AMD GPU AI lab. There is no build
system, linter, CI, or git repository here. The one automated check is the
pytest suite under `mcp/tests/`. The root `README.md` is the authoritative
runbook; `mcp/README.md` covers the agent internals. When they disagree with
code or manifests, trust the code/manifests.

## Cluster shape

- k3d cluster `ai-cluster`, application namespaces `auton` (agent, Ollama, n8n)
  and `auton-apps` (workloads the operation agent may repair without being
  asked); the AMD device plugin lives in `kube-system` and must exist before
  pods can request `amd.com/gpu`.
- Workloads/services all named `auton`: Deployment `auton`, Service `auton:8080`,
  image `auton:local`. Plus `ollama-service:11434` and `n8n:5678`.
  **All Services are ClusterIP**; Traefik hosts are
  `auton`/`agent`/`ollama`/`n8n`.localhost.
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

## Deploying changes

- Apply from the repo root with `kubectl apply -f yamls/<file>`. Apply
  `yamls/namespace.yaml` and `yamls/auton-apps.yaml` before namespaced
  resources.
- Bootstrap order: `namespace.yaml` → `auton-apps.yaml` → Secret
  `auton-api-token` → `auton-rbac.yaml` → `auton-model.yaml` →
  `ollama-deployment.yaml` → pull the model → build/import `auton:local` →
  `agent-deployment.yaml` → `n8n-deployment.yaml` → `ingress.yaml`.
- The agent image is local-only, not from a registry:
  ```bash
  docker build -t auton:local ./mcp
  k3d image import auton:local -c ai-cluster
  kubectl apply -f yamls/agent-deployment.yaml
  kubectl -n auton rollout restart deploy/auton   # after any rebuild
  ```
  `imagePullPolicy: IfNotPresent` is why the imported image is used.
- `mcp/Dockerfile` explicitly `COPY`s the Python modules (currently
  `agent_core.py agent_server.py agent_client.py agent_service.py audit.py
  k8s_tools.py llm.py n8n_tools.py`). Any new module must be added there or it
  will be missing inside the pod. The image also installs `git`, `ripgrep`,
  `bubblewrap` and **kubectl v1.35.5** (matches the server minor); `tests/` and
  `.venv/` are excluded via `.dockerignore`.
- Never apply `yamls/ollama-deployment.yaml.orig`; it creates a second Ollama in
  the `default` namespace that competes for the GPU.
- Do not change the Deployments' `strategy: Recreate` while PVCs are
  `ReadWriteOnce`; a rolling update would deadlock on the mounted volume.
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
    stdin, never from argv. Read per request, so a key rotation needs no
    restart. `yamls/auton-model.yaml` also defines ConfigMap
    `auton-model-config` (provider/base_url/model/max_tokens/data_policy).

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
  32 tools total: workspace (`list_directory`, `read_file`, `write_file`,
  `edit_file`, `apply_patch`), search (`grep`, `glob`), shell (`bash`, `job`,
  `run_tests`), git (`git_status`, `git_diff`), housekeeping (`fs`,
  `todo_write`), Kubernetes (`k8s_get`, `k8s_describe`, `k8s_logs`,
  `k8s_snapshot`, `k8s_diagnose`, `k8s_events`, `k8s_wait`, `k8s_apply`,
  `k8s_patch`, `k8s_scale`, `k8s_rollout`, `k8s_delete`, `k8s_exec`) and n8n
  (`n8n_workflows`, `n8n_activate`, `n8n_executions`, `n8n_webhook`,
  `n8n_create_agent_pipeline`). `run_command` is a deprecated alias for `bash`.
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
  long-lived HTTP request.
- File/search tools are jailed to `AGENT_WORKSPACE` (default `./workspace`;
  container uses `/workspace`). `bash`/`run_tests`/`git` are deliberately not
  jailed and run a real shell; `AGENT_SANDBOX=bwrap` optionally wraps them.
- The pod is hardened: `runAsNonRoot` UID/GID 10001, read-only root filesystem,
  `drop: ["ALL"]`, `/tmp` as an emptyDir (`HOME`/`TMPDIR=/tmp`,
  `PYTHONDONTWRITEBYTECODE=1`), and a root init container that `chown`s the
  workspace. `local-path` does not reliably apply `fsGroup`, so removing that
  init container leaves `/workspace` unwritable for the agent.
- Auth: `/health` (and `/`) are open; `/run` and `/jobs*` require
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
  `134 passed`). `tests/test_rbac.py` parses `yamls/` and fails if a Role ever
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
  Simulate a broken log by pointing `AGENT_AUDIT_DIR` at a file — the mutation
  must be refused with `refused ... audit log is not writable` and kubectl must
  not run.
- n8n tools: `kubectl -n auton exec deploy/auton -- python -c 'import
  n8n_tools; print(n8n_tools.n8n_workflows("list"))'`.
- End-to-end: activate the generated webhook workflow and call
  `http://n8n:5678/webhook/auton-agent` with `{"task": "..."}`; a 200 with the
  agent's JSON result proves n8n → agent → Ollama.
- GPU: `kubectl get nodes -o jsonpath='{.items[0].status.capacity.amd\.com/gpu}'`
  should print `1`; `kubectl -n auton exec deploy/ollama -- ollama ps` should
  show `100% GPU` once a model is loaded.
