"""Tests for the n8n REST tools (no network: _http/_api are faked)."""
from __future__ import annotations

import json

import pytest

import n8n_tools


def _err(out: str) -> dict:
    data = json.loads(out)
    assert data["ok"] is False
    return data


@pytest.fixture()
def api(monkeypatch):
    recorded = []

    def fake_api(method, path, body=None):
        recorded.append({"method": method, "path": path, "body": body})
        return json.dumps({"id": "wf1", "name": "Auton agent", "active": False})

    monkeypatch.setattr(n8n_tools, "_api", fake_api)
    monkeypatch.setattr(n8n_tools, "_api_key", lambda: "test-key")
    return recorded


def test_missing_api_key_explains_the_secret(monkeypatch, tmp_path):
    monkeypatch.delenv("N8N_API_KEY", raising=False)
    monkeypatch.setattr(n8n_tools, "KEY_FILE", str(tmp_path / "missing"))
    out = _err(n8n_tools.n8n_workflows("list"))
    assert "n8n API key" in out["error"]
    assert "n8n-api" in out["hint"]


def test_api_key_is_read_from_file(monkeypatch, tmp_path):
    key = tmp_path / "api-key"
    key.write_text("file-key\n")
    monkeypatch.delenv("N8N_API_KEY", raising=False)
    monkeypatch.setattr(n8n_tools, "KEY_FILE", str(key))
    assert n8n_tools._api_key() == "file-key"


def test_list_summarises_workflows(monkeypatch):
    payload = {"data": [{"id": "1", "name": "a", "active": True,
                         "nodes": [1, 2, 3], "updatedAt": "now"}]}

    def fake_api(method, path, body=None):
        assert method == "GET" and path.startswith("/workflows")
        return json.dumps(payload)

    monkeypatch.setattr(n8n_tools, "_api", fake_api)
    out = json.loads(n8n_tools.n8n_workflows("list"))
    assert out["count"] == 1
    assert out["workflows"][0] == {"id": "1", "name": "a", "active": True,
                                   "updatedAt": "now"}


def test_create_rejects_bad_json_before_calling_the_api(api):
    out = _err(n8n_tools.n8n_workflows("create", payload="{not json"))
    assert "not valid JSON" in out["error"]
    assert api == []


def test_activate_and_deactivate(api):
    assert "activated" in n8n_tools.n8n_activate("wf1", True)
    assert api[-1] == {"method": "POST", "path": "/workflows/wf1/activate",
                       "body": None}
    n8n_tools.n8n_activate("wf1", False)
    assert api[-1]["path"].endswith("/deactivate")


def test_pipeline_template_shape():
    wf = n8n_tools._pipeline_workflow("Auton agent", "auton-agent")
    nodes = {n["name"]: n for n in wf["nodes"]}
    assert set(nodes) == {"Webhook", "Start Job", "Wait", "Get Job", "Done?",
                          "Respond"}
    assert nodes["Webhook"]["parameters"]["responseMode"] == "responseNode"

    start = nodes["Start Job"]["parameters"]
    assert start["url"] == n8n_tools.AGENT_RUN_URL + "?async=1"
    assert start["headerParameters"]["parameters"][0]["value"] == \
        "=Bearer {{ $env.AGENT_API_TOKEN }}"

    poll = nodes["Get Job"]["parameters"]
    assert poll["method"] == "GET"
    assert poll["url"].endswith("/jobs/{{ $json.job_id }}")
    assert poll["headerParameters"]["parameters"][0]["value"] == \
        "=Bearer {{ $env.AGENT_API_TOKEN }}"

    wait = nodes["Wait"]["parameters"]
    assert wait["unit"] == "seconds" and wait["amount"] >= 1
    done = nodes["Done?"]
    assert done["typeVersion"] >= 2
    statuses = {c["rightValue"] for c in
                done["parameters"]["conditions"]["conditions"]}
    assert statuses == {"done", "error", "cancelled"}
    assert nodes["Respond"]["parameters"]["respondWith"] == "json"
    assert wf["settings"]["executionTimeout"] >= 900

    conns = wf["connections"]
    assert conns["Webhook"]["main"][0][0]["node"] == "Start Job"
    assert conns["Start Job"]["main"][0][0]["node"] == "Wait"
    assert conns["Wait"]["main"][0][0]["node"] == "Get Job"
    assert conns["Get Job"]["main"][0][0]["node"] == "Done?"
    assert conns["Done?"]["main"][0][0]["node"] == "Respond"
    # The false branch loops back to the polling wait.
    assert conns["Done?"]["main"][1][0]["node"] == "Wait"


def test_create_pipeline_creates_then_activates(api):
    out = n8n_tools.n8n_create_agent_pipeline("My pipeline", "my-path")
    assert "Created workflow wf1" in out and "Activated" in out
    assert api[0]["method"] == "POST" and api[0]["path"] == "/workflows"
    assert api[0]["body"]["name"] == "My pipeline"
    assert api[1]["path"] == "/workflows/wf1/activate"


def test_pipeline_rejects_invalid_path(api):
    assert _err(n8n_tools.n8n_create_agent_pipeline(path="../etc/passwd"))
    assert api == []


def test_webhook_posts_json(monkeypatch):
    recorded = {}

    def fake_http(method, url, body=None, timeout=None):
        recorded.update(method=method, url=url, body=body, timeout=timeout)
        return 200, '{"final": "done"}'

    monkeypatch.setattr(n8n_tools, "_http", fake_http)
    out = n8n_tools.n8n_webhook("auton-agent", '{"task": "hi"}')
    assert "HTTP 200" in out
    assert recorded["url"] == f"{n8n_tools.WEBHOOK_BASE}/auton-agent"
    assert recorded["body"] == {"task": "hi"}
    assert recorded["timeout"] == n8n_tools.WEBHOOK_TIMEOUT


def test_webhook_errors_and_bad_payload(monkeypatch):
    monkeypatch.setattr(n8n_tools, "_http",
                        lambda *a, **k: (404, '{"message":"not registered"}'))
    out = _err(n8n_tools.n8n_webhook("nope", "{}"))
    assert "404" in out["error"]
    assert "not active" in out["hint"]
    assert _err(n8n_tools.n8n_webhook("auton-agent", "[1, 2]"))
    assert _err(n8n_tools.n8n_webhook("", "{}"))


def test_executions_are_summarised(monkeypatch):
    payload = {"data": [{"id": 1, "workflowId": "wf1", "status": "success",
                         "startedAt": "t0", "stoppedAt": "t1", "data": "huge"}]}
    monkeypatch.setattr(n8n_tools, "_api", lambda *a, **k: json.dumps(payload))
    out = json.loads(n8n_tools.n8n_executions("wf1", limit=999))
    assert out["executions"][0] == {"id": 1, "workflowId": "wf1",
                                    "status": "success", "startedAt": "t0",
                                    "stoppedAt": "t1"}


def test_workflow_delete_is_summarised(api):
    out = json.loads(n8n_tools.n8n_workflows("delete", workflow_id="wf1"))
    assert out == {"ok": True, "deleted": "wf1"}
    assert api[-1] == {"method": "DELETE", "path": "/workflows/wf1",
                       "body": None}


def test_workflow_delete_surfaces_api_errors(monkeypatch):
    monkeypatch.setattr(n8n_tools, "_api",
                        lambda *a, **k: '{"ok": false, "error": "boom"}')
    assert "boom" in n8n_tools.n8n_workflows("delete", workflow_id="wf1")
