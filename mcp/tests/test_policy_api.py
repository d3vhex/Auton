"""Tests for the policy and approval HTTP endpoints."""
from __future__ import annotations

import json
import threading

import pytest

import agent_service
import approvals


def _request(method: str, url: str, body=None, timeout: float = 15,
              headers: dict | None = None):
    import urllib.error
    import urllib.request

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
def server(monkeypatch, tmp_path):
    """The HTTP service with an isolated incident + approval store."""
    import incidents

    monkeypatch.setenv("AGENT_POLICY_DIR", str(tmp_path / "policy"))
    (tmp_path / "policy").mkdir()
    monkeypatch.setattr(agent_service, "INCIDENT_STORE",
                        incidents.IncidentStore(str(tmp_path / "incidents")))
    monkeypatch.setattr(agent_service, "APPROVAL_STORE",
                        approvals.ApprovalStore(str(tmp_path / "approvals")))

    async def fake_run(task, *, log=lambda *_: None, should_stop=None):
        return {"final": "ok", "steps": [], "stop_reason": "final",
                "elapsed": 0.0}

    monkeypatch.setattr(agent_service, "run_agent", fake_run)
    agent_service.start_worker()
    httpd = agent_service.ThreadingHTTPServer(("127.0.0.1", 0),
                                               agent_service.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def _plan(**overrides) -> dict:
    plan = {"plan_id": "plan-1", "namespace": "auton-apps", "kind": "Pod",
            "name": "api-1", "resource": "pod/api-1", "cause": "oom",
            "action": "INCREASE_MEMORY_LIMIT", "tier": 2,
            "params": {"container": "hog"}}
    plan.update(overrides)
    return plan


def _approval(server, **overrides) -> dict:
    store = agent_service.APPROVAL_STORE
    return store.create(plan=_plan(), policy={"action": "require_approval",
                                               "tier": 2},
                        incident_id=overrides.pop("incident_id",
                                                 "inc-1a2b3c4d5e"),
                        timeout=600)


# --------------------------------------------------------------------------- #
# GET /policy
# --------------------------------------------------------------------------- #

def test_the_policy_endpoint_reports_the_effective_policy(server):
    status, payload = _request("GET", f"{server}/policy")
    assert status == 200
    assert payload["ok"] is True
    assert payload["policy"]["mode"] in payload["modes"]
    assert payload["tiers"] == [0, 1, 2, 3]


def test_the_policy_endpoint_reports_a_broken_policy(server, tmp_path):
    (tmp_path / "policy" / "policy.json").write_text("{not json",
                                                     encoding="utf-8")
    status, payload = _request("GET", f"{server}/policy")
    assert status == 200
    assert payload["ok"] is False
    assert "JSON" in payload["error"]


# --------------------------------------------------------------------------- #
# GET /approvals
# --------------------------------------------------------------------------- #

def test_the_approvals_endpoint_lists_the_requests(server):
    _approval(server)
    status, payload = _request("GET", f"{server}/approvals")
    assert status == 200
    assert payload["count"] == 1
    assert payload["approvals"][0]["status"] == "pending"


def test_the_approvals_endpoint_can_be_filtered(server):
    _approval(server)
    approved = _approval(server)
    agent_service.APPROVAL_STORE.decide(approved["id"], approve=True)
    status, payload = _request("GET", f"{server}/approvals?status=pending")
    assert status == 200
    assert payload["count"] == 1


def test_one_approval_can_be_fetched(server):
    created = _approval(server)
    status, payload = _request("GET", f"{server}/approvals/{created['id']}")
    assert status == 200
    assert payload["id"] == created["id"]


def test_an_unknown_approval_is_a_404(server):
    status, _ = _request("GET", f"{server}/approvals/apr-00000000")
    assert status == 404


# --------------------------------------------------------------------------- #
# POST /approvals/{id}/approve
# --------------------------------------------------------------------------- #

def test_approving_an_approval_marks_it_approved(server, monkeypatch):
    enqueued: list[dict] = []

    class FakeStore:
        def submit(self, queue, fields, **kwargs):
            enqueued.append(dict(fields))
            return {"id": "tsk-1234", "status": "queued"}

    monkeypatch.setattr(agent_service, "tasks_store", lambda: FakeStore())
    created = _approval(server)
    status, payload = _request("POST", f"{server}/approvals/{created['id']}"
                                      "/approve", {"by": "alice"})
    assert status == 200
    assert payload["ok"] is True
    assert payload["approval"]["status"] == "approved"
    # The approved plan became a repair task.
    assert enqueued
    assert enqueued[0]["approval_id"] == created["id"]
    assert enqueued[0]["incident_id"] == "inc-1a2b3c4d5e"
    assert enqueued[0]["force"] is True


def test_approving_twice_is_a_conflict(server):
    created = _approval(server)
    _request("POST", f"{server}/approvals/{created['id']}/approve", {})
    status, payload = _request("POST", f"{server}/approvals/{created['id']}"
                                      "/approve", {})
    assert status == 409
    assert "already" in payload["error"]


def test_rejecting_an_approval_closes_the_incident(server):
    import incidents

    store = agent_service.INCIDENT_STORE
    incident = store.create(namespace="auton-apps", kind="Pod", name="api-1",
                            cause="oom", observation="x", severity="critical")
    created = _approval(server, incident_id=incident["id"])
    status, payload = _request("POST", f"{server}/approvals/{created['id']}"
                                      "/reject", {"by": "bob",
                                                  "comment": "not now"})
    assert status == 200
    assert payload["approval"]["status"] == "rejected"
    closed = store.get(incident["id"])
    assert closed["status"] == "needs_human"
    assert closed["actions"][-1]["action"] == "rejected"


def test_an_unknown_approval_cannot_be_approved(server):
    status, _ = _request("POST", f"{server}/approvals/apr-00000000/approve", {})
    assert status == 404


def test_an_unknown_action_is_a_404(server):
    created = _approval(server)
    status, _ = _request("POST", f"{server}/approvals/{created['id']}/burn",
                         {})
    assert status == 404


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #

def test_the_policy_and_approval_endpoints_require_a_token(server, monkeypatch):
    monkeypatch.setattr(agent_service, "API_TOKEN", "secret-token")
    monkeypatch.setattr(agent_service, "AUTH_DISABLED", False)
    for method, path in (("GET", "/policy"), ("GET", "/approvals"),
                         ("POST", "/approvals/apr-00000000/approve")):
        status, _ = _request(method, f"{server}{path}")
        assert status == 401, path
