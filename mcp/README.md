# Auton — Autonomous Kubernetes Operations Platform

Auton is a self-healing Kubernetes operations platform that runs entirely on a
local k3d cluster. It has two halves that share one image and one queue:

- **The autonomous ops half** — detect → analyze → diagnose → plan → policy
  check → repair → verify → learn. Detection, diagnosis and repair are
  **deterministic** (the LLM is not in the repair path); the model is used for
  the coding-agent half and, optionally, as an *advisory* second opinion on a
  diagnosis.
- **The coding-agent half** — an MCP agent that drives a model in a
  tool-calling loop over a workspace, Kubernetes and n8n. Local Ollama is the
  default; OpenAI, Gemini, Claude and any OpenAI-compatible endpoint can be
  selected instead.

- **Ollama** serves `qwen2.5-coder:7b-instruct` on the AMD RX 6600
  (Vulkan backend, full GPU offload) — the default provider.
- **`llm.py`** is the provider layer; configuration is read per request, so a
  switch needs no restart, and a remote provider never falls back to local.
- **`agent_service.py`** is the HTTP front end (sync `/run` plus an async jobs
  API), with bearer-token auth.
- **`agent_core.py`** is the reusable agent loop, including context budgeting
  and cancellation.
- **`agent_server.py`** is the MCP stdio server that gives the model workspace,
  search, patch, shell, git, Kubernetes and n8n tools.
- **`agent_client.py`** is the CLI equivalent of the HTTP service.

For cluster creation, GPU setup, Ollama, n8n and ingress, see the
[root README](../README.md). This document covers only the agent.

## The pod shape

Five Auton pods, four ServiceAccounts, two queues:

```
                         ┌──────────────────────────────────────────┐
                         │  Redis (auton:6379, ClusterIP)           │
                         │  repair queue · agent queue · locks ·     │
                         │  cooldowns · budget · GPU slot           │
                         └───────▲──────────────▲───────────────▲─────┘
                                 │              │               │
  ┌──────────────┐      ┌────────┴───┐   ┌──────┴──────┐  ┌─────┴────────┐
  │ auton        │      │ auton-     │   │ auton-ops   │  │ auton-worker │
  │ HTTP + UI    │      │ scanner ×2 │   │ LLM         │  │ ×2           │
  │ SA auton-api │      │ SA scanner │   │ executor    │  │ SA worker    │
  │ producer     │      │ read-only  │   │ SA agent    │  │ the only     │
  │ only         │      │ detection  │   │ advisory    │  │ writers      │
  └──────────────┘      └────────────┘   └──────┬──────┘  └─────┬────────┘
                                                 │               │
                                          kube-apiserver (in-cluster)
```

| Pod | Role | ServiceAccount | May write |
|---|---|---|---|
| `auton` | HTTP API + bundled UI | `auton-api` | only `patch` on ConfigMap `auton-model-config` and Secret `auton-model` (by name) |
| `auton-scanner` ×2 | read-only detection | `auton-scanner` | nothing |
| `auton-worker` ×2 | the playbooks | `auton-worker` | pods (delete) + Deployments (patch/scale) in `auton-apps` only |
| `auton-ops` | the LLM executor | `auton-agent` | workloads in `auton` + `auton-apps` |
| `redis` | queue + locks + budget | *(none)* | — |

RBAC — not the Python code — is the security boundary. No identity can read
Secrets, delete a PVC, or touch roles/serviceaccounts/namespaces. Cluster scope
is read-only (`auton-cluster-reader`, bound to `auton-agent`).

The namespace, Service and Deployment are all named `auton`; the Service is
**ClusterIP**, so use `kubectl port-forward` (or the Traefik ingress) from the
host.

---

## Python files

| File | Purpose |
|---|---|
| `agent_server.py` | MCP server over stdio: registers the 34 tools below (plus a deprecated `run_command` alias) |
| `audit.py` | Write-ahead audit log: JSONL + SQLite, fail-closed, `begin`/`finish`/`record` |
| `k8s_api.py` | **The direct Kubernetes API client**: stdlib HTTPS transport, the credential lookup, the closed resource registry, the WebSocket exec channel, the transport counters |
| `k8s_ops.py` | The Kubernetes **operations** over that transport: get/list/logs/events/apply/patch/scale/delete/rollout/exec/describe |
| `k8s_tools.py` | The 13 Kubernetes tools: direct HTTPS first, kubectl as the logged fallback, diagnosis + audit-gated mutations |
| `llm.py` | Provider layer: `ollama`/`openai`/`gemini`/`anthropic`/`custom`, config read per request, redaction, no fallback |
| `provider_admin.py` | Validated provider writes behind `POST /provider`: ConfigMap/Secret patch (or files), audit without the key, `400` for a config the agent would reject |
| `n8n_tools.py` | n8n public-API + webhook client: 6 tools, stdlib HTTP only |
| `incidents.py` | **Incident Manager v2**: fingerprint, occurrence count, timeline, 13-state machine, recurrence linking, auto-resolve, optional Redis index |
| `evidence.py` | **Evidence Collector**: the structured §5 evidence document (resource/problem/container/resources/owner/evidence) — never reads a Secret |
| `diagnosis.py` | **Diagnosis Engine**: 18 deterministic root causes, 6-action allowlist, advisory-only LLM refinement |
| `policy.py` | **Policy Engine**: modes (disabled/observe-only/safe-auto/approval-required), risk tiers 0–3, rules, protected targets, fail-closed load |
| `planner.py` | **Repair Planner**: the `RepairPlan` schema — action, tier, playbook, expected effect, verification, rollback |
| `approvals.py` | **Approval Store**: atomic JSON requests that park a plan until a human decides (decide-once, expiry, prune) |
| `learning.py` | **Learn stage**: repair outcomes, per-workload profiles and the bounded, audit-gated auto-adaptation (step, tier, cooldown, budget, quarantine) |
| `playbooks.py` | Deterministic repairs: OOM memory bump, stuck pod, crashloop recreate, rollout undo, replica restore |
| `ops_controller.py` | The pipeline: snapshot → finding → evidence → diagnosis → incident state machine → playbook → verify → report |
| `ops_queue.py` | The queue backends: `memory` (default, local) and `redis` (the cluster manifests) — **no silent fallback**, **no silent unauthenticated fallback** (Phase 5) |
| `task_store.py` | Task lifecycle: lease (300 s), retry with the same incident, cooldown, hourly action budget, dead-letter handling |
| `ops_scanner.py` | The scanner loop (Deployment `auton-scanner`): watch → detect → submit → dedupe |
| `watcher.py` | **Kubernetes watcher**: real `resourceVersion` watch streams, backoff, 410 → re-list, event cache, health counters |
| `metrics.py` | **Prometheus exposition**: the text format, written by hand — registry, gauge/counter/histogram, the collectors. Standard library only |
| `ops_worker.py` | The worker loop (Deployment `auton-worker`): claim → repair → verify |
| `agent_worker.py` | The executor loop (Deployment `auton-ops`): run the agent for the `agent` queue |
| `agent_core.py` | Agent loop: dual-path tool calling, observation caps, compaction, corrective retry, cancellation |
| `agent_client.py` | CLI entry point (`python agent_client.py "task"`) |
| `agent_service.py` | HTTP front end: bearer auth, sync `POST /run`, async `/jobs`, SSE, cancel, `/audit`, `/incidents`, `/policy`, `/approvals`, `/learning`, `/metrics` |
| `Dockerfile` | Builds `auton:local` (`python:3.11-slim` + git, ripgrep, bubblewrap, kubectl) |
| `ui/index.html` | Single-page UI served at `/`: chat, incidents, approvals, learning, audit, provider; no bundler or CDN |
| `requirements.txt` | `mcp>=2.0,<3`, `ollama>=0.4.0`, `pathspec>=0.12` |
| `tests/` | pytest suite (tools, loop helpers, MCP transport, HTTP service, auth, k8s/n8n, incident pipeline) |
| `eval/run_eval.py` | 8 coding tasks + 4 optional live tasks; needs a live Ollama |

> `mcp.server.mcpserver.MCPServer` is the **MCP v2** API. Do not downgrade
> `requirements.txt` to MCP v1. New modules must be added to the `COPY` line in
> the Dockerfile, or they will not exist inside the pod, and new tools must be
> added to `EXPECTED` in `tests/test_server_stdio.py`.

---

## The toolset

Successful tools return plain text; failures return compact JSON
(`{"ok": false, "error": "...", "hint": "..."}`). Hints are deliberate: a 7B
model recovers from "old_string occurs 2 times, add context" but not from an
opaque error.

### Workspace

| Tool | Signature | Behaviour |
|---|---|---|
| `list_directory` | `(path=".", depth=1, max_entries=200)` | Recursive to `depth`, sorted, skips ignored paths |
| `read_file` | `(path, start_line=1, end_line=0, max_chars=20000)` | Numbered lines, explicit truncation + resume hint, refuses binaries |
| `write_file` | `(path, content)` | Atomic write; reports bytes/lines/sha256; no-op if identical |
| `edit_file` | `(path, old_string, new_string, replace_all=False)` | Requires a **unique** match by default; returns a unified diff |
| `apply_patch` | `(path, patch)` | Multiple SEARCH/REPLACE blocks, applied atomically; returns a diff |

`read_file` output looks like:

```
notes.md — lines 10-12 of 240
    10| ## Setup
    11| Run `make test` first.
    12| Then `make run`.
[next: start_line=13, end_line=240]
```

`edit_file` replaces exactly one occurrence unless `replace_all=true`. If the
anchor is missing it reports where the first line does appear.

### `apply_patch` (preferred for edits)

One call can make several edits to one file. Blocks are applied in order and
**nothing is written** if any block fails:

```
<<<<<<< SEARCH
def add(a, b):
    return a - b
=======
def add(a, b):
    return a + b
>>>>>>> REPLACE
```

- Exact match first, then retries that ignore trailing whitespace, then quote
  style (`'` vs `"`); the result says which fallback fired.
- A block that matches twice is an error; add surrounding lines.
- A real miss returns the closest line in the file, verbatim, so the model can
  copy it instead of retrying the same near-miss.
- The returned unified diff is proof of the change — no need to re-read the file.

### Search

| Tool | Signature | Behaviour |
|---|---|---|
| `grep` | `(pattern, path=".", include="", exclude="", regex=False, case_sensitive=True, context=0, max_results=50)` | `path:line: text`; ripgrep when available, gitignore-aware either way |
| `glob` | `(pattern, path=".")` | `**/*.py`-style file discovery, gitignore-aware |

Use `glob` to find files and `grep` to find content — both are much cheaper than
walking directories by trial and error.

### Shell

