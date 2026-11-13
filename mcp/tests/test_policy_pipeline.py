"""End-to-end tests for the policy-gated repair pipeline.

detect → diagnose → plan → policy check → (auto | approval | deny | observe)

These are the paths a real repair takes in the cluster, with the Kubernetes
calls mocked out.
"""
from __future__ import annotations

import json

import pytest

import approvals
import diagnosis
import incidents
import ops_controller
import ops_queue
import planner
import policy
import task_store


def _evidence_document(cause: str = "oom") -> dict:
    terminated = cause == "oom"
    return {
        "resource": {"kind": "Pod", "name": "api-1", "namespace": "auton-apps",
                     "uid": "uid-abc", "labels": {"app": "api"}},
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
                     "events_available": True, "node_available": False,
                     "node_metrics_available": False,
                     "owner_available": True},
        "errors": [],
    }


@pytest.fixture()
def env(monkeypatch, tmp_path):
    incident_store = incidents.IncidentStore(str(tmp_path / "incidents"))
    approval_store = approvals.ApprovalStore(str(tmp_path / "approvals"))
    policy_dir = tmp_path / "policy"
    policy_dir.mkdir()
    monkeypatch.setenv("AGENT_POLICY_DIR", str(policy_dir))
    monkeypatch.setattr(ops_controller, "STORE", incident_store)
    monkeypatch.setattr(ops_controller, "APPROVALS", approval_store)
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
        "approvals": approval_store,
        "policy_dir": policy_dir,
        "queue": task_store.TaskStore(ops_queue.MemoryBackend(), lease=30,
                                      max_attempts=2, backoff=[0, 0],
                                      max_queue=50),
        "delivered": delivered,
    })


def _write_policy(env, document: dict) -> None:
    (env.policy_dir / "policy.json").write_text(json.dumps(document),
                                                 encoding="utf-8")


def _submit(env, cause="oom"):
    item = ops_controller._finding("Pod", "api-1", cause, "critical",
                                   f"pod/api-1 reports {cause}")
    return ops_controller._submit_finding(env.queue, item, "auton-apps")


def _execute(env):
    claim = env.queue.claim("repair", "worker-1")[0]
    return ops_controller.execute_task(env.queue, claim)


# --------------------------------------------------------------------------- #
# The default policy: today's behaviour is unchanged
# --------------------------------------------------------------------------- #

def test_the_default_policy_still_repairs_automatically(env):
    task = _submit(env)
    result = _execute(env)
    assert result["task"] == "resolved"
    incident = env.incidents.get(env.queue.get(task["id"])["incident_id"])
    assert incident["status"] == "resolved"
    assert incident["policy"]["action"] == "auto"
    assert incident["repair_plan"]["tier"] == 2


def test_the_incident_records_the_plan_and_the_decision(env):
    task = _submit(env)
    _execute(env)
    incident = env.incidents.get(env.queue.get(task["id"])["incident_id"])
    plan = incident["repair_plan"]
    assert plan["action"] == diagnosis.INCREASE_MEMORY_LIMIT
    assert plan["playbook"] == "oom_bump_memory"
    assert plan["verification"]
    assert incident["policy"]["action"] == "auto"
    assert incident["policy"]["tier"] == 2


def test_the_timeline_still_walks_the_whole_pipeline(env):
    task = _submit(env)
    _execute(env)
    incident = env.incidents.get(env.queue.get(task["id"])["incident_id"])
    states = [entry["to"] for entry in incident["timeline"]]
    for expected in ("TRIAGING", "DIAGNOSING", "PLANNING", "POLICY_CHECK",
                     "QUEUED", "EXECUTING", "VERIFYING", "RESOLVED"):
        assert expected in states, f"{expected} missing from {states}"


# --------------------------------------------------------------------------- #
# observe-only
# --------------------------------------------------------------------------- #

