"""`/tasks` API: the operator view of both queues, and manual repair submits.

The endpoint must never be a second, weaker path into the cluster: it validates
the same fields a finding has, writes an audit *event* for the submission, and
leaves the actual mutation to the audit-gated worker.
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

import agent_server
import agent_service
import ops_queue
import task_store


def _request(base: str, path: str, payload: dict | None = None,
             method: str | None = None) -> tuple[int, dict]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"{base}{path}", data=data,
        method=method or ("POST" if data else "GET"),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode() or "{}")


@pytest.fixture()
def server(monkeypatch):
    store = task_store.TaskStore(ops_queue.MemoryBackend(), lease=30,
                                 max_attempts=2, backoff=[0, 0], max_queue=50)
    monkeypatch.setattr(agent_service, "QUEUE_KIND", "memory")
    monkeypatch.setattr(agent_service, "AGENT_STORE", store)
    httpd = agent_service.ThreadingHTTPServer(
        ("127.0.0.1", 0), agent_service.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", store
    finally:
        httpd.shutdown()
        httpd.server_close()


def _submit(base: str, **overrides) -> tuple[int, dict]:
    payload = {"queue": "repair", "kind": "Pod", "name": "api-1",
               "cause": "oom", "namespace": "auton-apps"}
    payload.update(overrides)
    return _request(base, "/tasks", payload)


# --------------------------------------------------------------------------- #
# Read side
# --------------------------------------------------------------------------- #

def test_empty_queue_reports_stats_and_the_manual_causes(server):
    base, _ = server
    status, payload = _request(base, "/tasks")
    assert status == 200
    assert payload["count"] == 0
    assert payload["tasks"] == []
    assert payload["stats"]["backend"] == "memory"
    # The UI form needs the allowed causes, and they must match the controller.
    assert payload["causes"]["oom"] == "critical"
    assert payload["causes"]["image"] == "warning"

    status, stats = _request(base, "/tasks/stats")
    assert status == 200
    assert stats["queue_depth"]["repair_normal"] == 0
    assert stats["active"] == {"repair": 0, "agent": 0}


def test_list_filters_by_queue_and_status(server):
    base, store = server
    _submit(base, cause="oom")
    _submit(base, cause="crashloop")
    agent_service.submit("look at the build") if agent_service.redis_mode() \
        else store.submit("agent", {"task": "look at the build"})

    status, payload = _request(base, "/tasks?queue=repair&status=queued")
    assert status == 200
    assert payload["count"] == 2
    assert {task["cause"] for task in payload["tasks"]} == {"oom", "crashloop"}

    status, payload = _request(base, "/tasks?queue=agent")
    assert status == 200
    assert payload["count"] == 1
    assert payload["tasks"][0]["queue"] == "agent"


def test_get_returns_the_record_with_logs(server):
    base, store = server
    task_id = _submit(base)[1]["task_id"]
    store.append_log(task_id, "[worker] raised memory 256Mi -> 320Mi")

    status, payload = _request(base, f"/tasks/{task_id}")
    assert status == 200
    assert payload["id"] == task_id
    assert payload["status"] == "queued"
    assert payload["resource"] == "pod/api-1"
    assert "raised memory" in payload["logs"][-1]

    assert _request(base, "/tasks/tsk-00000000")[0] == 404
    assert _request(base, "/tasks/nonsense")[0] == 404


# --------------------------------------------------------------------------- #
# Submit side
# --------------------------------------------------------------------------- #

def test_submit_repair_queues_an_audited_manual_task(server):
    base, store = server
    status, payload = _submit(base)
    assert status == 202
    assert payload["queue"] == "repair"
    assert payload["resource"] == "pod/api-1"

    record = store.get(payload["task_id"])
    assert record["status"] == "queued"
    assert record["source"] == "manual"
    assert record["force"] is True          # a human click is not held by a cooldown
    assert record["priority"] == "critical"  # oom is critical
    assert record["severity"] == "critical"
    assert record["observation"].startswith("manual repair request")
    # The submission is recorded, but the kubectl mutation is still the worker's
    # audit-gated job.
    assert payload["audit_ids"]


def test_submit_rejects_unknown_fields_with_the_allowed_values(server):
    base, store = server
    status, payload = _submit(base, cause="exploded")
    assert status == 400
    assert "cause" in payload["error"]
    assert "oom" in payload["causes"]

    assert _submit(base, kind="Secret")[0] == 400
    assert _submit(base, name="Not A Name")[0] == 400
    assert store.list() == []


def test_submit_refuses_a_namespace_outside_the_ops_allowlist(server):
    base, store = server
    status, payload = _submit(base, namespace="kube-system")
    assert status == 400
    assert "AGENT_OPS_NAMESPACES" in payload["error"]
    assert store.list() == []


def test_manual_submit_can_opt_out_of_force(server):
    base, store = server
    task_id = _submit(base, force=False)[1]["task_id"]
    assert store.get(task_id)["force"] is False


def test_submit_agent_job_uses_the_agent_queue(server, monkeypatch):
    base, store = server
    monkeypatch.setattr(agent_service, "QUEUE_KIND", "redis")
    status, payload = _request(base, "/tasks",
                              {"queue": "agent", "task": "explain the crash"})
    assert status == 202
    assert payload["queue"] == "agent"
    record = store.get(payload["task_id"])
    assert record["queue"] == "agent"
    assert record["task"] == "explain the crash"
    assert payload["poll"] == f"/tasks/{payload['task_id']}"


def test_submit_agent_job_requires_a_task(server):
    base, _ = server
    status, payload = _request(base, "/tasks", {"queue": "agent"})
    assert status == 400
    assert "task" in payload["error"]


def test_cancel_a_queued_task(server):
    base, store = server
    task_id = _submit(base)[1]["task_id"]
    status, payload = _request(base, f"/tasks/{task_id}/cancel", method="POST")
    assert status == 200
    assert payload["cancelled"] is True
    assert store.get(task_id)["status"] == "cancelled"

    assert _request(base, "/tasks/tsk-ffffffffffff/cancel",
                    method="POST")[0] == 404
    assert _request(base, "/tasks/nonsense/cancel", method="POST")[0] == 404


# --------------------------------------------------------------------------- #
# `task_queue` tool
# --------------------------------------------------------------------------- #

@pytest.fixture()
def tool(monkeypatch):
    store = task_store.TaskStore(ops_queue.MemoryBackend(), lease=30,
                                 max_attempts=2, backoff=[0, 0], max_queue=50)
    monkeypatch.setattr(agent_server, "TASK_STORE", store)
    return store


def test_tool_submits_and_reads_a_repair_task(tool):
    out = json.loads(agent_server.task_queue(
        action="submit", kind="Deployment", name="api", cause="oom"))
    assert out["ok"] is True
    task_id = out["task_id"]
    assert tool.get(task_id)["source"] == "agent"
    assert tool.get(task_id)["force"] is False   # the model respects a cooldown

    listing = json.loads(agent_server.task_queue(action="list"))
    assert listing["count"] == 1
    assert listing["tasks"][0]["task_id"] == task_id

    detail = json.loads(agent_server.task_queue(action="get", task_id=task_id))
    assert detail["status"] == "queued"
    assert "logs" in detail

    stats = json.loads(agent_server.task_queue(action="stats"))
    assert stats["queue_depth"]["repair_critical"] == 1


def test_tool_rejects_a_bad_cause_and_a_bad_action(tool):
    out = json.loads(agent_server.task_queue(
        action="submit", kind="Pod", name="api-1", cause="nope"))
    assert out["ok"] is False
    assert "invalid repair task" in out["error"]
    assert tool.list() == []

    out = json.loads(agent_server.task_queue(action="rm -rf"))
    assert out["ok"] is False
    assert "unknown action" in out["error"]


def test_tool_cancels_and_reports_unknown_ids(tool):
    task_id = tool.submit("repair", {"namespace": "auton-apps", "kind": "Pod",
                                     "name": "api-1", "cause": "oom"})["id"]
    out = json.loads(agent_server.task_queue(action="cancel", task_id=task_id))
    assert out["ok"] is True
    assert tool.get(task_id)["status"] == "cancelled"

    out = json.loads(agent_server.task_queue(action="get", task_id="tsk-00000000"))
    assert out["ok"] is False
    out = json.loads(agent_server.task_queue(action="get", task_id="nope"))
    assert out["ok"] is False