| Tool | Signature | Behaviour |
|---|---|---|
| `bash` | `(command, cwd=".", timeout=120, max_output_chars=8000, background=False)` | `/bin/sh -c`, cwd jailed, head+tail truncation, real exit code |
| `job` | `(job_id="", action="output", tail_chars=8000)` | `output` / `kill` / `list` for background jobs |
| `run_tests` | `(command="", timeout=300)` | Auto-detects pytest / npm / cargo / go / make; returns PASS/FAIL + tail |

`bash(background=true)` returns a `job_id`; logs are written under
`.agent/jobs/{job_id}.log` on the workspace PVC, so they survive the pod.

`run_tests` is the intended verification step: it keeps the last 80 lines (where
pytest puts the failure) and caps output so a noisy suite cannot eat the context.

### Git and housekeeping

| Tool | Signature | Behaviour |
|---|---|---|
| `git_status` | `()` | `git status --porcelain -b` |
| `git_diff` | `(path="", staged=False)` | Truncated diff — the densest "what changed" summary |
| `fs` | `(path, op="stat", dest="", recursive=False)` | `stat` / `mkdir` / `move` / `delete`; protects the workspace root and `.git` |
| `todo_write` | `(todos: list[dict] = None)` | Persists a checklist to `.agent/todos.json`; `[]` or no argument reads it back; accepts plain strings too |

`git` and `ripgrep` are installed in the image. `fs` refuses to delete the
workspace root or `.git`, and a non-empty directory needs `recursive=true`.

### Kubernetes (`k8s_tools.py`)

The pod runs as ServiceAccount `auton-agent`. RBAC (see
`yamls/auton-rbac.yaml`) is the real security boundary; this module adds
defence in depth:

- the target namespace must be in the **allowlist** (`AGENT_NAMESPACES`,
  deployment sets `auton,auton-apps`); `AGENT_NAMESPACE` is the default. A
  confused model cannot try `-n kube-system`;
- `resource` / `name` / `selector` are regex-validated, so model output cannot
  smuggle extra kubectl flags;
- `AGENT_K8S_WRITE=0` turns every mutating tool into a refusal even if RBAC
  would allow it;
- every mutation calls `audit.begin()` **before** the request and is refused if
  the record cannot be written (`audit.py`); `audit.finish()` records the
  outcome. The audit gate is **transport-independent**: the record is written
  first, and only then is the direct path (or kubectl) asked to act;
- PVC delete is refused in code *and* RBAC, with a hint telling the model to
  report and ask a human;
- output is clipped (`AGENT_MAX_OUTPUT_CHARS`).

**How a request is made.** The tools talk to the API server **directly over
HTTPS** (`k8s_api.py` / `k8s_ops.py`). kubectl stays in the image as a
**fallback**: when the direct path is not available or fails, the tool falls
back to kubectl and says so — `[k8s] using kubectl for …` in the log and a
counter in `k8s_api.COUNTERS` (visible in `/tasks/stats` and in
`auton_k8s_transport_total`). The fallback is never silent. A *refusal*
(404/403/409) is a final answer and is never retried with kubectl.

`k8s_snapshot` and `k8s_diagnose` do evidence collection in code (status,
owner, warning events, current + previous logs) and append a deterministic
`diagnosis:` / `suggested_action:` pair. This is what keeps a 7B model from
having to parse hundreds of lines of raw JSON before it can decide anything.

| Tool | Signature | Behaviour |
|---|---|---|
| `k8s_get` | `(resource, name="", selector="", output="", namespace="")` | List/get; `output` is `wide`, `yaml`, `json` or `name` |
| `k8s_describe` | `(resource, name="", namespace="")` | Describe (includes recent events for pods/workloads) |
| `k8s_logs` | `(pod, container="", tail=100, previous=False, namespace="")` | Log tail, capped at 2000 lines |
| `k8s_snapshot` | `(namespace="")` | Pods, deployments, statefulsets, PVCs, warning events + `PROBLEMS:` |
| `k8s_diagnose` | `(pod, namespace="", container="")` | Evidence pack + deterministic cause/playbook suggestion |
| `k8s_events` | `(since="15m", warnings_only=True, namespace="")` | Events filtered by age (`90s`/`15m`/`2h`/`1d`) |
| `k8s_wait` | `(resource, name, condition="ready", timeout=60, namespace="")` | `ready`/`available`/`complete`/`delete`, max 300 s |
| `k8s_apply` | `(manifest, namespace="")` | Apply YAML/JSON on stdin; omit `metadata.namespace` |
| `k8s_patch` | `(resource, name, patch, patch_type="strategic", namespace="")` | `strategic` / `merge` / `json` patch |
| `k8s_scale` | `(deployment, replicas, namespace="")` | Scale 0–10 |
| `k8s_rollout` | `(action, deployment="", namespace="")` | `status` (read-only), `restart`, `undo` |
| `k8s_delete` | `(resource, name, confirm=False, namespace="")` | Refuses unless `confirm=true`; never deletes a whole resource type; PVCs always refused |
| `k8s_exec` | `(pod, command, container="", timeout=60, namespace="")` | `sh -c` in the pod (audited, max 120 s) |

`_kubeconfig_args()` writes an explicit in-cluster kubeconfig to
`/tmp/auton-kubeconfig` (CA cert + `tokenFile`). This is **required**:
kubectl silently drops the implicit in-cluster config when `--request-timeout`
is non-zero and falls back to `http://localhost:8080` — which inside the agent
pod is the agent's own authenticated API, producing a baffling 401. Do not
remove the workaround. It is only used on the **fallback** path now.

### n8n (`n8n_tools.py`)