def test_observe_only_records_the_plan_without_changing_anything(env,
                                                                 monkeypatch):
    _write_policy(env, {"mode": "observe-only"})
    monkeypatch.setattr(ops_controller, "_run_playbook",
                        lambda *a, **k: pytest.fail("observe-only must not "
                                                    "repair"))
    task = _submit(env)
    result = _execute(env)

    assert result["task"] == "skipped"
    incident = env.incidents.get(env.queue.get(task["id"])["incident_id"])
    assert incident["status"] == "suppressed"
    assert incident["state"] == "SUPPRESSED"
    # The plan and the diagnosis are still there: this is shadow mode.
    assert incident["repair_plan"]["action"] == diagnosis.INCREASE_MEMORY_LIMIT
    assert incident["diagnosis"]["root_cause"] == "OOMKilled"
    assert incident["policy"]["action"] == "observe"


# --------------------------------------------------------------------------- #
# disabled
# --------------------------------------------------------------------------- #

def test_disabled_refuses_the_repair(env, monkeypatch):
    _write_policy(env, {"mode": "disabled"})
    monkeypatch.setattr(ops_controller, "_run_playbook",
                        lambda *a, **k: pytest.fail("disabled must not repair"))
    task = _submit(env)
    result = _execute(env)

    assert result["task"] == "needs_human"
    incident = env.incidents.get(env.queue.get(task["id"])["incident_id"])
    assert incident["status"] == "needs_human"
    assert incident["policy"]["action"] == "deny"
    assert incident["actions"][0]["action"] == "denied"


# --------------------------------------------------------------------------- #
# approval-required
# --------------------------------------------------------------------------- #

def test_a_tier_that_needs_approval_is_parked(env, monkeypatch):
    _write_policy(env, {"mode": "safe-auto", "auto_approve_tiers": [0, 1],
                         "approval_tiers": [2, 3]})
    monkeypatch.setattr(ops_controller, "_run_playbook",
                        lambda *a, **k: pytest.fail("nothing may run before "
                                                    "the approval"))
    task = _submit(env)
    result = _execute(env)

    assert result["task"] == "awaiting_approval"
    approval_id = result["approval_id"]

    # The incident waits, and the approval request exists.
    incident = env.incidents.get(env.queue.get(task["id"])["incident_id"])
    assert incident["status"] == "awaiting_approval"
    assert incident["state"] == "AWAITING_APPROVAL"
    assert incident["approval_id"] == approval_id
    assert incident["needs_human"] is True

    request = env.approvals.get(approval_id)
    assert request["status"] == "pending"
    assert request["incident_id"] == incident["id"]
    assert request["plan"]["action"] == diagnosis.INCREASE_MEMORY_LIMIT
    assert request["policy"]["action"] == "require_approval"


def test_the_timeline_shows_the_waiting_state(env):
    _write_policy(env, {"mode": "safe-auto", "auto_approve_tiers": [0, 1],
                         "approval_tiers": [2, 3]})
    task = _submit(env)
    _execute(env)
    incident = env.incidents.get(env.queue.get(task["id"])["incident_id"])
    states = [entry["to"] for entry in incident["timeline"]]
    assert "POLICY_CHECK" in states
    assert "AWAITING_APPROVAL" in states
    assert "EXECUTING" not in states


def test_approving_the_request_lets_the_repair_run(env, monkeypatch):
    _write_policy(env, {"mode": "safe-auto", "auto_approve_tiers": [0, 1],
                         "approval_tiers": [2, 3]})
    task = _submit(env)
    parked = _execute(env)
    approval_id = parked["approval_id"]

    # A human approves.
    request = env.approvals.decide(approval_id, approve=True, by="alice",
                                   comment="safe to bump")
    assert request["status"] == "approved"

    # The approved plan becomes a new task that carries the same incident.
    plan = request["plan"]
    resumed = env.queue.submit("repair", {
        "namespace": plan["namespace"],
        "kind": plan["kind"],
        "name": plan["name"],
        "cause": plan["cause"],
        "severity": "critical",
        "observation": "approved repair plan",
        "container": (plan.get("params") or {}).get("container", ""),
        "resource": plan["resource"],
        "source": "approval",
        "incident_id": request["incident_id"],
        "approval_id": approval_id,
        "force": True,
    }, priority="critical", actor="alice")

    result = _execute(env)
    assert result["task"] == "resolved"

    # The same incident continued.
    incident = env.incidents.get(request["incident_id"])
    assert incident["status"] == "resolved"
    assert incident["approval_id"] == approval_id
    assert incident["policy"]["action"] == "auto"
    assert "approved by alice" in incident["policy"]["reason"]

    # The approval request is closed.
    assert env.approvals.get(approval_id)["status"] == "executed"
    assert resumed["id"]


