"""Scanner → queue → worker split, exercised without a cluster.

The scanner is read-only and the worker owns every mutation, so the tests check
the contract between them: one task per finding, one writer per resource, and
incident/task statuses that stay in step.
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

import incidents
import ops_controller
import ops_queue
import task_store
from fake_redis import FakeRedis


def _pod(name="api-1", reasons=(), deleting=False, protected=False,
         restarts=0, phase="Running"):
    return {
        "name": name, "phase": phase, "ready": "0/1", "restarts": restarts,
        "reasons": list(reasons), "owner": "ReplicaSet/api-rs", "node": "node-0",
        "started": "5m", "age_seconds": 300, "deleting": deleting,
        "deleting_age": "10m" if deleting else "", "deleting_seconds": 600,
        "last_terminated": {}, "protected": protected,
    }


def _snapshot(pods=()):
    return {"namespace": "auton-apps", "captured_at": "",
            "pods": list(pods), "deployments": [], "statefulsets": [],
            "pvcs": [], "events": []}


@pytest.fixture()
def env(monkeypatch, tmp_path):
    """Temp incident store, no cluster, playbook + notify stubbed."""
    incident_store = incidents.IncidentStore(str(tmp_path / "incidents"))
    monkeypatch.setattr(ops_controller, "STORE", incident_store)
    monkeypatch.setattr(ops_controller, "_workload_for",
                        lambda pod, namespace: ("", ""))
    monkeypatch.setattr(ops_controller, "_diagnose", lambda item, ns: "")
    monkeypatch.setattr(ops_controller, "DRY_RUN", False)
    monkeypatch.setattr(ops_controller, "MAX_ACTIONS_PER_HOUR", 6)
    monkeypatch.setattr(ops_controller, "COOLDOWN", 1800)
    delivered: list[dict] = []
    monkeypatch.setattr(ops_controller, "notify",
                        lambda record: delivered.append(record) or record)
    instance = SimpleNamespace(
        incidents=incident_store,
        queue=task_store.TaskStore(ops_queue.MemoryBackend(), lease=30,
                                   max_attempts=2, backoff=[0, 0],
                                   max_queue=50),
        delivered=delivered,
        repair=lambda *a, **k: {"action": "oom_bump_memory", "ok": True,
                                "detail": "raised memory 256Mi -> 320Mi",
                                "verification": "deployment/api ready=1/1",
                                "needs_human": False},
    )
    monkeypatch.setattr(ops_controller, "_run_playbook",
                        lambda *a, **k: instance.repair(*a, **k))
    return instance


def _collect(monkeypatch, snapshot):
    monkeypatch.setattr(ops_controller.k8s_tools, "_collect",
                        lambda namespace: (snapshot, ""))


def _run(env, name="api-1", cause="oom", severity="critical"):
    item = ops_controller._finding("Pod", name, cause, severity,
                                   f"pod/{name} reports OOMKilled")
    return ops_controller._submit_finding(env.queue, item, "auton-apps")


def _execute(env):
    claim = env.queue.claim("repair", "worker-1")[0]
    return ops_controller.execute_task(env.queue, claim), claim


# --------------------------------------------------------------------------- #
# Scanner
# --------------------------------------------------------------------------- #

def test_scan_queues_one_task_per_finding(env, monkeypatch):
    _collect(monkeypatch, _snapshot(pods=[
        _pod("oom-1", ["OOMKilled"]),
        _pod("loop-1", ["CrashLoopBackOff"]),
        _pod("ok-1"),
    ]))
    summary = ops_controller.scan_once(env.queue)

    assert summary["findings"] == 2
    assert summary["queued"] == 2
    tasks = env.queue.list(queue="repair")
    assert {task["cause"] for task in tasks} == {"oom", "crashloop"}
    oom = next(t for t in tasks if t["cause"] == "oom")
    assert oom["namespace"] == "auton-apps"
    assert oom["resource"] == "pod/oom-1"
    assert oom["priority"] == "critical"
    assert oom["source"] == "scanner"
    assert oom["status"] == "queued"


def test_scan_is_read_only_and_survives_a_snapshot_error(env, monkeypatch):
    def boom(namespace):
        return None, "cluster unreachable"
    monkeypatch.setattr(ops_controller.k8s_tools, "_collect", boom)

    summary = ops_controller.scan_once(env.queue)
    assert summary["errors"] == 1
    assert summary["queued"] == 0
    assert env.queue.list() == []


def test_two_scanners_submit_the_same_finding_once(env, monkeypatch):
    _collect(monkeypatch, _snapshot(pods=[_pod("oom-1", ["OOMKilled"])]))
    # Two independent stores share the backend, like two scanner pods.
    other = task_store.TaskStore(env.queue.backend, lease=30, max_attempts=2,
                                 backoff=[0, 0], max_queue=50)
    first = ops_controller.scan_once(env.queue)
    assert first["queued"] == 1

    # The race the idempotency key exists for: the second scanner read the
    # snapshot before the first submit, so it also tries to enqueue.
    item = ops_controller._finding("Pod", "oom-1", "oom", "critical",
                                   "pod/oom-1 reports OOMKilled")
    duplicate = ops_controller._submit_finding(other, item, "auton-apps")
    assert duplicate["duplicate"] is True
    assert duplicate["id"] == first["tasks"][0]

    # A later scanner sees the active task and does not even try.
    later = ops_controller.scan_once(other)
    assert later["suppressed"] == 1
    assert later["queued"] == 0
    assert len(env.queue.list()) == 1


def test_scan_suppresses_an_active_task(env, monkeypatch):
    _collect(monkeypatch, _snapshot(pods=[_pod("oom-1", ["OOMKilled"])]))
    ops_controller.scan_once(env.queue)
    # A second pass in the next dedupe bucket must still see the active task.
    summary = ops_controller.scan_once(env.queue)
    assert summary["suppressed"] == 1
    assert summary["queued"] == 0


def test_scan_suppresses_a_recent_incident(env, monkeypatch):
    _collect(monkeypatch, _snapshot(pods=[_pod("oom-1", ["OOMKilled"])]))
    record = env.incidents.create(namespace="auton-apps", kind="Pod",
                                  name="oom-1", cause="oom")
    env.incidents.update(record["id"], status="resolved")

    summary = ops_controller.scan_once(env.queue)
    assert summary["suppressed"] == 1
    assert env.queue.list() == []


def test_scan_suppresses_a_cooldown(env, monkeypatch):
    _collect(monkeypatch, _snapshot(pods=[_pod("oom-1", ["OOMKilled"])]))
    env.queue.set_cooldown("pod/oom-1", "oom", 60)
    summary = ops_controller.scan_once(env.queue)
    assert summary["suppressed"] == 1
    assert env.queue.list() == []


# --------------------------------------------------------------------------- #
# Worker
# --------------------------------------------------------------------------- #

def test_execute_resolves_task_and_incident(env):
    task = _run(env)
    result, claim = _execute(env)

    assert result["task"] == "resolved"
    stored = env.queue.get(task["id"])
    assert stored["status"] == "resolved"
    assert stored["result"]["action"] == "oom_bump_memory"
    assert stored["verification"] == "deployment/api ready=1/1"
    incident = env.incidents.get(stored["incident_id"])
    assert incident["status"] == "resolved"
    assert incident["actions"][0]["ok"] is True
    assert env.delivered and env.delivered[-1]["id"] == incident["id"]
    # Cooldown is per resource+cause, and the resource lock is released.
    assert env.queue.in_cooldown("pod/api-1", "oom")
    assert env.queue.backend.get(env.queue.lock_key("pod/api-1")) is None
    assert env.queue.backend.zcard(env.queue.claimed_key()) == 0


def test_execute_report_only_never_runs_a_playbook(env, monkeypatch):
    monkeypatch.setattr(ops_controller, "_run_playbook",
                        lambda *a: pytest.fail("report-only must not repair"))
    task = _run(env, cause="image")
    result, _ = _execute(env)

    assert result["task"] == "needs_human"
    stored = env.queue.get(task["id"])
    incident = env.incidents.get(stored["incident_id"])
    assert incident["status"] == "needs_human"
    assert incident["needs_human"] is True
    assert incident["actions"][0]["action"] == "report_only"
    assert "Fix the image reference" in incident["report"]


def test_execute_dry_run_records_detected_without_mutating(env, monkeypatch):
    monkeypatch.setattr(ops_controller, "DRY_RUN", True)
    monkeypatch.setattr(ops_controller, "_run_playbook",
                        lambda *a: pytest.fail("dry run must not repair"))
    task = _run(env)
    result, _ = _execute(env)

    assert result["task"] == "skipped"
    stored = env.queue.get(task["id"])
    assert stored["result"]["action"] == "dry_run"
    incident = env.incidents.get(stored["incident_id"])
    assert incident["status"] == "detected"
    assert incident["actions"][0]["action"] == "dry_run"
    assert "nothing was changed" in incident["verification"]


def test_execute_skips_when_the_budget_is_exhausted(env, monkeypatch):
    monkeypatch.setattr(ops_controller, "MAX_ACTIONS_PER_HOUR", 1)
    env.queue.take_budget(1)  # another worker used the only action
    monkeypatch.setattr(ops_controller, "_run_playbook",
                        lambda *a: pytest.fail("budget must skip the repair"))
    task = _run(env)
    result, _ = _execute(env)

    assert result["task"] == "skipped"
    stored = env.queue.get(task["id"])
    assert stored["result"]["action"] == "budget"
    assert stored["incident_id"] == ""
    assert env.incidents.list() == []


def test_execute_respects_cooldown_unless_forced(env):
    env.queue.set_cooldown("pod/api-1", "oom", 300)
    forced = env.queue.submit("repair", {
        "namespace": "auton-apps", "kind": "Pod", "name": "api-1",
        "cause": "oom", "severity": "critical", "force": True,
        "source": "manual"}, priority="critical")
    result, _ = _execute(env)
    assert result["task"] == "resolved"
    assert env.queue.get(forced["id"])["status"] == "resolved"


def test_execute_skips_a_cooldown_for_an_automatic_task(env):
    env.queue.set_cooldown("pod/api-1", "oom", 300)
    task = _run(env)
    result, _ = _execute(env)
    assert result["task"] == "skipped"
    assert env.queue.get(task["id"])["result"]["action"] == "cooldown"


def test_execute_refuses_a_namespace_outside_the_ops_allowlist(env):
    task = env.queue.submit("repair", {
        "namespace": "auton", "kind": "Deployment", "name": "auton",
        "cause": "oom", "severity": "critical"})
    result, _ = _execute(env)
    assert result["task"] == "skipped"
    assert env.queue.get(task["id"])["result"]["action"] == "refused"


def test_execute_retries_a_playbook_exception_and_reuses_the_incident(env):
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("kubectl timed out")
        return {"action": "oom_bump_memory", "ok": True,
                "detail": "raised", "verification": "ready=1/1",
                "needs_human": False}

    env.repair = flaky
    task = _run(env)
    first, _ = _execute(env)
    assert first["task"] == "queued"
    stored = env.queue.get(task["id"])
    assert stored["status"] == "queued"
    assert stored["attempts"] == 1
    incident_id = stored["incident_id"]
    assert env.incidents.get(incident_id)["status"] == "failed"

    # The delayed retry is due immediately (backoff=[0, 0]).
    second, _ = _execute(env)
    assert second["task"] == "resolved"
    final = env.queue.get(task["id"])
    assert final["attempts"] == 2
    assert final["incident_id"] == incident_id
    incident = env.incidents.get(incident_id)
    assert incident["status"] == "resolved"
    assert [a["action"] for a in incident["actions"]] == [
        "playbook_error", "oom_bump_memory"]


def test_execute_gives_up_after_max_attempts(env):
    def boom(*args, **kwargs):
        raise RuntimeError("still broken")
    env.repair = boom
    task = _run(env)
    _execute(env)                     # attempt 1 -> queued
    result, _ = _execute(env)         # attempt 2 -> max -> needs_human
    assert result["task"] == "needs_human"
    stored = env.queue.get(task["id"])
    assert stored["status"] == "needs_human"
    incident = env.incidents.get(stored["incident_id"])
    assert incident["status"] == "needs_human"
    assert incident["needs_human"] is True


# --------------------------------------------------------------------------- #
# End to end (scan → claim → execute), both backends
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("backend", ["memory", "redis"])
def test_scan_claim_execute_end_to_end(env, monkeypatch, backend):
    if backend == "redis":
        env.queue = task_store.TaskStore(
            ops_queue.RedisBackend(client=FakeRedis()), lease=30,
            max_attempts=2, backoff=[0, 0], max_queue=50)
    _collect(monkeypatch, _snapshot(pods=[_pod("oom-1", ["OOMKilled"])]))

    summary = ops_controller.scan_once(env.queue)
    assert summary["queued"] == 1
    result, _ = _execute(env)
    assert result["task"] == "resolved"

    # A later scan is suppressed by the queue cooldown, not re-queued.
    _collect(monkeypatch, _snapshot(pods=[_pod("oom-1", ["OOMKilled"])]))
    again = ops_controller.scan_once(env.queue)
    assert again["suppressed"] == 1
    assert again["queued"] == 0

    stats = env.queue.stats()
    assert stats["statuses"]["resolved"] == 1
    assert stats["backend"] == backend


def test_delayed_retry_is_promoted_only_when_due(env):
    task = _run(env)
    claim = env.queue.claim("repair", "worker-1")[0]
    env.queue.retry(claim, reason="kubectl hiccup")
    # backoff is [0, 0] here, so it is due immediately; with a positive backoff
    # the delay key holds it back (covered in test_task_store).
    env.queue.backend.zadd(env.queue.delayed_key(), time.time() + 60,
                           task["id"])
    assert env.queue.promote_delayed() == []
    assert env.queue.get(task["id"])["status"] == "queued"