n8n has no environment variable for public-API keys. The key is created once in
the UI (or via n8n's REST API) and stored in Secret `n8n-api`; the module reads
it from `N8N_API_KEY_FILE` on **every call**, so rotating the Secret needs no
restart. The catch is that `/etc/n8n-api` is a *projected* Secret volume, which
kubelet refreshes asynchronously (typically <1 min, with no immediate signal), so
a rotation is only complete once the file inside the pod changes. Use
`../scripts/rotate-n8n-api-key.sh`: it applies the Secret, polls the mounted
file, restarts the Deployment only if the projection stalls, and finally calls
`n8n_workflows("list")` inside the pod to prove n8n accepts the new key.

| Tool | Signature | Behaviour |
|---|---|---|
| `n8n_workflows` | `(action="list", workflow_id="", payload="")` | `list`, `get`, `create`, `update`, `delete`; lists are summarised (no node JSON) |
| `n8n_activate` | `(workflow_id, active=True)` | Activate/deactivate |
| `n8n_executions` | `(workflow_id="", limit=20)` | Recent runs, newest first, status only |
| `n8n_webhook` | `(path, payload="")` | POST JSON to `/webhook/<path>` and return the response (900 s timeout) |
| `n8n_create_agent_pipeline` | `(name="Auton agent", path="auton-agent", activate=True)` | Create the webhook → async job → poll → respond template and activate it |
| `n8n_create_ops_pipeline` | `(kind="both", name="Auton ops reports", path="auton-ops", activate=True)` | Create the incident → Slack/Telegram workflow (`kind=slack\|telegram\|both`) |

`n8n_create_agent_pipeline` exists because asking a 7B model to hand-write n8n
node JSON is a losing game: the graph is fixed and generated, only the name and
webhook path vary. The graph is:

```
Webhook → Start Job (POST /run?async=1) → Wait 5s → Get Job (GET /jobs/{id})
        → Done? (status ∈ done|error|cancelled) ─yes→ Respond
                                  └──────────────no──→ Wait (loop)
```

The false branch loops back to the Wait node, so a multi-minute agent run never
depends on one long-lived HTTP request; `Respond` returns `$json.result` when the
job finished and the raw job payload (status/error) otherwise. The workflow's
`executionTimeout` is 3600 s. Both HTTP nodes send
`Authorization: Bearer $env.AGENT_API_TOKEN`, and the base URL for the polls is
`AGENT_BASE_URL` (derived from `AGENT_RUN_URL` by stripping `/run`).

`n8n_create_ops_pipeline` generates the reporting graph: a webhook that accepts
the ops notification payload and fans out to generic HTTP Request nodes.

```
Webhook (respond 200 immediately)
   ├─→ Slack    POST $env.SLACK_OPS_WEBHOOK
   └─→ Telegram POST api.telegram.org/bot$env.TELEGRAM_BOT_TOKEN/sendMessage
```

No n8n credential is needed: the Slack incoming-webhook URL and the Telegram
bot token/chat id come from n8n environment variables (Secret `n8n-notify` in
`yamls/n8n-deployment.yaml`). `kind="slack"` or `"telegram"` creates only that
branch. The message is built from the payload fields (`severity`, `status`,
`resource`, `namespace`, `summary`, `action`, `verification`, `url`).

### Ignoring files

`list_directory`, `grep` and `glob` skip `.git`, `.venv`, `venv`,
`__pycache__`, `node_modules`, `.agent` and the usual cache directories, plus
anything matched by the workspace's `.gitignore` (via `pathspec`).

---

## The agent loop (`agent_core.py`)

1. Spawns `agent_server.py` as an MCP subprocess over stdio and lists its tools.
2. Converts the MCP schemas to Ollama function schemas and injects a system
   prompt with the tool signatures and routing rules (workspace vs. Kubernetes
   vs. n8n).
3. Calls `llm.chat(messages, tools)` in a loop up to `MAX_STEPS`; `llm.py`
   selects Ollama (native `tool_calls`) or a remote provider (tool calls
   serialised as JSON text, see [Model providers](#model-providers-llmpy)).
4. Dispatches each tool call back into the MCP session; every call and its
   output is recorded in `steps`.
5. Stops on a plain-text final answer, the step budget, cancellation, or an
   unrecoverable parse failure.

**Dual-path tool calling.** Small local models frequently emit a tool call as
JSON *text* instead of Ollama's structured `tool_calls`. The loop accepts both:

- **(a) native** — `resp.message.tool_calls` from Ollama.
- **(b) fallback** — a `{"name": ..., "arguments": {...}}` object parsed out of
  the message content (`extract_tool_call` also looks inside fenced code blocks
  and at every balanced `{...}` substring). The tool name must be in the valid
  set, so stray JSON in prose is ignored.

**Context discipline** (this matters more than tool count with a 7B model):

| Mechanism | Env var | Effect |
|---|---|---|
| Observation cap | `AGENT_MAX_OBSERVATION_CHARS` (8000) | Head+tail clip on every tool result |
| History compaction | `AGENT_MAX_HISTORY_CHARS` (60000), `AGENT_KEEP_OBSERVATIONS` (3) | Older results collapse to `[elided N chars of bash output]` |
| Corrective retry | `AGENT_PARSE_RETRIES` (1) | One "reply with only JSON" nudge for a malformed call |
| Context window | `AGENT_NUM_CTX` (16384) | Passed to Ollama so tool schemas + history are not silently truncated |
| Repeat guard | `AGENT_REPEAT_LIMIT` (2), `AGENT_REPEAT_PENALTY` (1.1) | Identical calls (or identical output under a new signature) are nudged, then stop the run |
| Cancellation | `/jobs/{id}/cancel` | `should_stop()` is checked before every step |

**Loop guards.** Small models fail in predictable ways, and the loop defends
against each one rather than burning the step budget:

| Symptom | Guard |
|---|---|
| Re-runs the same successful call | 2nd identical call gets a "you already ran this" observation, 3rd stops with `repeat` |
| Varies an argument that has no effect (`depth=13,14,15…` all clamp) | Consecutive calls to the same tool returning **identical output** are nudged, then stop with `repeat` |
| Repeats the same *failing* call (even with a read in between) | Failures are counted per `(tool, args)`; the 3rd stops with `repeat` |
| Rewrites the tool output as its answer ("Observation from ...") | One nudge to continue; then `parse_failed` |
| Says "Next I will run the tests" and stops | One nudge to make the call in the same turn |
| Emits the call wrapped in prose or a fence | Extraction scans fenced blocks and every balanced `{...}` |
| Sees a red test and rewrites the assertion | **Test-file guard**: `edit_file`/`apply_patch`/`write_file` refuse to remove or change an `assert` line in `test_*.py` / `*_test.py` / `conftest.py` (JSON error + HARD RULE 1 hint, nothing written). Creating test files and *adding* assertions is allowed. `AGENT_ALLOW_TEST_EDITS=1` turns the guard off. |

**Argument coercion.** `coerce_args()` uses the tool schema to repair the shapes
models actually emit: arguments as a JSON string, `[]` for a no-argument tool,
or a bare list/value for a single-parameter tool
(`{"name": "todo_write", "arguments": [...]}` → `{"todos": [...]}`).

The returned dict is `{final, steps, stop_reason, elapsed}`, where `stop_reason`
is `final`, `max_steps`, `parse_failed`, `repeat`, `cancelled` or `error`.

---

## Model providers (`llm.py`)

The loop does not call Ollama directly; it calls `llm.chat()`. Configuration is
read **per request** (so a switch needs no restart):

```
$AGENT_MODEL_DIR/config/provider      ollama | openai | gemini | anthropic | custom
$AGENT_MODEL_DIR/config/base_url      API base
$AGENT_MODEL_DIR/config/model         model name/tag
$AGENT_MODEL_DIR/config/max_tokens    response cap (remote providers)
$AGENT_MODEL_DIR/config/data_policy   redact | allow | local-only
$AGENT_MODEL_DIR/secret/api-key       key (Secret mount, never logged)
```

In the cluster the directory is `/etc/auton-model` (ConfigMap
`auton-model-config` + Secret `auton-model`). Environment variables
(`AGENT_PROVIDER`, `AGENT_LLM_BASE_URL`, `AGENT_LLM_MODEL`,
`AGENT_LLM_API_KEY`, `AGENT_DATA_POLICY`) are the local-development fallback;
file values win.

There are two write paths, and they produce the same result:

- `scripts/set-provider.sh` (host, kubeconfig) — waits for the projected volume
  and runs the round-trip.
- `POST /provider` / `DELETE /provider/key` (UI or API) — `provider_admin.py`
  validates, audits (`provider_update`, key never recorded), then patches the
  named ConfigMap/Secret. `AGENT_PROVIDER_STORE=files` writes the files directly
  for local development. The API identity may `patch` only
  `auton-model-config` and `auton-model` (`resourceNames` in the `auton-api`
  Role); it cannot read secrets or touch workloads.

| Provider | Transport | Notes |
|---|---|---|
| `ollama` | `ollama.AsyncClient` | Default; native `tool_calls` are preserved |
| `openai` | `POST {base}/chat/completions` | `tools` as function definitions |
| `gemini` | same OpenAI-compatible endpoint | `base_url` defaults to Google's `/v1beta/openai` |
| `anthropic` (`claude`) | `POST {base}/v1/messages` | system prompt separate, `tool_use` blocks |
| `custom` | OpenAI-compatible | `base_url` required (vLLM, LM Studio, llama.cpp) |

Two invariants, both covered by `tests/test_llm.py`:

1. **No fallback.** `provider != ollama` never touches the local model. A
   missing key (`LLMError: no API key`) or an unreachable endpoint fails the
   run; it does not degrade to Ollama.
2. **Remote data is filtered.** `data_policy=redact` (default) masks
   passwords/tokens/API keys, `Authorization` headers, AWS/GitHub tokens,
   private-key blocks and URL credentials before any request body is built.
   `local-only` refuses remote providers outright (`LLMError`).

Remote tool calls are converted to the JSON text the loop already parses
(`{"name": ..., "arguments": {...}}`), so the assistant/tool history stays in
one format and no provider-specific ids leak into it. Tool schemas are sent as
function definitions (Anthropic: `input_schema`).

Switch and verify with `scripts/set-provider.sh` (writes ConfigMap/Secret,
waits for the projected files, prints `provider_info()`, optional `--test`):

```bash
scripts/set-provider.sh --provider openai --model gpt-4o-mini \
  --api-key-file /tmp/opencode/openai.key --test
scripts/set-provider.sh --provider ollama --clear-key --test
```

`GET /provider` returns `{provider, model, base_url, data_policy,
local_model_enabled, api_key_set, api_key_hint}` — never the raw key.
`POST /provider/test` sends one tiny completion (`502` on failure).

---

## Autonomous ops (`ops_controller.py` + `incidents.py` + `evidence.py` + `diagnosis.py`)

The operations half is split across three pods so a bug in one stage cannot
widen the other's permissions:

```
auton-scanner (×2)          auton-worker (×2)           auton-ops
  snapshot auton-apps          claim from the             LLM executor:
  → rule-based findings        `repair` queue             advisory diagnosis
  → dedupe                     → evidence collection       + agent queue
  → submit a task              → diagnosis                 (never in the
                                → repair plan                 repair path)
                                → policy check  ─┬─ auto → playbook
                                               ├─ observe → SUPPRESSED
                                               ├─ deny → NEEDS_HUMAN
                                               └─ approval → AWAITING_APPROVAL
                                → verify (audit-gated)
                                → incident + report
```

The repair path is **LLM-free**. The same `k8s_tools._collect` snapshot feeds a
rule table and a matching deterministic playbook runs. A 7B model explaining an
OOMKill is useful; a 7B model choosing a kubectl patch is not. The model may
only *refine* a diagnosis (`AGENT_OPS_LLM_DIAGNOSIS=1`), and even then it may
not invent a root cause, lower the confidence, or choose an action outside the
six-item allowlist.

### The Kubernetes watcher (`watcher.py`)

Phase 3 replaced the scanner's blind polling with a real Kubernetes **watch**.
Before, every scanner asked the API server the same question every 60 seconds
— "what does this namespace look like right now?" — and parsed a full snapshot
to answer it. A pod that was OOMKilled at second 3 was noticed at second 60.

Now the API server pushes the changes:

```
┌──────────┐   list()                    ┌──────────────────┐
│ watcher  │ ──────────────────────────▶ │ api-server       │
│  thread  │   watch(rv=12345)            │                  │
│          │ ◀────────────────────────── │ ADDED/MODIFIED/  │
└──────────┘   event stream              │ DELETED/BOOKMARK │
             ▲                           └──────────────────┘
             │ reconnect with backoff
             └── 410 Gone ──▶ re-list ──▶ new resourceVersion
```

Three rules shape it:

**A real resourceVersion.** The watcher never re-lists to "catch up". It keeps
the last `resourceVersion` it saw and resumes the stream from exactly there.
The API server may close a watch at any time; that is normal, not an error.

**Failures are counted, not hidden.** Every reconnect, expired
`resourceVersion` and error is a counter, and `Watcher.stats()` exposes them.
A watcher that silently stops watching is the worst possible outcome for a
self-healing platform, so "is it still connected?" must be answerable from the
outside — it is, at `GET /tasks/stats` under `watchers`.

**Degrade, never crash.** The watcher is an *optimisation*: it makes detection
faster and cheaper. If the API server is unreachable, if the ServiceAccount
may not watch, or if `AGENT_WATCH_ENABLED=0`, the scanner falls back to the
polling loop exactly as before. Detection never stops because a watch stream
did.

The scanner runs two tiers side by side:

| Tier | Interval | Source | Rules |
|---|---|---|---|
| fast | `AGENT_WATCH_FAST_INTERVAL` (60 s) **and immediately on a bad event** | the watch cache — no API call | pod-level (`pod_findings`) |
| full | `AGENT_WATCH_FULL_INTERVAL` (300 s) | a real snapshot (`k8s_tools._collect`) | every rule, workload-level included |

A pod event whose reasons map to a known incident cause (`ops_controller.REASONS`)
wakes the scanner up right away; a Deployment event wakes the full
reconciliation, because the workload-level rules may apply. Both triggers are
debounced by `AGENT_WATCH_MIN_GAP` (5 s).

**Five streams per namespace (D6).** The watcher covers `pods`,
`deployments`, `statefulsets`, `persistentvolumeclaims` and `events` — the
resources the full-tier snapshot needs, so the watch cache and the snapshot
agree. Events are high churn, so they get a **TTL**
(`AGENT_WATCH_EVENT_TTL`, 900 s): a stale event is worse than no event, and the
cache is pruned so it cannot grow without bound. A pod event can trigger the
fast tier; the other four trigger the full tier.

Only the standard library is used: `urllib` for HTTPS, `ssl` for the cluster
CA, `json` for the event stream. There is no client-go and no new dependency in
the image. The HTTP plumbing itself lives in `k8s_api.py` (shared with the
tools); `watcher.py` re-exports the client, so its public surface did not
change.

### The executor write scope (`watcher.py`'s sibling: RBAC + `AGENT_K8S_WRITE`)

Phase 4 narrowed the LLM executor. The component that talks to a 7B model may
**read** every namespace it is allowed to see, but by default it may not
**change** anything: a hallucinated `kubectl patch` must not become a real
change.

The scope is enforced by two independent layers:

| Layer | Where | What it does |
|---|---|---|
| **RBAC** (the boundary) | `yamls/auton-rbac.yaml` | the `auton-agent` Role grants `get/list/watch` **only** — no write verb, no `pods/exec` |
| **the tools** (the belt) | `AGENT_K8S_WRITE=0` | `k8s_apply`, `k8s_patch`, `k8s_scale`, `k8s_rollout`, `k8s_delete` and `k8s_exec` refuse before they run |

The important consequence: **the shell cannot bypass it.** The executor pod
has `kubectl` and its ServiceAccount token, but without a write verb the API
server answers `Forbidden`:

```
$ kubectl -n auton exec deploy/auton-ops -- \
    kubectl -n auton-apps patch deployment api --type merge -p '{...}'
Error from server (Forbidden): deployments.apps "api" is forbidden:
User "system:serviceaccount:auton:auton-agent" cannot patch resource
"deployments" in API group "apps" in the namespace "auton-apps"
```

RBAC is the security boundary; the tool code is a fast, friendly refusal on
top of it.

The write half lives in a **separate, opt-in** manifest
(`yamls/auton-rbac-write.yaml`: Role `auton-agent-write` + bindings in both
namespaces) and is enabled with one command, which changes both halves at
once — either half alone would leave a confusing state:

```bash
scripts/set-agent-writes.sh --status     # report the current scope
scripts/set-agent-writes.sh --enable     # apply the write Role + AGENT_K8S_WRITE=1
scripts/set-agent-writes.sh --disable    # delete the write Role + AGENT_K8S_WRITE=0
```

The Helm equivalent is `agent.allowWrites=true` (default `false`), and
`scripts/install.sh --allow-writes` does the same during an install.

The **repair path is not affected**: the scanner and the worker keep their own
identities and their own policy-gated permissions. `auton-worker` remains the
only component that mutates a workload by default.

### Queue authentication (`ops_queue.py`)

The queue is the one component every pod talks to, so it is the one component
that must never accept an unauthenticated client. Since Phase 5 **Redis
requires a password**, and the client **fails closed**.

**Where the password comes from** — `ops_queue.redis_password()`:

1. `AGENT_REDIS_PASSWORD_FILE` (the manifests: `/etc/auton-redis/password`) —
   a mounted Secret. The file wins over the environment variable, because a
   value in the environment shows up in every process dump.
2. `AGENT_REDIS_PASSWORD` — for local runs only.

An **unreadable** or an **empty** file is an error, never an empty password: a
client that silently connects without auth looks healthy against a
passwordless server and is refused by a protected one.

**How the client fails closed** — `create_backend()` calls `backend.ping()`,
and every failure mode raises `QueueError` (the pod refuses to start):

| Situation | Message |
|---|---|
| no password, protected server | `Redis authentication failed: Authentication required.` |
| wrong password | `Redis authentication failed: invalid username-password pair or user is disabled.` |
| password, passwordless server | `Redis authentication failed: AUTH … called without any password configured…` |
| the server is down | `Redis is unreachable: …` |

The first three are *authentication* failures and the last one is an outage —
the distinction matters, because an operator must not go looking at the
network when the problem is the password. `ops_queue._is_auth_error()` does
that classification (redis-py maps some of these replies to
`AuthenticationError` and leaves others as a plain `ResponseError`, so the
message is inspected too).

There is **no** "retry without a password" path and **no** fallback to the
in-memory backend: both would look healthy and silently lose repair tasks.

**Where the password lives on the server** — the Redis pod's init container
reads the mounted Secret and writes a config file:

```yaml
command: ["redis-server", "/tmp/redis/redis.conf"]     # no password in argv
```

The password must be ≥ 16 characters from `[A-Za-z0-9_-]`; the init container
refuses to start otherwise, instead of writing a config Redis cannot parse.
The probes authenticate (`REDISCLI_AUTH=… redis-cli ping | grep -qx PONG`) —
an unauthenticated PING answers `NOAUTH …` and redis-cli still exits 0, so the
probe checks the *answer*, not only the exit code.

`GET /health` reports `queue_auth: password` (or `none` / `unreadable`) — the
state, never the value.

### The incident pipeline

Every incident walks an explicit state machine. The route is the legal path
through the adjacency table; an illegal transition raises and writes nothing.

```
DETECTED → TRIAGING → DIAGNOSING → PLANNING → POLICY_CHECK → QUEUED
        → EXECUTING → VERIFYING → RESOLVED
                     ↘ AWAITING_APPROVAL → (approved) → QUEUED → …
                     ↘ SUPPRESSED (observe-only / cooldown / budget)
          ↘ FAILED → NEEDS_HUMAN
```

`incidents.py` stores one atomic JSON file per incident and records:

| Field | Meaning |
|---|---|
| `fingerprint` | sha1 of `cluster\|kind\|namespace\|name\|cause` — the stable identity of *this problem on this resource* (16 hex) |
| `occurrence_count`, `first_seen`, `last_seen` | how often and when the problem was seen |
| `previous_incident_id`, `recurrence_ids` | a recurrence opens a *new* incident linked to the old one; a repair retry reuses the *same* incident |
| `resource_uid`, `cluster_id`, `type` | which object, on which cluster, with which classified root cause |
| `timeline[]` | every transition: `from` → `to`, `by`, `note`, ISO timestamp |
| `diagnosis` | root cause, confidence, severity, evidence, recommended action, `requires_human` |
| `evidence_structured` | the full evidence document (see below) |
| `repair_plan`, `policy` | the plan that was proposed and the decision the policy engine made about it |
| `resolution` | what was done, and how it was verified |
| `severity_changes[]` | the severity history |

A closed incident may only be re-opened through `reopen()` — a silent status
write back to `detected` is refused.

### The evidence document (`evidence.py`)

`evidence.collect(namespace, kind, name)` returns the structured document the
platform spec asks for:

```json
{
  "resource":  {"kind": "Pod", "name": "api-1", "namespace": "auton-apps", "uid": "…"},
  "problem":   "OOMKilled",
  "container": {"name": "api", "restart_count": 4, "last_termination_reason": "OOMKilled",
                "exit_code": 137, "state": "waiting", "ready": false,
                "resources": {"memory_limit": "512Mi", …},
                "probes": {"liveness": {"configured": true, "path": "/healthz"}},
                "image_pull_secret_configured": false},
  "resources": {"memory_request": "256Mi", "memory_limit": "512Mi", …},
  "owner":     {"kind": "Deployment", "name": "api", "chain": [...]},
  "evidence":  {"pod_available": true, "logs_available": true,
                "events_available": true, "node_available": false,
                "node_metrics_available": false, "owner_available": true},
  "events": [...], "logs": "…", "previous_logs": "…",
  "node": {...}, "node_metrics": {...}, "deployment": {...},
  "errors": []
}
```

Two invariants, both covered by `tests/test_evidence.py`:

1. **Secrets are never read.** The collector only reports *whether* an image-pull
   secret is configured — never its name, never its value — and no env var value
   or credential-like string reaches the document.
2. **Unavailable evidence is marked, never invented.** A node the worker may not
   read (the worker's Role is namespace-scoped, so `node_available` is `false`)
   is reported as unavailable instead of being guessed.

### The diagnosis engine (`diagnosis.py`)

Eighteen root causes, classified deterministically in priority order: pod
status → events → container termination → node metrics/pressure → logs →
ownership → remaining rules. A generic `Error` exit is only an *observation*:
the reason why the container died is in the logs, the events or the probes, so
the engine keeps looking instead of concluding "application crash" early.

The result is `{root_cause, confidence, severity, evidence[], recommended_action,
requires_human, source}`. `recommended_action` is always one of the six
allowlisted actions (`INCREASE_MEMORY_LIMIT`, `RESTART_POD`,
`ROLLBACK_DEPLOYMENT`, `RESTORE_REPLICAS`, `REPORT_ONLY`, `NONE`).

`validate_llm_analysis()` accepts a model proposal only when it agrees with the
evidence: the cause must be known, must equal the deterministic cause (or the
engine must have been unable to decide), the confidence may not be lowered, and
an unknown action is replaced by the deterministic one. A model answer that
violates any of those is discarded, not partially applied.

### The policy engine (`policy.py`)

Deterministic is not the same as *allowed*. The policy engine is the gate
between a **plan** and an **action**: nothing reaches a playbook without a
decision from it.

The policy document lives in ConfigMap `auton-policy` (key `policy.json`) and
is mounted into the pods as a **projected volume**, so no ServiceAccount needs
permission to read it. It is read **per decision**, so an operator can tighten
it without a restart. The flat key `mode` is the quick switch: one
`kubectl patch` flips the whole platform.

```json
{
  "mode": "safe-auto",
  "default_tier": 1,
  "auto_approve_tiers": [0, 1, 2],
  "approval_tiers": [3],
  "deny_tiers": [],
  "protected_namespaces": ["kube-system", "kube-public", "kube-node-lease"],
  "protected_labels": [{"key": "auton.io/protect", "value": "true"}],
  "namespace_tiers": {},
  "rules": [],
  "approval_timeout_seconds": 3600,
  "max_plan_age_seconds": 900
}
```

#### The modes

| Mode | Behaviour |
|---|---|
| `disabled` | **The off switch.** Every decision is a denial. Nothing runs. |
| `observe-only` | Shadow mode: plans are computed and recorded, never executed. The incident closes as `SUPPRESSED`. |
| `safe-auto` | The tiers in `auto_approve_tiers` run unattended; the tiers in `approval_tiers` wait for a human; the tiers in `deny_tiers` are refused. |
| `approval-required` | Every mutating tier waits for a human. Tier 0 runs, because there is nothing to approve. |

The shipped default is `safe-auto` with `auto_approve_tiers: [0, 1, 2]` and
`approval_tiers: [3]` — exactly today's behaviour, with the irreversible tier
gated.

#### The tiers

The tier describes the **risk of the action**, not the severity of the symptom:

| Tier | Meaning | Examples |
|---|---|---|
| 0 | No mutation at all | observe, report, escalate |
| 1 | Transient and recreated by a controller | restart a pod, undo a rollout, scale replicas back up |
| 2 | Changes the workload's specification | raise a memory limit |
| 3 | Data-affecting or irreversible | never automatic |

#### The rules

`rules` is an ordered list; the **first match wins**. A rule may set a tier
and/or an action:

```json
{"match": {"cause": "oom"}, "tier": 3}
{"match": {"namespace": "auton"}, "action": "deny",
 "reason": "the platform never repairs itself"}
```

`match` accepts `cause`, `namespace`, `kind`, `resource` (a prefix match on
`kind/name`), `action` and `tier`. A rule may not unlock a tier the mode
denies outright.

#### Fail closed

An unreadable or invalid policy document is treated as `disabled`: the decision
is a denial that names the problem, and `GET /policy` reports the error. A typo
in the ConfigMap must never silently *widen* what the platform may do. A
document that is valid JSON but not an object (e.g. `[]`) is a **broken
document, not an absent one** — it denies, it does not fall back to the
default.

#### Changing it

```bash
scripts/set-policy.sh --show
scripts/set-policy.sh --mode observe-only
scripts/set-policy.sh --mode safe-auto --auto-tiers 0,1,2 --approval-tiers 3
scripts/set-policy.sh --deny-cause crashloop
scripts/set-policy.sh --require-approval-cause oom
scripts/set-policy.sh --reset
```

The script validates the document before it writes it, and it never resets a
policy that already exists.

### The repair planner (`planner.py`)

The planner turns a diagnosis into a **proposal** — enough detail that a human
can approve it and an operator can audit it afterwards:

```json
{
  "plan_id": "plan-1f0c8b2d4e5a",
  "incident_id": "inc-e96239a771",
  "namespace": "auton-apps", "kind": "Pod", "name": "api-1",
  "resource": "pod/api-1", "cause": "oom", "root_cause": "OOMKilled",
  "action": "INCREASE_MEMORY_LIMIT", "playbook": "oom_bump_memory",
  "tier": 2,
  "params": {"container": "hog"},
  "expected_effect": "the deployment's memory limit … is raised by 25% …",
  "verification": "deployment/api ready=1/1 available=1",
  "reversible": true,
  "rollback": {"action": "RESTORE_MEMORY_LIMIT",
               "params": {"memory_limit": "64Mi"}},
  "requires_human": false,
  "labels": {"app": "api"},
  "created_at": 1794602994.059
}
```

The plan is **not** an instruction to the cluster. It names a *registered*
playbook and a fixed set of parameters — there is no free-form command
anywhere in this path. The `plan_id` is stable for the same problem, so the
same defect always produces the same plan.

### The approval workflow (`approvals.py`)

When the policy engine answers `require_approval`, **nothing in the cluster
changes**. The plan is written to the approval store as an approval request and
the incident waits in `AWAITING_APPROVAL`.

```
policy check ──require_approval──▶ approval request (pending)
                                      │
                        approve ──────┴────── reject
                           │                      │
             new task, same incident      incident → NEEDS_HUMAN
             (approval_id + force)        request  → rejected
                           │
                     repair runs → approval → executed
```

* One atomic JSON file per request under `AGENT_APPROVALS_DIR` (default:
  `AGENT_AUDIT_DIR/approvals`), so the worker that parks a plan and the API
  that decides it see the same files on the workspace PVC.
* States: `pending` → `approved` | `rejected` | `expired`, then `executed`
  (the repair ran) or `cancelled`.
* **Decide-once**: a second click on an already-decided request is a `409`,
  never a second repair.
* Approving enqueues a **new** task that carries `incident_id` +
  `approval_id` + `force: true`. The same incident continues — the history
  stays in one record instead of being split.
* The approved resume re-reads the policy, but only the `disabled` off switch
  may still stop it: **the human is the authorisation**.

### The playbooks

| Symptom | Cause | Playbook |
|---|---|---|
| `OOMKilled` | `oom` | raise the Deployment memory limit 25% (min 32Mi, cap `AGENT_OPS_MAX_MEMORY`), roll out, verify — the bump comes from the learned profile and is clamped here again |
| pod stuck `Terminating` beyond `AGENT_OPS_TERMINATION_GRACE` | `stuck` | `delete --grace-period=0 --force`, verify gone |
| `CrashLoopBackOff` with no config-shaped log evidence | `crashloop` | delete the pod so the ReplicaSet recreates it, verify Deployment Available |
| `ProgressDeadlineExceeded` / `ReplicaFailure` | `rollout` | `rollout undo`, verify |
| scale-to-zero | `replicas_zero` | scale 1, verify |
| image / config / scheduling / PVC / StatefulSet / bare failed pod | (report only) | `needs_human` with the exact fix |

Safety rules are enforced in `playbooks.py`, not only in RBAC: the namespace
must be in `AGENT_OPS_NAMESPACES` **and** in the `k8s_tools` allowlist,
`auton.io/protect=true` resources are skipped, and a missing/mismatched owner
(no Deployment to patch or controller to recreate the pod) goes to a human.
Every function re-reads the resource after acting, so "resolved" always means
verified, not attempted.

Each repair is wrapped in `k8s_tools.incident_scope(id)`; every audit record
(`begin`/`end`) then carries that `incident_id`, and `/audit?incident_id=`
returns the pair. Cooldown (`AGENT_OPS_COOLDOWN`, 1800 s) and an hourly budget
(`AGENT_OPS_MAX_ACTIONS_PER_HOUR`, 6) bound a flapping workload;
`AGENT_OPS_DRY_RUN=1` records `detected` + `dry_run` and mutates nothing.

Two graces keep normal Kubernetes churn out of the incident log:
`AGENT_OPS_STARTUP_GRACE` (120 s) ignores a `Pending`/`NotReady` pod while its
image is pulled or its container starts — `Unschedulable` is exempt and is
reported immediately — and it also suppresses the pod's Deployment, so a
rollout that is simply not Ready yet is not reported as "0 available".
`AGENT_OPS_TERMINATION_GRACE` (120 s) means a `DeletionTimestamp` alone is not
"stuck"; only a long termination is. A pod that reports several reasons gets
**one** finding: `_primary_reason()` picks by `REASON_PRIORITY` (`OOMKilled`
wins over `CrashLoopBackOff`), because two playbooks on one pod race — the
crashloop playbook deletes the pod the OOM playbook is verifying.

Run one pass from the host (needs a kubeconfig and the env in
`yamls/auton-ops-deployment.yaml`):

```bash
AGENT_NAMESPACES=auton,auton-apps AGENT_OPS_NAMESPACES=auton-apps \
AGENT_AUDIT_DIR=/tmp/auton-ops AGENT_OPS_DRY_RUN=1 AGENT_K8S_WRITE=1 \
  python ops_controller.py --once
```

`--once` is also what the chaos tests use. The notification payload is flat so
Slack/Telegram templates stay trivial:

```json
{"incident_id": "inc-…", "severity": "critical", "status": "resolved",
 "state": "RESOLVED", "namespace": "auton-apps", "resource": "pod/api-1",
 "summary": "OOMKilled", "root_cause": "OOMKilled", "confidence": 0.97,
 "recommended_action": "INCREASE_MEMORY_LIMIT", "requires_human": false,
 "occurrence_count": 1, "action": "raised memory 64Mi -> 96Mi",
 "verification": "ready=1/1", "report": "…",
 "url": "http://auton.localhost/incidents/inc-…"}
```

### The Learn stage (`learning.py`)

The pipeline ends with **verify**, and the result of that verification is fed
back into the next repair. `learning.py` is the feedback edge:

```
repair → verify → outcome ─┬─▶ profile (per workload + cause)
                            └─▶ adaptation (audit-gated) → next plan
```

**Outcome** — one repair attempt: cause, action, ok/failed, needs_human,
duration, incident id, the policy mode and the tier. One atomic JSON file per
outcome under `AGENT_LEARN_DIR` (default `AGENT_AUDIT_DIR/learning`), pruned
at `AGENT_LEARN_MAX_OUTCOMES`.

**Profile** — what the platform has learned about one *(workload, cause)*:
`attempts`, `successes`, `failures`, `consecutive_failures`, `recurrences`,
`memory_step`, `tier`, `cooldown_seconds`, `budget_per_hour`, `quarantined`
and the bounded adaptation history. The profile belongs to the **workload**,
not to the pod: an OOM kills a pod, but the repair changes the Deployment and
the rollout creates a new pod — a profile per pod would forget everything
after the first repair (`ops_controller._learning_resource()` resolves
pod → ReplicaSet → Deployment).

**Adaptation** — a named, closed allowlist. There is no free-form learning
rule and an unknown name is refused:

| Name | Trigger (`MIN_SAMPLES` = 3) | Effect | Cap |
|---|---|---|---|
| `raise_oom_step` | the cause was repaired ≥ 3× | the memory bump doubles | `MAX_STEP` (1.0) |
| `raise_tier` | the cause was repaired ≥ 6× | the next plan gets a higher tier | tier 3 |
| `raise_cooldown` | 3 failures in a row | the cooldown of that resource grows | `COOLDOWN_MAX` (6 h) |
| `lower_budget` | 3 failures in a row | fewer actions/hour for that resource | `BUDGET_MIN` (1) |
| `quarantine` | 3 failures in a row | the resource is not repaired any more → `needs_human` | — |

**The envelope** — learning may only make the platform *more cautious* or tune
a *bounded number*. It may never lower a tier, change the action of a plan,
widen what the platform may touch, exceed the caps, bypass the audit gate, or
run at all when the policy is `disabled`/`observe-only` or
`AGENT_LEARN_ENABLED=0`. Every clamp is applied twice: in `learning.py` **and**
in the playbook (`fix_oom(step=…)` clamps the fraction again).

**The audit gate** — an adaptation is written to the audit log
(`learn_adapt`, a `begin`/`end` pair) *before* it is applied. If the record
cannot be written, the adaptation is refused and the platform keeps its old
behaviour. "The platform changed its mind" is as auditable as "the platform
changed the cluster".

A learning failure never breaks the repair path: every call in the controller
is guarded, and an unwritable learning directory makes the service inert
(`NullStore`) instead of fatal.

### The metrics (`metrics.py`)

`GET /metrics` serves a **Prometheus exposition** in the standard text format
(`text/plain; version=0.0.4`) — written by hand, with the **standard library
only**. There is no `prometheus_client` dependency and no metrics sidecar: the
platform must not need a Python package to report that it is alive.

```python
reg = metrics.Registry()
reg.gauge("auton_up", "1 when the Auton API is serving metrics.", 1)
reg.counter("auton_http_responses_total", "HTTP responses sent.", 42)
reg.histogram("auton_repair_duration_seconds", "How long repairs took.",
              metrics.REPAIR_BUCKETS, [1.2, 8.0])
print(reg.render())
```

The module is three small pieces:

| Piece | What it does |
|---|---|
| `Registry` | the metric families of one scrape: `gauge`, `counter`, `histogram` (cumulative `_bucket`/`_sum`/`_count`) and `render()` |
| `escape_help` / `escape_label_value` / `format_value` | the escaping rules of the format (a raw backslash, quote or newline breaks the parse) |
| `collect(snapshot)` | the collectors: service, queue, watch, incidents, approvals, learning, policy, jobs, HTTP, process |

**The snapshot is the contract.** `agent_service._metrics_snapshot()` reads the
real state — the queue in Redis, the incident/approval/learning stores on the
shared PVC, the watcher health the scanner publishes, the policy document. The
key distinction:

* the key is **present** → the source was read. An empty store is a fact and
  is reported as a zero total;
* the key is **absent** → the source could not be read. The families are left
  out entirely, because a zero would be a guess.

Three rules the exposition follows:

1. **No invented numbers** — see above.
2. **No secrets** — counts, states and durations only; never a token, a key, a
   password or a resource body.
3. **Bounded cardinality** — the label values come from a closed set (causes,
   states, roles) plus the learned profiles, which are capped at
   `MAX_PROFILES` (50).

The endpoint needs the **bearer token**, like every other data endpoint, so a
scrape that forgets it gets `401` instead of a free status page:

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

`AUTON_VERSION` (the chart version) is exposed as the `version` label of
`auton_build_info`.

### The direct Kubernetes API client (`k8s_api.py` + `k8s_ops.py`)

The platform talks to the API server **directly over HTTPS** — no process is
forked, no client is started, and the answer arrives in milliseconds instead
of a tenth of a second. There is **no client library**: `urllib`, `ssl` and
`json` are enough, and a dependency is a dependency that can break the image.

```python
import k8s_api, k8s_ops

client = k8s_api.client()               # None outside the cluster
items, rv = client.list("/api/v1/namespaces/auton-apps/pods")
document = k8s_ops.get_object("pods", "api-1", "auton")
```

Two modules, two jobs:

| Module | What it does |
|---|---|
| `k8s_api.py` | the **transport**: `ApiConfig`, the credentials the kubelet projects (`/var/run/secrets/kubernetes.io/serviceaccount/{token,ca.crt}`), `ApiClient` (GET/POST/PATCH/DELETE/list/watch), the resource registry, the WebSocket exec channel and the transport counters |
| `k8s_ops.py` | the **vocabulary**: `get_object`, `list_objects`, `pod_logs`, `events`, `top_nodes`, `top_pods`, `apply_manifest`, `patch_object`, `scale`, `delete_object`, `rollout_restart`, `rollout_undo`, `rollout_status`, `exec_command`, `describe` |

The **registry is closed**: a resource outside it never reaches the network.
`resolve_resource()` accepts the kubectl aliases (`po`, `svc`, `pvc`,
`deploy`, `sts`, …) and ignores a group suffix (`deployments.apps`).

Four rules the client follows:

1. **The RBAC is still the boundary.** The client can do whatever the
   ServiceAccount may do and nothing else; it grants no extra permission.
2. **A refusal is a final answer.** `404`, `403` and `409` are raised as
   `ApiNotFound` / `ApiForbidden` / `ApiConflict` and are **never** retried
   with kubectl — kubectl would give the same answer.
3. **kubectl stays as a fallback, and it is never silent.** If the direct path
   is unavailable (no credentials) or fails (`ApiUnavailable`), the caller
   falls back to kubectl, logs `[k8s] using kubectl for …` and counts it in
   `k8s_api.COUNTERS` (`direct`, `direct_error`, `kubectl`, `unavailable`).
   The counters are published in `/tasks/stats` and in the Prometheus
   exposition (`auton_k8s_transport_total{transport="direct"|"kubectl"}`).
4. **Nothing is invented.** An error is reported with the API server's own
   status and message, not with a guess.

`AGENT_K8S_DIRECT=0` forces the kubectl path (it is how the two are
compared); the default is `1`.

Two operations need no YAML parser and no SPDY library:

* `k8s_apply` uses **server-side apply** (`application/apply-patch+yaml`) — the
  API server parses the YAML, so only the kind and the name are read locally
  (`_manifest_identity`);
* `k8s_exec` speaks the **`v4.channel.k8s.io` WebSocket** protocol directly —
  a hand-rolled RFC 6455 frame (`_ws_frame` / `_ws_read`), where the first
  byte of each frame is the channel (1 = stdout, 2 = stderr, 3 = error).

The watch (`watcher.py`) now uses the same client; its public surface did not
change (`watcher.ApiClient` is a thin subclass that turns an HTTP status into
"expired" or "forbidden").

## HTTP API

`GET /` and `GET /ui` serve the UI (`ui/index.html`, one file, no build step);
`GET /health` is the open JSON probe. Everything else requires:

```
Authorization: Bearer <token from the auton-api-token Secret>
```

The service **refuses to start** without `AGENT_API_TOKEN` unless
`AGENT_AUTH_DISABLED=1`, and an empty token answers `503` rather than becoming
"open". Errors: `401` missing/wrong token (with `WWW-Authenticate: Bearer`),
`503` no token configured. `GET /auth/check` is the cheapest authenticated
route: it exists so the UI can tell "wrong token" (`401`) from "API down".

Synchronous (default, and what the n8n workflows use):

```bash
TOKEN=$(kubectl -n auton get secret auton-api-token -o jsonpath='{.data.token}' | base64 -d)

curl -s http://localhost:8080/health
curl -s http://localhost:8080/run \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"task":"Create fib.py that prints the first 15 Fibonacci numbers, then run it."}' \
  | python3 -m json.tool

# mutation audit trail (auth required; 503 if the log is unwritable)
curl -s "http://localhost:8080/audit?limit=20" -H "Authorization: Bearer $TOKEN"
curl -s "http://localhost:8080/audit?incident_id=inc-..." -H "Authorization: Bearer $TOKEN"

# incidents recorded by auton-ops (auth required; 503 if the store is unwritable)
curl -s "http://localhost:8080/incidents?limit=20" -H "Authorization: Bearer $TOKEN"
curl -s "http://localhost:8080/incidents?status=needs_human" -H "Authorization: Bearer $TOKEN"
curl -s "http://localhost:8080/incidents?severity=critical&cause=oom" -H "Authorization: Bearer $TOKEN"
curl -s "http://localhost:8080/incidents?namespace=auton-apps" -H "Authorization: Bearer $TOKEN"
curl -s "http://localhost:8080/incidents?resource=pod/api-1" -H "Authorization: Bearer $TOKEN"
curl -s "http://localhost:8080/incidents?fingerprint=62b2dc54f7ce6775" -H "Authorization: Bearer $TOKEN"
curl -s "http://localhost:8080/incidents/inc-..." -H "Authorization: Bearer $TOKEN"

# active model provider (key masked) and a one-line round-trip
curl -s http://localhost:8080/provider -H "Authorization: Bearer $TOKEN"
curl -s -X POST http://localhost:8080/provider/test -H "Authorization: Bearer $TOKEN"

# what the platform has learned (auth required; 503 if the store is unwritable)
curl -s "http://localhost:8080/learning?limit=20" -H "Authorization: Bearer $TOKEN"
curl -s "http://localhost:8080/learning?cause=oom" -H "Authorization: Bearer $TOKEN"

# the Prometheus exposition (auth required; text/plain, version=0.0.4)
curl -s http://localhost:8080/metrics -H "Authorization: Bearer $TOKEN"

# change provider settings from the UI/API (partial update; api_key is write-only)
curl -s -X POST http://localhost:8080/provider -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"provider":"openai","model":"gpt-4o-mini","api_key":"sk-..."}'
curl -s -X DELETE http://localhost:8080/provider/key -H "Authorization: Bearer $TOKEN"
```

| Endpoint | Purpose |
|---|---|
| `GET /auth/check` | Token probe (`200` = accepted) |
| `GET /provider` | Active provider, masked key, `providers`/`policies`/`defaults` |
| `POST /provider` | Partial update; `400` for an invalid or key-less remote config |
| `DELETE /provider/key` | Remove the stored key (a remote provider then fails, no fallback) |
| `POST /provider/test` | One tiny completion through the active provider |
| `GET /audit?limit=&incident_id=` | JSONL-backed mutation records |
| `GET /incidents?limit=&status=&severity=&cause=&namespace=&resource=&fingerprint=` | Newest-first incident list (every filter is a comma list) |
| `GET /incidents/{id}` | One incident: diagnosis, structured evidence, timeline, repair plan, policy decision, actions, verification, report, audit ids |
| `POST /incidents/ingest` | What `auton-ops` calls; idempotent by `id`, validates the file name |
| `GET /policy` | The effective policy, its source (`configmap`/`default`/`invalid`), the modes, actions and tiers |
| `GET /approvals?limit=&status=&incident_id=` | Approval requests, newest first |
| `GET /approvals/{id}` | One approval request: the plan, the policy decision, the decision timeline |
| `POST /approvals/{id}/approve` | Approve a pending plan and enqueue the repair (`409` if it was already decided) |
| `POST /approvals/{id}/reject` | Reject a pending plan; the incident goes to `NEEDS_HUMAN` |
| `GET /learning?limit=&namespace=&resource=&cause=` | The Learn stage: the outcomes, the learned profiles, the statistics and the caps |
| `GET /metrics` | The Prometheus exposition (`text/plain; version=0.0.4`) — the queue, the watcher health, the incidents, the approvals, the learning, the policy, the jobs and the process |
| `GET /tasks/stats` | Queue depth, active counts, workers, the hourly budget and the **watcher health** (`watchers`: one entry per watch stream + a `scanner` summary) |

Response: `{job_id, final, steps, stop_reason, elapsed}`. A missing `task`
returns `400`; a queue overflow `429`; a failed job `500`; a sync wait that times
out `504` (with the `job_id` to poll).

### UI

`ui/index.html` is served verbatim at `/` and `/ui`. It is intentionally a
single dependency-free file (the cluster may have no egress) and uses only
same-origin `fetch` with the bearer token the user pastes in; the token lives in
`localStorage`.

Authentication is explained in an **API access** modal rather than a bare
`prompt()`: it says why the API is protected (it can run commands and patch
workloads), prints the `kubectl get secret auton-api-token …` command with a
copy button, states that the token stays in this browser, and offers *Test and
save* (`GET /auth/check`), *Save*, *Forget token* and *Close*. A banner remains
visible while no valid token is stored and any `401` re-opens the modal with the
rejected request named. A single `refreshHealth()` owns the health chip.

Tabs: **Chat** (`POST /run?async=1` + poll `/jobs/{id}`, cancel),
**Incidents** (`GET /incidents`, `GET /incidents/{id}`), **Audit**
(`GET /audit`) and **Provider** — a real form: provider, model, base URL, max
tokens, data policy and a write-only API key, with *Save settings*, *Clear API
key* and *Send test completion*. The page itself is open because it contains no
data; do not move secrets or records into the HTML.

### Async jobs

The GPU worker is single-threaded and FIFO, so long tasks should not hold an
HTTP connection:

```bash
JOB=$(curl -s -X POST 'http://localhost:8080/run?async=1' \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"task":"Refactor fib.py and run the tests."}' \
  | python3 -c 'import json,sys;print(json.load(sys.stdin)["job_id"])')

curl -s -H "Authorization: Bearer $TOKEN" "http://localhost:8080/jobs/$JOB" | python3 -m json.tool
curl -N -H "Authorization: Bearer $TOKEN" "http://localhost:8080/jobs/$JOB/events"      # SSE
curl -s -X POST -H "Authorization: Bearer $TOKEN" "http://localhost:8080/jobs/$JOB/cancel"
```

| Endpoint | Purpose |
|---|---|
| `POST /run?async=1` (or `{"async": true}`) | `202 {job_id, status, poll}` |
| `GET /jobs` | Last 50 jobs |
| `GET /jobs/{id}` | Status, elapsed, log tail, result |
| `GET /jobs/{id}/events` | SSE: log lines + status events |
| `POST /jobs/{id}/cancel` | Cooperative stop (queued jobs stop immediately) |

Limits: `AGENT_MAX_QUEUE` (8 pending, then `429`), `AGENT_JOB_TIMEOUT` (900 s),
`AGENT_MAX_JOBS` (100 retained).

All Services are ClusterIP, so port-forward first:

```bash
kubectl -n auton port-forward --address 127.0.0.1 svc/auton 8080:8080
```

### n8n integration

Both workflows in `../n8n/` call the agent over in-cluster DNS with
`Authorization: Bearer {{ $env.AGENT_API_TOKEN }}`:

| Workflow | Trigger | Behaviour |
|---|---|---|
| `ai-agent-demo.json` (Auton Agent Demo) | Manual ("Execute workflow") | Hardcoded demo task, sync `POST /run` |
| `ai-agent-webhook.json` (Auton Agent Webhook) | `POST /webhook/auton-agent` | Starts `POST /run?async=1`, polls `/jobs/{id}` every 5 s, responds when done |

Imported workflows are **inactive**. Activate the webhook workflow in the UI (or
let `n8n_create_agent_pipeline` create and activate one), then:

```bash
# from the host, with a port-forward on 5678
curl -s -X POST http://localhost:5678/webhook/auton-agent \
  -H 'Content-Type: application/json' \
  -d '{"task":"Create hello_n8n.py that prints hi, and run it."}'
```

`n8n` receives `AGENT_API_TOKEN` from the `auton-api-token` Secret, which is why
the `$env` expression works (`N8N_BLOCK_ENV_ACCESS_IN_NODE=false`).

The webhook workflow intentionally does not wait on one long HTTP request: a slow
7B run would hit proxy/n8n timeouts. It starts the job asynchronously and loops
`Wait → Get Job → Done?` in 5 s steps; only `done`, `error` and `cancelled` end
the loop. If you hand-edit it, keep the `Done?` status list and the false-branch
connection back to `Wait` intact.

---

## Build, deploy, test

```bash
# from the repository root
docker build -t auton:local ./mcp
k3d image import auton:local -c ai-cluster
kubectl apply -f yamls/agent-deployment.yaml
kubectl -n auton rollout status deploy/auton
```

After changing any Python file:

```bash
docker build -t auton:local ./mcp
k3d image import auton:local -c ai-cluster
kubectl -n auton rollout restart deploy/auton
```

There is no registry: `imagePullPolicy: IfNotPresent` makes the pod use the
imported image.

Run the tests locally (no cluster, GPU or Ollama required):

```bash
cd mcp
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt pytest
python -m pytest tests -q
```

The suite covers workspace escape, unique-match editing, atomic patches,
gitignore-aware search, background jobs, git tools, context compaction, the real
MCP stdio transport (all 34 tools), the HTTP jobs API, bearer auth (401/503 and
fail-closed startup), the Kubernetes tools (direct HTTPS first with the kubectl
fallback, namespace allowlist, validation, write gate,
snapshot/diagnose/events/wait/exec), the write-ahead audit log
(fail-closed, begin/end pairing, SQLite index), the provider layer
(config precedence, both remote adapters, redaction, no fallback), the n8n
client (mocked HTTP, generated agent/ops pipelines), the **Incident Manager v2**
(fingerprint, the state machine and its adjacency table, recurrence linking,
auto-resolve, severity history, the Redis index and its rebuild), the
**Evidence Collector** (shape, no-Secret guarantee, unavailable-marked-not-
invented), the **Diagnosis Engine** (root-cause discrimination, the action
allowlist, the advisory LLM validation), the **Policy Engine** (the four
modes, the tiers, the rules, protected targets, fail-closed loading), the
**Repair Planner** (the plan shape, the tier map, the rollback record), the
**Approval Store** (decide-once, expiry, filtering), the **policy-gated
pipeline** (auto / observe / deny / await-approval → approve → repair →
executed), the **incident pipeline** (detect →
diagnose → repair → verify, report-only, dry run, retry-reuses-the-incident),
the playbooks (OOM math, owner-chain and protect/namespace guards, report-only
causes), the ops controller (rule table, cooldown, hourly budget, dry-run,
report payload), the **Kubernetes watcher** (the URLs and headers, the event
parsing, the 410-Gone re-list, the backoff, the counters, the event cache, the
fast path and the polling fallback), the **executor write scope** (the RBAC
split, the read-only default, the opt-in manifest and the tool gate), the
**queue authentication** (the password source, the fail-closed client, the
server config and the manifest checks), the queue backends (`memory`/`redis`,
no silent fallback) and
the RBAC manifests (`tests/test_rbac.py` fails if a Role ever grants secrets or
PVC delete, and checks the ops identity is read-only in `auton`) and the
**manifest drift** (`tests/test_manifests.py` fails when `yamls/` no longer
matches the Helm chart it was generated from).

The suite is the only automated check in this repository — there is no build
system, linter or CI. Expect **746 passed**.

### Eval harness

Unit tests prove the tools behave; the eval harness measures whether the model
can actually use them. `eval/run_eval.py` drives the real loop over eight
scripted coding tasks (plus four live-cluster tasks) and checks the resulting
workspace:

```bash
cd mcp
python eval/run_eval.py --list              # 1..12, name per task
python eval/run_eval.py                     # all runnable tasks
python eval/run_eval.py --only 2 --verbose  # one task, stream agent logs
```

Each task wipes the workspace, seeds files, runs one prompt through `run_agent`,
then verifies the result by reading files or executing them. It needs a reachable
Ollama (`OLLAMA_HOST`) with the model pulled; exit code is `0` only when every
selected task passes. The workspace defaults to `eval-workspace/` (gitignored and
dockerignored) and can be overridden with `--workspace`.

| # | Task | Skills exercised |
|---|---|---|
| 1 | `create-and-run` | `write_file`, `bash` |
| 2 | `fix-failing-python` | `glob`, `read_file`, `apply_patch`, `run_tests` |
| 3 | `targeted-edit` | `apply_patch` / `edit_file`, `run_tests` |
| 4 | `multi-file-create` | `write_file` ×N, `bash` |
| 5 | `refactor-rename` | `grep`, `apply_patch`, `bash` |
| 6 | `add-cli-flag` | `read_file`, `apply_patch`, `bash` |
| 7 | `cleanup-dead-file` | `fs(delete)`, `apply_patch` |
| 8 | `fix-and-keep-tests` | `read_file`, `apply_patch`, `run_tests` |
| 9 | `k8s-inspect` | `k8s_get` — skipped unless a cluster is reachable |
| 10 | `n8n-list` | `n8n_workflows` — skipped unless n8n and an API key are reachable |
| 11 | `pod-triage` | `k8s_snapshot`, `k8s_diagnose`, `write_file` — creates a bad-image Deployment in `auton-apps`; skipped unless the namespace exists |
| 12 | `deploy-health` | `k8s_snapshot` — healthy Deployment, checks the model reads instead of inventing |

The two live tasks use `requires` preflights, so the coding tasks stay runnable
on a laptop with no kubeconfig. To run them from the host against the cluster:

```bash
AGENT_NAMESPACE=auton \
N8N_API_URL=http://localhost:5678/api/v1 \
N8N_WEBHOOK_BASE=http://localhost:5678/webhook \
N8N_API_KEY_FILE=~/.config/auton/n8n-api-key \
  python eval/run_eval.py --only 9
```

---

## CLI and local development

```bash
cd mcp
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

Run a single task against any reachable Ollama:

```bash
OLLAMA_HOST=http://localhost:11434 \
AGENT_WORKSPACE="$PWD/workspace" \
AGENT_MODEL=qwen2.5-coder:7b-instruct \
  python agent_client.py "Write greet.py and run it."
```

To use the cluster's Ollama, forward its service first:

```bash
kubectl -n auton port-forward svc/ollama-service 11434:11434
```

Run the HTTP front end outside Kubernetes the same way (auth off for local
development only):

```bash
OLLAMA_HOST=http://localhost:11434 AGENT_WORKSPACE="$PWD/workspace" \
AGENT_AUTH_DISABLED=1 python agent_service.py
```

To try a remote provider without touching the cluster, set the env fallbacks
(files under `AGENT_MODEL_DIR` win when they exist):

```bash
AGENT_PROVIDER=openai AGENT_LLM_MODEL=gpt-4o-mini AGENT_LLM_API_KEY=sk-... \
AGENT_WORKSPACE="$PWD/workspace" \
  python agent_client.py "Check the workspace and report."
```

`AGENT_DATA_POLICY=local-only` makes a remote provider fail instead of sending
anything; there is no fallback to the local model.

The Kubernetes tools use the caller's kubeconfig on the host (kubectl installed
and the k3d context merged) and `AGENT_NAMESPACE` (default `auton`). The n8n
tools need `N8N_API_URL` / `N8N_WEBHOOK_BASE` pointed at a port-forward and a key
in `N8N_API_KEY` or `N8N_API_KEY_FILE`.

For host runs, set `AGENT_SANDBOX=bwrap` to confine shell commands to the
workspace (read-only root, `/tmp` tmpfs, optional `AGENT_SANDBOX_NET=off`).

If `import mcp` fails, the local `.venv` is stale after a Python upgrade —
delete it and recreate it.

---

## Configuration

| Variable | Default | Used by | Meaning |
|---|---|---|---|
| `OLLAMA_HOST` | `http://localhost:11434` | core / service | Ollama base URL (pod: `http://ollama-service:11434`) |
| `AGENT_MODEL` | `qwen2.5-coder:7b-instruct` | core / service | Fallback model tag when no config file is mounted |
| `AGENT_MODEL_DIR` | `/etc/auton-model` | llm | Provider config root (`config/`, `secret/`); read per request |
| `AGENT_PROVIDER` | `ollama` | llm | Env fallback for the provider |
| `AGENT_LLM_BASE_URL` | *(provider default)* | llm | Env fallback for the API base |
| `AGENT_LLM_MODEL` | *(provider default)* | llm | Env fallback for the model name |
| `AGENT_LLM_API_KEY` | *(none)* | llm | Env fallback for the key (prefer the Secret file) |
| `AGENT_LLM_MAX_TOKENS` | `1024` | llm | Response cap for remote providers |
| `AGENT_LLM_TIMEOUT` | `120` | llm | Remote HTTP timeout, seconds |
| `AGENT_DATA_POLICY` | `redact` | llm | `redact` / `allow` / `local-only` |
| `AGENT_PROVIDER_STORE` | `kubectl` | provider_admin | `kubectl` (patch ConfigMap/Secret) or `files` (local dev) |
| `AGENT_PROVIDER_NAMESPACE` | `AGENT_NAMESPACE` | provider_admin | Namespace of the provider objects |
| `AGENT_PROVIDER_CONFIGMAP` | `auton-model-config` | provider_admin | ConfigMap the API may patch |
| `AGENT_PROVIDER_SECRET` | `auton-model` | provider_admin | Secret the API may patch (key only) |
| `AGENT_WORKSPACE` | `./workspace` | server | Root the file tools are jailed to (pod: `/workspace`) |
| `MAX_STEPS` | `25` | core | Iteration budget per task |
| `AGENT_NUM_CTX` | `16384` | core | Ollama context window |
| `AGENT_MAX_OBSERVATION_CHARS` | `8000` | core | Per-result cap |
| `AGENT_MAX_HISTORY_CHARS` | `60000` | core | Compaction threshold |
| `AGENT_KEEP_OBSERVATIONS` | `3` | core | Verbatim recent results |
| `AGENT_PARSE_RETRIES` | `1` | core | Corrective retries |
| `AGENT_REPEAT_LIMIT` | `2` | core | Identical calls, identical failures, or identical output with different args before nudging, then stopping |
| `AGENT_REPEAT_PENALTY` | `1.1` | core | `repeat_penalty` passed to Ollama |
| `AGENT_MAX_READ_CHARS` | `20000` | server | `read_file` default cap |
| `AGENT_MAX_OUTPUT_CHARS` | `8000` | server | `bash` / kubectl / n8n output cap |
| `AGENT_MAX_DIFF_CHARS` | `12000` | server | Diff cap |
| `AGENT_SANDBOX` | `off` | server | `bwrap` to sandbox shell tools |
| `AGENT_SANDBOX_NET` | `on` | server | `off` → `--unshare-net` |
| `AGENT_ALLOW_TEST_EDITS` | `0` | server | `1` allows edit tools to rewrite/remove test assertions |
| `AGENT_SYSTEM_EXTRA` | *(empty)* | core | Extra system-prompt text |
| `AGENT_PORT` | `8080` | service | HTTP listen port |
| `AGENT_BIND` | `0.0.0.0` | service | HTTP bind address |
| `AGENT_MAX_QUEUE` | `8` | service | Pending jobs before 429 |
| `AGENT_JOB_TIMEOUT` | `900` | service | Wall-clock limit per job |
| `AGENT_MAX_JOBS` | `100` | service | Retained job records |
| `AGENT_API_TOKEN` | *(none)* | service | Bearer token; required or the service refuses to start |
| `AGENT_AUTH_DISABLED` | *(unset)* | service | `1` disables auth — local development only |
| `AGENT_NAMESPACE` | `auton` | k8s | Default namespace for the Kubernetes tools |
| `AGENT_NAMESPACES` | `AGENT_NAMESPACE` | k8s | Comma allowlist; deployment sets `auton,auton-apps` |
| `AGENT_AUDIT_DIR` | `$AGENT_WORKSPACE/.ops` | k8s | Write-ahead audit log + SQLite index |
| `AGENT_INCIDENTS_DIR` | `$AGENT_AUDIT_DIR/incidents` | incidents | Where the incident JSON files live (shared PVC) |
| `AGENT_APPROVALS_DIR` | `$AGENT_AUDIT_DIR/approvals` | approvals | Where the approval requests live (shared PVC) |
| `AGENT_POLICY_DIR` | `/etc/auton-policy` | policy | Where the projected policy ConfigMap is mounted |
| `AGENT_POLICY_MODE` | *(none)* | policy | Mode override when no policy document is present |
| `AGENT_OPS_NAMESPACES` | `auton-apps` | ops | Namespaces the ops loop watches and may repair |
| `AGENT_OPS_INTERVAL` | `60` | ops | Snapshot interval, seconds |
| `AGENT_OPS_COOLDOWN` | `1800` | ops | Per-resource/cause cooldown, seconds |
| `AGENT_OPS_MAX_ACTIONS_PER_HOUR` | `6` | ops | Hourly repair budget |
| `AGENT_OPS_STARTUP_GRACE` | `120` | ops | Suppress `Pending`/`NotReady` for a young pod, seconds |
| `AGENT_OPS_TERMINATION_GRACE` | `120` | ops | A `DeletionTimestamp` alone is not "stuck", seconds |
| `AGENT_OPS_MAX_MEMORY` | *(none)* | playbooks | Cap for the raised memory limit |
| `AGENT_OPS_HEALTHY_THRESHOLD` | `3` | incidents | Healthy passes needed before an incident auto-resolves |
| `AGENT_OPS_MAX_INCIDENTS` | `500` | incidents | Incident files kept before the oldest are pruned |
| `AGENT_OPS_PRUNE_EVERY` | `60` | incidents | Prune interval, seconds |
| `AGENT_OPS_DRY_RUN` | `0` | ops | `1` records the diagnosis and mutates nothing |
| `AGENT_OPS_LLM_DIAGNOSIS` | `0` | diagnosis | `1` lets the model *refine* a diagnosis (advisory only) |
| `AGENT_QUEUE` | `memory` | queue | `memory` (local) or `redis` (the cluster manifests) — **no silent fallback** |
| `AGENT_REDIS_URL` | `redis://redis:6379/0` | queue | Redis endpoint for the queue backend |
| `AGENT_REDIS_PASSWORD` | *(none)* | queue | Redis password — local runs only; the manifests mount the Secret instead |
| `AGENT_REDIS_PASSWORD_FILE` | *(none)* | queue | File the Redis password is read from (the manifests: `/etc/auton-redis/password`) |
| `AGENT_QUEUE_PREFIX` | `auton` | queue | Redis key prefix |
| `AGENT_QUEUE_MAX` | *(queue-dependent)* | queue | Maximum queued tasks |
| `AGENT_SCAN_INTERVAL` | `60` | scanner | Scan interval, seconds |
| `AGENT_SCAN_DEDUPE_WINDOW` | `300` | scanner | Window in which the same finding is not resubmitted, seconds |
| `AGENT_WATCH_ENABLED` | `1` | watcher | `0` keeps the old polling behaviour |
| `AGENT_WATCH_FAST_INTERVAL` | `60` | watcher | Fast tier: reads the watch cache (pod-level rules), seconds |
| `AGENT_WATCH_FULL_INTERVAL` | `300` | watcher | Full tier: takes a real snapshot (every rule), seconds |
| `AGENT_WATCH_TIMEOUT` | `300` | watcher | How long the API server keeps one watch stream open, seconds |
| `AGENT_WATCH_MIN_GAP` | `5` | watcher | Smallest gap between two event-triggered scans, seconds |
| `AGENT_K8S_API` | *(in-cluster)* | watcher | API server base URL; `AGENT_K8S_TOKEN`/`AGENT_K8S_CA_FILE` override the projected ServiceAccount |
| `AGENT_K8S_ENABLED` | `1` | k8s | `0` disables the Kubernetes tools |
| `AGENT_K8S_WRITE` | `0` (read-only) / `1` (worker, opt-in executor) | k8s | Allows apply/patch/scale/rollout/delete/exec |
| `N8N_API_URL` | `http://n8n:5678/api/v1` | n8n | n8n public API base |
| `N8N_WEBHOOK_BASE` | `http://n8n:5678/webhook` | n8n | Webhook base |
| `N8N_API_KEY_FILE` | `/etc/n8n-api/api-key` | n8n | Key file, read on every call |
| `N8N_API_KEY` | *(none)* | n8n | Key value fallback (overrides the file) |
| `N8N_API_TIMEOUT` | `30` | n8n | Public-API timeout, seconds |
| `N8N_WEBHOOK_TIMEOUT` | `900` | n8n | Webhook timeout, seconds |
| `AGENT_RUN_URL` | `http://auton:8080/run` | n8n | URL baked into generated pipelines |
| `AGENT_BASE_URL` | derived from `AGENT_RUN_URL` | n8n | Base for the `/jobs/{id}` polling node |

---

## Safety and limitations

- **The LLM executor is read-only by default.** Since Phase 4 the
  `auton-agent` identity has no write verb at all: it may `get/list/watch`
  pods, workloads, events and logs in `auton` and `auton-apps`, and nothing
  more. The write half lives in a separate opt-in manifest
  (`yamls/auton-rbac-write.yaml`) and is enabled with
  `scripts/set-agent-writes.sh --enable` (`agent.allowWrites=true` in Helm).
  Even with `kubectl` and its token in the pod, a shell command that tries to
  patch a Deployment is refused by the API server. The repair path is
  unaffected: `auton-worker` remains the only component that mutates a
  workload by default.
- **The queue requires a password.** Since Phase 5 Redis answers an
  unauthenticated client with `NOAUTH Authentication required.`, and every pod
  authenticates with the Secret `auton-redis`. The password is not in any
  manifest, pod spec, environment variable or command line. The clients fail
  closed on an auth mismatch rather than retrying without a password.
- **Every mutation is audited before it runs.** `audit.py` appends and fsyncs a
  `begin` record to `$AGENT_AUDIT_DIR/audit.jsonl`; if that write fails the
  kubectl call is skipped. `GET /audit` exposes the records (auth required).
  Deleting this directory does not hide history from the operator — it makes
  every mutation fail closed.
- **The API has bearer auth**, but no rate limiting. `/health` is open; every
  other endpoint needs `Authorization: Bearer $AGENT_API_TOKEN`. The service
  fails closed without a token. Do not expose port 8080 beyond localhost/ingress
  without TLS in front.
- **`bash` is a real shell.** The workspace jail covers the file/search tools
  only; a shell can reach absolute paths. In the pod it has whatever the image
  ships (plus kubectl); on the host it has your tools. `AGENT_SANDBOX=bwrap`
  narrows this.
- **Small-model reliability.** A 7B coder model occasionally emits malformed
  calls or stops early. The JSON fallback, corrective retry, unique-match edits
  and `MAX_STEPS` cap are the mitigations; `stop_reason` tells you which one
  fired.
- **Output is only as good as the workspace.** The agent verifies work by running
  it, so give it tasks it can execute inside `/workspace`.
- **n8n has no built-in auth for the API key file.** The key grants workflow and
  execution access for the whole n8n instance; scope it down when creating it.

See the [root README](../README.md) for cluster, GPU, Ollama, n8n and ingress
setup, plus troubleshooting.