def test_a_rejected_request_never_repairs(env, monkeypatch):
    _write_policy(env, {"mode": "safe-auto", "auto_approve_tiers": [0, 1],
                         "approval_tiers": [2, 3]})
    monkeypatch.setattr(ops_controller, "_run_playbook",
                        lambda *a, **k: pytest.fail("a rejected plan must not "
                                                    "repair"))
    task = _submit(env)
    parked = _execute(env)
    request = env.approvals.decide(parked["approval_id"], approve=False,
                                   by="bob", comment="not now")
    assert request["status"] == "rejected"
    assert env.approvals.pending() == []


# --------------------------------------------------------------------------- #
# Rules
# --------------------------------------------------------------------------- #

def test_a_rule_can_deny_one_cause(env, monkeypatch):
    _write_policy(env, {"mode": "safe-auto",
                         "rules": [{"match": {"cause": "oom"},
                                    "action": "deny"}]})
    monkeypatch.setattr(ops_controller, "_run_playbook",
                        lambda *a, **k: pytest.fail("a denied cause must not "
                                                    "repair"))
    task = _submit(env)
    result = _execute(env)
    assert result["task"] == "needs_human"
    incident = env.incidents.get(env.queue.get(task["id"])["incident_id"])
    assert incident["policy"]["action"] == "deny"
    assert incident["policy"]["rule"] == "rule #1"


def test_a_protected_label_stops_the_repair(env, monkeypatch):
    _write_policy(env, {"mode": "safe-auto", "protected_labels": [
        {"key": "auton.io/protect", "value": "true"}]})
    monkeypatch.setattr(ops_controller, "_run_playbook",
                        lambda *a, **k: pytest.fail("a protected resource must "
                                                    "not be repaired"))
    monkeypatch.setattr(ops_controller, "_structured_evidence",
                        lambda item, ns: _evidence_document(item["cause"]))
    document = _evidence_document()
    document["resource"]["labels"] = {"auton.io/protect": "true"}
    monkeypatch.setattr(ops_controller, "_structured_evidence",
                        lambda item, ns: document)
    task = _submit(env)
    result = _execute(env)
    assert result["task"] == "needs_human"
    incident = env.incidents.get(env.queue.get(task["id"])["incident_id"])
    assert incident["policy"]["action"] == "deny"
    assert "protect" in incident["policy"]["reason"]


# --------------------------------------------------------------------------- #
# Report-only and dry run keep working
# --------------------------------------------------------------------------- #

def test_a_report_only_cause_still_needs_a_human(env):
    task = _submit(env, cause="image")
    result = _execute(env)
    assert result["task"] == "needs_human"
    incident = env.incidents.get(env.queue.get(task["id"])["incident_id"])
    assert incident["actions"][0]["action"] == "report_only"
    assert incident["diagnosis"]["root_cause"]


def test_a_dry_run_records_the_plan_without_changing_anything(env,
                                                               monkeypatch):
    monkeypatch.setattr(ops_controller, "DRY_RUN", True)
    monkeypatch.setattr(ops_controller, "_run_playbook",
                        lambda *a, **k: pytest.fail("dry run must not repair"))
    task = _submit(env)
    result = _execute(env)
    assert result["task"] == "skipped"
    incident = env.incidents.get(env.queue.get(task["id"])["incident_id"])
    assert incident["status"] == "detected"
    assert incident["actions"][0]["action"] == "dry_run"
