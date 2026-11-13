"""End-to-end tests for the incident pipeline (detect → diagnose → repair).

These exercise the path a real repair takes in the cluster: the worker
collects evidence, classifies the root cause, walks the incident through the
state machine and records the outcome.
"""
from __future__ import annotations

import pytest

import incidents
import ops_controller
import ops_queue
import task_store


def _evidence_document(cause: str = "oom") -> dict:
    """The structured evidence the collector would produce."""
    terminated = cause == "oom"
    return {
        "resource": {"kind": "Pod", "name": "api-1", "namespace": "auton-apps",
                     "uid": "uid-abc"},
        "problem": "OOMKilled" if terminated else "CrashLoopBackOff",
        "phase": "Running",
        "restart_count": 4 if terminated else 12,
        "container": {
            "name": "api",
            "restart_count": 4 if terminated else 12,
            "last_termination_reason": "OOMKilled" if terminated else "Error",
            "exit_code": 137 if terminated else 1,
            "state": "waiting",
            "ready": False,
            "image": "app:1",
            "resources": {"memory_request": "256Mi", "memory_limit": "512Mi",
                          "cpu_request": "100m", "cpu_limit": "500m"},
            "probes": {"liveness": {"configured": False},
                       "readiness": {"configured": False},
                       "startup": {"configured": False}},
            "image_pull_secret_configured": False,
        },
        "resources": {"memory_request": "256Mi", "memory_limit": "512Mi",
                      "cpu_request": "100m", "cpu_limit": "500m"},
        "owner": {"kind": "Deployment", "name": "api"},
        "probes": {},
        "node": {"name": "node-1", "available": True, "allocatable": {},
                 "capacity": {}, "conditions": [], "pressure": {}},
        "node_metrics": {},
        "events": [],
        "logs": "",
        "previous_logs": "",
        "deployment": {},
        "evidence": {"pod_available": True, "logs_available": True,
                     "previous_logs_available": False,
                     "events_available": True, "node_available": True,
                     "node_metrics_available": False,
                     "owner_available": True},
        "errors": [],
    }


@pytest.fixture()
def env(monkeypatch, tmp_path):
    incident_store = incidents.IncidentStore(str(tmp_path / "incidents"))
    monkeypatch.setattr(ops_controller, "STORE", incident_store)
    monkeypatch.setattr(ops_controller, "_diagnose", lambda item, ns: "text")
    monkeypatch.setattr(ops_controller, "_structured_evidence",
                        lambda item, ns: _evidence_document(item["cause"]))
    monkeypatch.setattr(ops_controller, "DRY_RUN", False)
    monkeypatch.setattr(ops_controller, "MAX_ACTIONS_PER_HOUR", 6)
    monkeypatch.setattr(ops_controller, "COOLDOWN", 1800)
    delivered: list[dict] = []
    monkeypatch.setattr(ops_controller, "notify",
                        lambda record: delivered.append(record) or record)
    monkeypatch.setattr(ops_controller, "_run_playbook",
                        lambda *a, **k: {"action": "oom_bump_memory", "ok": True,
                                         "detail": "raised 512Mi -> 640Mi",
                                         "verification": "deployment/api ready=1/1",
                                         "needs_human": False})
    return type("Env", (), {
        "incidents": incident_store,
        "queue": task_store.TaskStore(ops_queue.MemoryBackend(), lease=30,
                                      max_attempts=2, backoff=[0, 0],
                                      max_queue=50),
        "delivered": delivered,
    })


def _submit(env, cause="oom"):
    item = ops_controller._finding("Pod", "api-1", cause, "critical",
                                   f"pod/api-1 reports {cause}")
    return ops_controller._submit_finding(env.queue, item, "auton-apps")


def _execute(env):
    claim = env.queue.claim("repair", "worker-1")[0]
    return ops_controller.execute_task(env.queue, claim)


# --------------------------------------------------------------------------- #

