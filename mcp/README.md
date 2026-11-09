# Auton — Local Coding Agent (MCP + Ollama)

The `auton` service: an autonomous coding agent that runs entirely on a local
k3d cluster and drives a model in a tool-calling loop. Local Ollama is the
default; OpenAI, Gemini, Claude and any OpenAI-compatible endpoint can be
selected instead. Besides the workspace, it can read and change its own
Kubernetes namespace and manage n8n.

- **Ollama** serves `qwen2.5-coder:7b-instruct` on the AMD RX 6600
  (Vulkan backend, full GPU offload) — the default provider.
- **`llm.py`** is the provider layer; configuration is read per request, so a
  switch needs no restart, and a remote provider never falls back to local.
- **`agent_service.py`** is the HTTP front end that runs in the pod (sync
  `/run` plus an async jobs API), with bearer-token auth.
- **`agent_core.py`** is the reusable agent loop, including context budgeting
  and cancellation.
- **`agent_server.py`** is the MCP stdio server that gives the model workspace,
  search, patch, shell, git, Kubernetes and n8n tools.
- **`agent_client.py`** is the CLI equivalent of the HTTP service.

For cluster creation, GPU setup, Ollama, n8n and ingress, see the
[root README](../README.md). This document covers only the agent.

```
                 auton pod (:8080, ClusterIP)
  ┌───────────────────────────────────────────────────────┐
  │  agent_service.py                                     │
  │    └─ job queue ─▶ agent_core.run_agent()             │
  │         │ spawns (stdio, local subprocess)            │
  │         ▼                                             │
  │  agent_server.py   MCP tools                          │
  │    workspace · search · patch · shell · git           │
  │    kubectl (SA auton-agent) · n8n REST                │
  │         │ HTTP                          │ kubectl / HTTP
  │         ▼                               ▼             │
  │  ollama-service:11434          kube-apiserver · n8n   │
  │  → ollama pod (GPU)            (:443, in-cluster)     │
  └───────────────────────────────────────────────────────┘
```

The namespace, Service and Deployment are all named `auton`; the Service is
**ClusterIP**, so use `kubectl port-forward` (or the Traefik ingress) from the
host.

---

## Python files

| File | Purpose |
|---|---|
| `agent_server.py` | MCP server over stdio: registers the 32 tools below (plus a deprecated `run_command` alias) |
| `audit.py` | Write-ahead audit log: JSONL + SQLite, fail-closed, `begin`/`finish`/`record` |
| `k8s_tools.py` | `kubectl` wrapper: 13 Kubernetes tools (diagnosis + audit-gated mutations) |
| `llm.py` | Provider layer: `ollama`/`openai`/`gemini`/`anthropic`/`custom`, config read per request, redaction, no fallback |
| `n8n_tools.py` | n8n public-API + webhook client: 5 tools, stdlib HTTP only |
| `agent_core.py` | Agent loop: dual-path tool calling, observation caps, compaction, corrective retry, cancellation |
| `agent_client.py` | CLI entry point (`python agent_client.py "task"`) |
| `agent_service.py` | HTTP front end: bearer auth, sync `POST /run`, async `/jobs` API, SSE, cancel, `GET /audit` |
| `Dockerfile` | Builds `auton:local` (`python:3.11-slim` + git, ripgrep, bubblewrap, kubectl) |
| `requirements.txt` | `mcp>=2.0,<3`, `ollama>=0.4.0`, `pathspec>=0.12` |
| `tests/` | pytest suite (tools, loop helpers, MCP transport, HTTP service, auth, k8s/n8n) |
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
- every mutation calls `audit.begin()` **before** kubectl and is refused if the
  record cannot be written (`audit.py`); `audit.finish()` records the outcome;
- PVC delete is refused in code *and* RBAC, with a hint telling the model to
  report and ask a human;
- output is clipped (`AGENT_MAX_OUTPUT_CHARS`).

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
remove the workaround.

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

## HTTP API

`/health` (and `/`) are open for probes. Everything else requires:

```
Authorization: Bearer <token from the auton-api-token Secret>
```

The service **refuses to start** without `AGENT_API_TOKEN` unless
`AGENT_AUTH_DISABLED=1`, and an empty token answers `503` rather than becoming
"open". Errors: `401` missing/wrong token, `503` no token configured.

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

# active model provider (key masked) and a one-line round-trip
curl -s http://localhost:8080/provider -H "Authorization: Bearer $TOKEN"
curl -s -X POST http://localhost:8080/provider/test -H "Authorization: Bearer $TOKEN"
```

Response: `{job_id, final, steps, stop_reason, elapsed}`. A missing `task`
returns `400`; a queue overflow `429`; a failed job `500`; a sync wait that times
out `504` (with the `job_id` to poll).

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
MCP stdio transport (all 32 tools), the HTTP jobs API, bearer auth (401/503 and
fail-closed startup), the kubectl wrapper (namespace allowlist, validation,
write gate, snapshot/diagnose/events/wait/exec), the write-ahead audit log
(fail-closed, begin/end pairing, SQLite index), the provider layer
(config precedence, both remote adapters, redaction, no fallback), the n8n
client (mocked HTTP) and the RBAC manifests (`tests/test_rbac.py` fails if a
Role ever grants secrets or PVC delete).

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
| `AGENT_K8S_ENABLED` | `1` | k8s | `0` disables the Kubernetes tools |
| `AGENT_K8S_WRITE` | `0` (image) / `1` (deployment) | k8s | Allows apply/patch/scale/rollout/delete/exec |
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

- **The agent can change its own namespace.** `AGENT_K8S_WRITE=1` plus the
  `auton-agent` Role allow CRUD on workloads, services, configmaps, PVCs and jobs
  in `auton` and `auton-apps`. The Role deliberately denies secrets,
  serviceaccounts, roles/rolebindings, PVC delete and namespace delete;
  cluster-scoped access is read-only. Creating pods is still powerful (a pod
  spec can reference a Secret by name), so keep long-lived credentials out of
  these namespaces.
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
