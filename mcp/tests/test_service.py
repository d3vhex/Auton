"""Tests for the HTTP service and its job queue."""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

import pytest

import agent_service


def _request(method: str, url: str, body=None, timeout: float = 15,
             headers: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    hdrs = dict(headers or {})
    if data:
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


@pytest.fixture()
def server(monkeypatch):
    async def fake_run(task, *, log=lambda *_: None, should_stop=None):
        for i in range(5):
            if should_stop and should_stop():
                return {"final": "cancelled", "steps": [], "stop_reason": "cancelled",
                        "elapsed": 0.0}
            log(f"working {i}")
            time.sleep(0.02)
        return {
            "final": f"done: {task}",
            "steps": [{"tool": "bash", "arguments": {"command": "true"},
                       "output": "ok", "ms": 1}],
            "stop_reason": "final",
            "elapsed": 0.1,
        }

    monkeypatch.setattr(agent_service, "run_agent", fake_run)
    agent_service.start_worker()
    httpd = agent_service.ThreadingHTTPServer(("127.0.0.1", 0), agent_service.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def _wait_for(base: str, job_id: str, timeout: float = 10,
              headers: dict | None = None) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, payload = _request("GET", f"{base}/jobs/{job_id}", headers=headers)
        assert status == 200
        if payload["status"] in ("done", "error", "cancelled"):
            return payload
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish")


def test_health(server):
    status, payload = _request("GET", f"{server}/health")
    assert status == 200
    assert payload["status"] == "ok"
    assert "queue_depth" in payload and "model" in payload


def test_sync_run_returns_result(server):
    status, payload = _request("POST", f"{server}/run", {"task": "say hi"})
    assert status == 200
    assert payload["final"] == "done: say hi"
    assert payload["stop_reason"] == "final"
    assert payload["steps"][0]["tool"] == "bash"
    assert payload["job_id"]


def test_missing_task_and_bad_body(server):
    status, payload = _request("POST", f"{server}/run", {"nope": 1})
    assert status == 400 and "missing 'task'" in payload["error"]

    request = urllib.request.Request(
        f"{server}/run", data=b"not json", method="POST",
        headers={"Content-Type": "application/json"},
    )
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(request, timeout=5)
    assert exc.value.code == 400


def test_async_job_lifecycle_and_sse(server):
    status, payload = _request("POST", f"{server}/run?async=1", {"task": "async hi"})
    assert status == 202
    job_id = payload["job_id"]
    assert payload["poll"] == f"/jobs/{job_id}"

    done = _wait_for(server, job_id)
    assert done["status"] == "done"
    assert done["result"]["final"] == "done: async hi"
    assert any("working" in line for line in done["logs"])

    listing = _request("GET", f"{server}/jobs")[1]
    assert any(j["job_id"] == job_id for j in listing["jobs"])

    # SSE replay for a finished job terminates immediately.
    with urllib.request.urlopen(f"{server}/jobs/{job_id}/events", timeout=10) as response:
        body = response.read().decode()
    assert "event: status" in body
    assert '"status": "done"' in body


def test_cancel_queued_job(server):
    first = _request("POST", f"{server}/run?async=1", {"task": "long one"})[1]["job_id"]
    time.sleep(0.01)
    second = _request("POST", f"{server}/run?async=1", {"task": "queued one"})[1]["job_id"]

    status, payload = _request("POST", f"{server}/jobs/{second}/cancel")
    assert status == 200
    assert payload["cancelled"] is True

    second_done = _wait_for(server, second)
    assert second_done["status"] == "cancelled"
    _wait_for(server, first)


def test_unknown_job_and_route(server):
    assert _request("GET", f"{server}/jobs/nope")[0] == 404
    assert _request("POST", f"{server}/jobs/nope/cancel")[0] == 404
    assert _request("GET", f"{server}/nope")[0] == 404
    assert _request("POST", f"{server}/nope")[0] == 404


def test_queue_full_raises(monkeypatch):
    class FullQueue:
        def qsize(self):
            return 1

        def put(self, item):  # pragma: no cover - must not be reached
            raise AssertionError("should not enqueue")

    monkeypatch.setattr(agent_service, "MAX_QUEUE", 1)
    monkeypatch.setattr(agent_service, "QUEUE", FullQueue())
    with pytest.raises(agent_service.QueueFull):
        agent_service.submit("too much")


# --------------------------------------------------------------------------- #
# Bearer auth
# --------------------------------------------------------------------------- #

def _auth(token: str = "s3cret") -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def secured(monkeypatch):
    """Turn auth on for one test (conftest disables it for routing tests)."""
    monkeypatch.setattr(agent_service, "API_TOKEN", "s3cret")
    monkeypatch.setattr(agent_service, "AUTH_DISABLED", False)


def test_health_stays_open_but_run_requires_a_token(server, secured):
    assert _request("GET", f"{server}/health")[0] == 200

    status, payload = _request("POST", f"{server}/run", {"task": "hi"})
    assert status == 401 and payload["error"] == "unauthorized"
    assert "Bearer" in payload["hint"]

    status, payload = _request("POST", f"{server}/run", {"task": "hi"},
                               headers=_auth())
    assert status == 200 and payload["final"] == "done: hi"


def test_jobs_endpoints_require_a_token(server, secured):
    job_id = _request("POST", f"{server}/run?async=1", {"task": "async"},
                      headers=_auth())[1]["job_id"]
    _wait_for(server, job_id, headers=_auth())

    for method, path in (
        ("GET", f"{server}/jobs"),
        ("GET", f"{server}/jobs/{job_id}"),
        ("GET", f"{server}/jobs/{job_id}/events"),
        ("POST", f"{server}/jobs/{job_id}/cancel"),
    ):
        assert _request(method, path)[0] == 401, path
        assert _request(method, path, headers=_auth("wrong"))[0] == 401, path

    assert _request("GET", f"{server}/jobs", headers=_auth())[0] == 200


def test_missing_token_fails_closed(server, monkeypatch):
    monkeypatch.setattr(agent_service, "API_TOKEN", "")
    monkeypatch.setattr(agent_service, "AUTH_DISABLED", False)
    status, payload = _request("GET", f"{server}/jobs")
    assert status == 503 and "AGENT_API_TOKEN" in payload["error"]
    assert _request("GET", f"{server}/health")[0] == 200


def test_main_refuses_to_start_without_a_token(monkeypatch):
    monkeypatch.setattr(agent_service, "API_TOKEN", "")
    monkeypatch.setattr(agent_service, "AUTH_DISABLED", False)
    with pytest.raises(SystemExit) as exc:
        agent_service.main()
    assert exc.value.code == 1


def test_audit_endpoint_returns_records(server, audit_dir):
    import audit

    audit.begin("k8s_scale", {"replicas": 2}, incident_id="inc-test")
    audit.finish(audit.recent(1)[0]["audit_id"], result="scaled",
                 incident_id="inc-test")

    status, payload = _request("GET", f"{server}/audit?limit=10")
    assert status == 200
    assert payload["count"] == 2
    assert payload["records"][0]["action"] == "k8s_scale"

    status, payload = _request(
        "GET", f"{server}/audit?incident_id=inc-test")
    assert status == 200 and payload["count"] == 2


def test_audit_endpoint_reports_an_unavailable_log(server, monkeypatch):
    def boom(*args, **kwargs):
        raise agent_service.audit.AuditError("volume is gone")

    monkeypatch.setattr(agent_service.audit, "recent", boom)
    status, payload = _request("GET", f"{server}/audit")
    assert status == 503
    assert "volume is gone" in payload["error"]


def test_provider_endpoint_reports_local_default(server):
    status, payload = _request("GET", f"{server}/provider")
    assert status == 200
    assert payload["provider"] == "ollama"
    assert payload["local_model_enabled"] is True
    assert "api_key" not in payload


def test_provider_test_endpoint(server, monkeypatch):
    async def fake_test():
        return {"ok": True, "reply": "ok", "provider": "openai"}

    monkeypatch.setattr(agent_service.llm, "test", fake_test)
    status, payload = _request("POST", f"{server}/provider/test")
    assert status == 200 and payload["reply"] == "ok"


def test_provider_test_failure_is_a_502(server, monkeypatch):
    async def boom():
        raise agent_service.llm.LLMError("no API key")

    monkeypatch.setattr(agent_service.llm, "test", boom)
    status, payload = _request("POST", f"{server}/provider/test")
    assert status == 502 and "no API key" in payload["error"]


# --------------------------------------------------------------------------- #
# Incidents
# --------------------------------------------------------------------------- #

@pytest.fixture()
def incident_store(tmp_path, monkeypatch):
    import incidents

    store = incidents.IncidentStore(str(tmp_path / "incidents"))
    monkeypatch.setattr(agent_service, "INCIDENT_STORE", store)
    monkeypatch.setattr(agent_service, "INCIDENT_STORE_ERROR", "")
    return store


def test_incidents_list_get_and_filter(server, incident_store):
    first = incident_store.create(namespace="auton-apps", kind="Pod",
                                  name="api-1", cause="oom",
                                  observation="OOMKilled")
    second = incident_store.create(namespace="auton-apps", kind="Pod",
                                   name="api-2", cause="crashloop")
    second["created_at"] = first["created_at"] + 1
    incident_store.save(second)
    incident_store.update(first["id"], status="resolved")

    status, payload = _request("GET", f"{server}/incidents")
    assert status == 200 and payload["count"] == 2
    assert payload["incidents"][0]["id"] == second["id"]

    status, payload = _request("GET", f"{server}/incidents?status=resolved")
    assert status == 200
    assert [r["id"] for r in payload["incidents"]] == [first["id"]]

    status, payload = _request("GET", f"{server}/incidents/{first['id']}")
    assert status == 200 and payload["resource"] == "pod/api-1"
    assert payload["status"] == "resolved"

    assert _request("GET", f"{server}/incidents/inc-missing")[0] == 404


def test_incident_ingest_saves_and_validates(server, incident_store):
    record = {
        "id": "inc-abc123", "namespace": "auton-apps", "kind": "Deployment",
        "name": "api", "cause": "rollout", "status": "resolved",
        "observation": "rollout failed", "actions": [], "report": "text",
    }
    status, payload = _request("POST", f"{server}/incidents/ingest", record)
    assert status == 200 and payload["ok"] is True
    saved = incident_store.get("inc-abc123")
    assert saved["resource"] == "deployment/api"
    assert saved["ingested_at"] > 0

    status, payload = _request("POST", f"{server}/incidents/ingest",
                               {"namespace": "auton-apps"})
    assert status == 400 and "id" in payload["error"]

    status, payload = _request("POST", f"{server}/incidents/ingest",
                               {"id": "../escape", "resource": "pod/x"})
    assert status == 400 and "invalid incident id" in payload["error"]


def test_incidents_report_an_unavailable_store(server, monkeypatch):
    monkeypatch.setattr(agent_service, "INCIDENT_STORE", None)
    monkeypatch.setattr(agent_service, "INCIDENT_STORE_ERROR", "volume is gone")

    for method, path in (("GET", "/incidents"),
                         ("GET", "/incidents/inc-1"),
                         ("POST", "/incidents/ingest")):
        status, payload = _request(method, f"{server}{path}",
                                   {"id": "inc-1"} if method == "POST" else None)
        assert status == 503, path
        assert "volume is gone" in payload["error"]


def test_incidents_endpoints_require_a_token(server, secured, incident_store):
    incident_store.create(namespace="auton-apps", kind="Pod", name="api-1",
                          cause="oom")
    assert _request("GET", f"{server}/incidents")[0] == 401
    assert _request("GET", f"{server}/incidents/inc-1")[0] == 401
    status, _ = _request("POST", f"{server}/incidents/ingest",
                         {"id": "inc-1"})
    assert status == 401
    assert _request("GET", f"{server}/incidents", headers=_auth())[0] == 200