def test_a_repair_records_evidence_diagnosis_and_timeline(env):
    task = _submit(env)
    result = _execute(env)
    assert result["task"] == "resolved"

    stored = env.queue.get(task["id"])
    incident = env.incidents.get(stored["incident_id"])

    # The evidence is attached in its structured form.
    assert incident["evidence_structured"]["resource"]["uid"] == "uid-abc"
    assert incident["evidence_structured"]["container"]["exit_code"] == 137

    # The root cause was classified, not guessed.
    assert incident["diagnosis"]["root_cause"] == "OOMKilled"
    assert incident["diagnosis"]["recommended_action"] == "INCREASE_MEMORY_LIMIT"
    assert incident["diagnosis"]["requires_human"] is False
    assert incident["type"] == "OOMKilled"
    assert incident["resource_uid"] == "uid-abc"

    # The incident walked the pipeline.
    states = [entry["to"] for entry in incident["timeline"]]
    for expected in ("TRIAGING", "DIAGNOSING", "PLANNING", "POLICY_CHECK",
                     "QUEUED", "EXECUTING", "VERIFYING", "RESOLVED"):
        assert expected in states, f"{expected} missing from {states}"

    # And it ended resolved with a resolution record.
    assert incident["status"] == "resolved"
    assert incident["state"] == "RESOLVED"
    assert incident["resolution"]["status"] == "resolved"
    assert incident["resolution"]["action"] == "oom_bump_memory"


def test_a_report_only_cause_ends_in_needs_human(env, monkeypatch):
    monkeypatch.setattr(ops_controller, "_run_playbook",
                        lambda *a, **k: pytest.fail("report-only must not repair"))
    task = _submit(env, cause="image")
    result = _execute(env)

    assert result["task"] == "needs_human"
    stored = env.queue.get(task["id"])
    incident = env.incidents.get(stored["incident_id"])
    assert incident["status"] == "needs_human"
    assert incident["state"] == "NEEDS_HUMAN"
    assert incident["needs_human"] is True
    assert incident["actions"][0]["action"] == "report_only"
    # Even a report-only incident carries its diagnosis.
    assert incident["diagnosis"]["root_cause"]


def test_a_dry_run_records_the_diagnosis_without_changing_anything(env,
                                                                    monkeypatch):
    monkeypatch.setattr(ops_controller, "DRY_RUN", True)
    monkeypatch.setattr(ops_controller, "_run_playbook",
                        lambda *a, **k: pytest.fail("dry run must not repair"))
    task = _submit(env)
    result = _execute(env)

    assert result["task"] == "skipped"
    stored = env.queue.get(task["id"])
    incident = env.incidents.get(stored["incident_id"])
    assert incident["status"] == "detected"
    assert incident["diagnosis"]["root_cause"] == "OOMKilled"
    assert incident["actions"][0]["action"] == "dry_run"


def test_a_retry_reuses_the_same_incident_and_reopens_it(env, monkeypatch):
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("kubectl timed out")
        return {"action": "oom_bump_memory", "ok": True, "detail": "raised",
                "verification": "ready=1/1", "needs_human": False}

    monkeypatch.setattr(ops_controller, "_run_playbook", flaky)
    task = _submit(env)

    first = _execute(env)
    assert first["task"] == "queued"
    incident_id = env.queue.get(task["id"])["incident_id"]
    assert env.incidents.get(incident_id)["status"] == "failed"

    second = _execute(env)
    assert second["task"] == "resolved"
    assert env.queue.get(task["id"])["incident_id"] == incident_id
    incident = env.incidents.get(incident_id)
    assert incident["status"] == "resolved"
    # The reopen is visible in the timeline.
    notes = [entry["note"] for entry in incident["timeline"]]
    assert any("reopened" in note for note in notes)
    # Both attempts are on the incident.
    assert [a["action"] for a in incident["actions"]] == [
        "playbook_error", "oom_bump_memory"]


def test_the_incident_carries_the_fingerprint(env):
    task = _submit(env)
    _execute(env)
    incident_id = env.queue.get(task["id"])["incident_id"]
    incident = env.incidents.get(incident_id)
    expected = incidents.fingerprint("Pod", "auton-apps", "api-1", "oom")
    assert incident["fingerprint"] == expected
    assert incident["cluster_id"] == "default"
    assert incident["occurrence_count"] == 1
