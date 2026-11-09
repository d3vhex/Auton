#!/usr/bin/env python3
"""n8n tools for the Auton agent (REST API v1, stdlib only).

n8n has no environment variable for API keys: create one once in the UI
(Settings -> n8n API) and store it as the Secret ``n8n-api``. The key is read
from ``N8N_API_KEY_FILE`` on **every call**, so rotating the Secret does not
require restarting the agent.

Environment:
  N8N_API_URL       default http://n8n:5678/api/v1
  N8N_WEBHOOK_BASE  default http://n8n:5678/webhook
  N8N_API_KEY_FILE  default /etc/n8n-api/api-key (fallback: N8N_API_KEY)
  AGENT_RUN_URL     default http://auton:8080/run (used by the pipeline template)
  AGENT_BASE_URL    default derived from AGENT_RUN_URL, used for /jobs/{id}
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
import uuid

API_URL = os.environ.get("N8N_API_URL", "http://n8n:5678/api/v1").rstrip("/")
WEBHOOK_BASE = os.environ.get("N8N_WEBHOOK_BASE", "http://n8n:5678/webhook").rstrip("/")
KEY_FILE = os.environ.get("N8N_API_KEY_FILE", "/etc/n8n-api/api-key")
AGENT_RUN_URL = os.environ.get("AGENT_RUN_URL", "http://auton:8080/run")
# Base URL for the jobs API; derived from AGENT_RUN_URL (strip the /run suffix).
AGENT_BASE_URL = os.environ.get(
    "AGENT_BASE_URL", AGENT_RUN_URL.rsplit("/run", 1)[0]
).rstrip("/")
ASYNC_RUN_URL = AGENT_RUN_URL + ("&" if "?" in AGENT_RUN_URL else "?") + "async=1"
TIMEOUT = int(os.environ.get("N8N_API_TIMEOUT", "30"))
WEBHOOK_TIMEOUT = int(os.environ.get("N8N_WEBHOOK_TIMEOUT", "900"))
MAX_CHARS = int(os.environ.get("AGENT_MAX_OUTPUT_CHARS", "8000"))

_PATH_RE = re.compile(r"^[a-z0-9][a-z0-9/_-]*$")


def _err(message: str, hint: str = "") -> str:
    return json.dumps({"ok": False, "error": message, "hint": hint})


def _clip(text: str) -> str:
    if len(text) <= MAX_CHARS:
        return text
    head = MAX_CHARS * 2 // 3
    tail = MAX_CHARS - head
    return text[:head] + f"\n… ({len(text) - MAX_CHARS} chars clipped) …\n" + text[-tail:]


def _api_key() -> str | None:
    key = os.environ.get("N8N_API_KEY", "").strip()
    if key:
        return key
    try:
        with open(KEY_FILE, encoding="utf-8") as fh:
            return fh.read().strip() or None
    except OSError:
        return None


def _http(method: str, url: str, body: dict | None = None,
          timeout: int = TIMEOUT) -> tuple[int, str]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    key = _api_key()
    if key:
        headers["X-N8N-API-KEY"] = key
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as exc:
        return 0, str(exc)


def _api(method: str, path: str, body: dict | None = None) -> str:
    key = _api_key()
    if not key:
        return _err("no n8n API key available.",
                    f"Create one in n8n (Settings -> n8n API) and store it as "
                    f"Secret 'n8n-api' key 'api-key' (mounted at {KEY_FILE}).")
    status, text = _http(method, f"{API_URL}{path}", body)
    if status == 0:
        return _err(f"n8n is unreachable at {API_URL}: {_clip(text)[:300]}",
                    "Check the n8n pod with k8s_get('pods') and k8s_logs('n8n-...').")
    if status >= 400:
        return _err(f"n8n API {method} {path} -> HTTP {status}: {_clip(text)[:800]}",
                    "401/403 means the API key is wrong or was revoked.")
    return text


def _summarize_workflow(wf: dict) -> dict:
    return {
        "id": wf.get("id"),
        "name": wf.get("name"),
        "active": wf.get("active"),
        "updatedAt": wf.get("updatedAt"),
    }


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #

def n8n_workflows(action: str = "list", workflow_id: str = "",
                  payload: str = "") -> str:
    """Manage n8n workflows: list | get | create | update | delete.

    payload is a JSON object string for create/update (name, nodes, connections,
    settings). For a ready-made agent pipeline use n8n_create_agent_pipeline().
    """
    action = (action or "list").strip().lower()
    if action == "list":
        status_text = _api("GET", "/workflows?limit=100")
        if status_text.startswith('{"ok": false'):
            return status_text
        try:
            data = json.loads(status_text)
        except json.JSONDecodeError:
            return _err("n8n returned non-JSON for the workflow list.",
                        _clip(status_text)[:400])
        items = data.get("data", data if isinstance(data, list) else [])
        return json.dumps({"count": len(items),
                           "workflows": [_summarize_workflow(w) for w in items]},
                          indent=2)

    if action in ("get", "update", "delete", "activate", "deactivate"):
        if not workflow_id.strip():
            return _err(f"workflow_id is required for {action}.",
                        "Get ids from n8n_workflows('list').")
        if action == "get":
            return _clip(_api("GET", f"/workflows/{workflow_id.strip()}"))
        if action == "delete":
            resp = _api("DELETE", f"/workflows/{workflow_id.strip()}")
            if resp.startswith('{"ok": false'):
                return resp
            # n8n echoes the whole workflow back; a 6-node graph is a lot of
            # context for a model that only needs to know the delete worked.
            return json.dumps({"ok": True,
                               "deleted": workflow_id.strip()})
        if action in ("activate", "deactivate"):
            return _api("POST", f"/workflows/{workflow_id.strip()}/{action}")
        body = _parse_payload(payload)
        if isinstance(body, str):
            return body
        return _clip(_api("PUT", f"/workflows/{workflow_id.strip()}", body))

    if action == "create":
        body = _parse_payload(payload)
        if isinstance(body, str):
            return body
        new_id = None
        text = _api("POST", "/workflows", body)
        if text.startswith('{"ok": false'):
            return text
        try:
            new_id = json.loads(text).get("id")
        except (json.JSONDecodeError, AttributeError):
            return _clip(text)
        return f"Created workflow {new_id} ({body.get('name', 'unnamed')}).\n" + _clip(text)[:1500]

    return _err(f"unknown action {action!r}",
                "Use list, get, create, update, delete, activate or deactivate.")


def _parse_payload(payload: str) -> dict | str:
    if not payload.strip():
        return _err("payload is required for create/update.",
                    "Pass a JSON object with name, nodes, connections, settings.")
    try:
        body = json.loads(payload)
    except json.JSONDecodeError as exc:
        return _err(f"payload is not valid JSON: {exc}",
                    "Pass a JSON object string, not YAML.")
    if not isinstance(body, dict):
        return _err("payload must be a JSON object.")
    return body


def n8n_activate(workflow_id: str, active: bool = True) -> str:
    """Activate or deactivate a workflow (deactivated workflows do not fire webhooks)."""
    if not workflow_id.strip():
        return _err("workflow_id is required.", "Get ids from n8n_workflows('list').")
    action = "activate" if active else "deactivate"
    text = _api("POST", f"/workflows/{workflow_id.strip()}/{action}")
    if text.startswith('{"ok": false'):
        return text
    return f"Workflow {workflow_id} {action}d.\n" + _clip(text)[:1200]


def n8n_executions(workflow_id: str = "", limit: int = 20) -> str:
    """Recent n8n executions, newest first (optionally filtered by workflow)."""
    limit = max(1, min(int(limit), 100))
    query = f"/executions?limit={limit}&includeData=false"
    if workflow_id.strip():
        query += f"&workflowId={workflow_id.strip()}"
    status_text = _api("GET", query)
    if status_text.startswith('{"ok": false'):
        return status_text
    try:
        data = json.loads(status_text)
    except json.JSONDecodeError:
        return _clip(status_text)
    items = data.get("data", [])
    rows = [{
        "id": e.get("id"),
        "workflowId": e.get("workflowId"),
        "status": e.get("status"),
        "startedAt": e.get("startedAt"),
        "stoppedAt": e.get("stoppedAt"),
    } for e in items]
    return json.dumps({"count": len(rows), "executions": rows}, indent=2)


def n8n_webhook(path: str, payload: str = "") -> str:
    """Call an n8n webhook (runs its pipeline) and return the response.

    path is what comes after /webhook, e.g. "auton-agent". payload is a JSON
    object string; it is sent as the request body. Long timeout: an agent run
    can take minutes.
    """
    path = (path or "").strip().lstrip("/")
    if not path:
        return _err("path is required.", 'Example: n8n_webhook("auton-agent", "{\\"task\\": \\"...\\"}")')
    if not _PATH_RE.match(path):
        return _err(f"invalid webhook path {path!r}",
                    "Use lowercase letters, digits, '-', '_' and '/'.")
    body = {}
    if payload.strip():
        try:
            body = json.loads(payload)
        except json.JSONDecodeError as exc:
            return _err(f"payload is not valid JSON: {exc}")
    if not isinstance(body, dict):
        return _err("payload must be a JSON object.")
    status, text = _http("POST", f"{WEBHOOK_BASE}/{path}", body,
                         timeout=WEBHOOK_TIMEOUT)
    if status == 0:
        return _err(f"webhook unreachable: {_clip(text)[:300]}",
                    "Check that the workflow is active and the path is correct.")
    if status >= 400:
        return _err(f"webhook returned HTTP {status}: {_clip(text)[:800]}",
                    "404 usually means the workflow is not active.")
    return f"HTTP {status}\n{_clip(text)}"


# --------------------------------------------------------------------------- #
# Agent pipeline template
# --------------------------------------------------------------------------- #

def _auth_header() -> dict:
    return {
        "sendHeaders": True,
        "headerParameters": {
            "parameters": [{
                "name": "Authorization",
                "value": "=Bearer {{ $env.AGENT_API_TOKEN }}",
            }],
        },
    }


def _finished_condition(status: str) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "leftValue": "={{ $json.status }}",
        "rightValue": status,
        "operator": {"type": "string", "operation": "equals",
                     "name": "filter.operator.equals"},
    }


def _pipeline_workflow(name: str, path: str) -> dict:
    """webhook -> start async job -> poll /jobs/{id} until done -> respond.

    Long agent tasks easily outlive a single HTTP request (and any sensible
    proxy timeout), so the workflow starts the job with ``?async=1`` and then
    polls. The Wait/IF pair loops every few seconds until the job reports
    ``done``, ``error`` or ``cancelled``.

    Generated instead of asking a 7B model to hand-write n8n node JSON: the
    graph is fixed and boring, only the name/path vary.
    """
    poll_seconds = 5
    return {
        "name": name,
        "settings": {"executionOrder": "v1", "executionTimeout": 3600},
        "nodes": [
            {
                "id": str(uuid.uuid4()),
                "name": "Webhook",
                "type": "n8n-nodes-base.webhook",
                "typeVersion": 2,
                "position": [-260, 0],
                "webhookId": str(uuid.uuid4()),
                "parameters": {
                    "path": path,
                    "httpMethod": "POST",
                    "responseMode": "responseNode",
                    "options": {},
                },
            },
            {
                "id": str(uuid.uuid4()),
                "name": "Start Job",
                "type": "n8n-nodes-base.httpRequest",
                "typeVersion": 4.2,
                "position": [-20, 0],
                "parameters": {
                    "method": "POST",
                    "url": ASYNC_RUN_URL,
                    **_auth_header(),
                    "sendBody": True,
                    "specifyBody": "json",
                    # Only forward the caller's task; the agent's API rejects
                    # payloads without it and ignores everything else.
                    "jsonBody": "={{ JSON.stringify({ task: ($json.body || $json).task }) }}",
                    "options": {"timeout": 30000},
                },
            },
            {
                "id": str(uuid.uuid4()),
                "name": "Wait",
                "type": "n8n-nodes-base.wait",
                "typeVersion": 1.1,
                "position": [200, 0],
                "webhookId": str(uuid.uuid4()),
                "parameters": {"amount": poll_seconds, "unit": "seconds"},
            },
            {
                "id": str(uuid.uuid4()),
                "name": "Get Job",
                "type": "n8n-nodes-base.httpRequest",
                "typeVersion": 4.2,
                "position": [420, 0],
                "parameters": {
                    "method": "GET",
                    "url": "=" + AGENT_BASE_URL + "/jobs/{{ $json.job_id }}",
                    **_auth_header(),
                    "options": {"timeout": 30000},
                },
            },
            {
                "id": str(uuid.uuid4()),
                "name": "Done?",
                "type": "n8n-nodes-base.if",
                "typeVersion": 2.2,
                "position": [640, 0],
                "parameters": {
                    "conditions": {
                        "options": {
                            "caseSensitive": True,
                            "leftValue": "",
                            "typeValidation": "loose",
                            "version": 2,
                        },
                        "conditions": [
                            _finished_condition("done"),
                            _finished_condition("error"),
                            _finished_condition("cancelled"),
                        ],
                        "combinator": "or",
                    },
                    "options": {},
                },
            },
            {
                "id": str(uuid.uuid4()),
                "name": "Respond",
                "type": "n8n-nodes-base.respondToWebhook",
                "typeVersion": 1.1,
                "position": [880, 0],
                "parameters": {
                    "respondWith": "json",
                    # done -> the agent's {final, steps, stop_reason}; error ->
                    # whatever /jobs returned, so the caller sees why.
                    "responseBody": "={{ $json.result || $json }}",
                    "options": {},
                },
            },
        ],
        "connections": {
            "Webhook": {"main": [[{"node": "Start Job", "type": "main", "index": 0}]]},
            "Start Job": {"main": [[{"node": "Wait", "type": "main", "index": 0}]]},
            "Wait": {"main": [[{"node": "Get Job", "type": "main", "index": 0}]]},
            "Get Job": {"main": [[{"node": "Done?", "type": "main", "index": 0}]]},
            "Done?": {"main": [
                [{"node": "Respond", "type": "main", "index": 0}],
                [{"node": "Wait", "type": "main", "index": 0}],
            ]},
        },
    }


def n8n_create_agent_pipeline(name: str = "Auton agent", path: str = "auton-agent",
                              activate: bool = True) -> str:
    """Create a webhook -> async Auton job -> poll -> respond pipeline in n8n.

    The workflow accepts POST /webhook/<path> with {"task": "..."}, starts the
    job with ``?async=1`` and polls ``/jobs/{id}`` every few seconds, so a slow
    7B model never hits a request timeout. It authenticates with the
    AGENT_API_TOKEN env var that n8n receives from the auton-api-token Secret.
    """
    path = (path or "").strip().lstrip("/")
    if not path or not _PATH_RE.match(path):
        return _err(f"invalid webhook path {path!r}",
                    "Use lowercase letters, digits, '-', '_' and '/'.")
    body = _pipeline_workflow(name.strip() or "Auton agent", path)
    text = _api("POST", "/workflows", body)
    if text.startswith('{"ok": false'):
        return text
    try:
        workflow_id = json.loads(text).get("id")
    except (json.JSONDecodeError, AttributeError):
        return _clip(text)
    result = f"Created workflow {workflow_id} ({body['name']})."
    if activate:
        activation = _api("POST", f"/workflows/{workflow_id}/activate")
        if activation.startswith('{"ok": false'):
            return result + f"\nActivation failed: {activation}"
        result += " Activated."
    return (result + f"\nCall it with n8n_webhook('{path}', "
            '\'{"task": "..."}\').')


TOOLS = [n8n_workflows, n8n_activate, n8n_executions, n8n_webhook,
         n8n_create_agent_pipeline]


def register(mcp) -> None:
    """Attach every tool in this module to an MCPServer instance."""
    for fn in TOOLS:
        mcp.tool()(fn)
