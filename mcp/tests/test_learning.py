"""Tests for the Learn stage (outcomes, profiles, bounded auto-adaptation).

The Learn stage is the only part of the platform that changes its own
behaviour, so the tests are mostly about the **envelope**:

* nothing is adapted below the sample threshold;
* a learned value may only make the platform more cautious;
* every hard cap holds, even when the stored profile is corrupt;
* an adaptation without an audit record does not happen;
* the policy engine and the learning switch can both stop it.
"""
from __future__ import annotations

import json
import time

import pytest

import audit
import incidents
import learning
import ops_controller
import ops_queue
import task_store


@pytest.fixture()
def store(tmp_path):
    return learning.LearningStore(str(tmp_path / "learning"))


@pytest.fixture()
def service(store):
    return learning.LearningService(store)


def _outcome(**overrides) -> dict:
    row = {
        "namespace": "auton-apps",
        "resource": "deployment/api",
        "cause": "oom",
        "action": "oom_bump_memory",
        "ok": True,
        "needs_human": False,
        "ts": time.time(),
        "duration_seconds": 1.5,
    }
    row.update(overrides)
    return row


def _fill(service, count: int, ok: bool = True, cause: str = "oom",
          resource: str = "deployment/api") -> list[dict]:
    rows = []
    now = time.time()
    for index in range(count):
        row = _outcome(ok=ok, cause=cause, resource=resource,
                       ts=now + index)
        rows.append(service.record_outcome(row) or row)
    return rows


# --------------------------------------------------------------------------- #
# The store
# --------------------------------------------------------------------------- #

def test_outcomes_are_stored_atomically_and_read_back(store):
    saved = store.save_outcome(_outcome())
    assert saved["id"].startswith("out-")
    assert saved["ts_iso"].endswith("Z")
    assert store.get_outcome(saved["id"])["resource"] == "deployment/api"
    # No temp file is left behind.
    assert not list(store.directory.glob("*.tmp-*"))


def test_outcomes_are_filtered(store):
    store.save_outcome(_outcome())
    store.save_outcome(_outcome(cause="crashloop", resource="deployment/other"))
    assert len(store.outcomes()) == 2
    assert len(store.outcomes(cause="oom")) == 1
    assert len(store.outcomes(resource="deployment/other")) == 1
    assert len(store.outcomes(namespace="auton")) == 0


def test_the_store_is_bounded(tmp_path):
    store = learning.LearningStore(str(tmp_path / "learning"), max_outcomes=10)
    for index in range(25):
        store.save_outcome(_outcome(ts=float(index)))
    assert len(store.outcomes(limit=100)) <= 10


def test_the_profile_is_per_resource_and_cause(store):
    a = learning.profile_id("auton-apps", "deployment/api", "oom")
    b = learning.profile_id("auton-apps", "deployment/api", "crashloop")
    c = learning.profile_id("auton-apps", "deployment/other", "oom")
    assert a != b and a != c
    assert a.startswith("lp-")


def test_an_invalid_id_never_escapes_the_directory(store):
    with pytest.raises(learning.LearningError):
        store.get_outcome("../../etc/passwd")


# --------------------------------------------------------------------------- #
# The threshold: nothing is learned from one or two samples
# --------------------------------------------------------------------------- #

def test_nothing_is_adapted_below_the_sample_threshold(service):
    _fill(service, learning.MIN_SAMPLES - 1)
    applied = service.adapt("auton-apps", "deployment/api", "oom")
    assert applied == []
    assert service.profile("auton-apps", "deployment/api", "oom")["tier"] == 0


def test_the_oom_step_grows_after_enough_repairs(service):
    _fill(service, learning.MIN_SAMPLES)
    applied = service.adapt("auton-apps", "deployment/api", "oom")
    names = [change["name"] for change in applied]
    assert "raise_oom_step" in names
    profile = service.profile("auton-apps", "deployment/api", "oom")
    assert profile["memory_step"] == pytest.approx(0.5)
    assert profile["memory_step"] <= learning.MAX_STEP


def test_the_step_never_grows_past_the_cap(service):
    _fill(service, learning.MIN_SAMPLES * 6)
    for _ in range(5):
        service.adapt("auton-apps", "deployment/api", "oom")
    profile = service.profile("auton-apps", "deployment/api", "oom")
    assert profile["memory_step"] <= learning.MAX_STEP


def test_the_tier_only_grows_after_twice_as_many_samples(service):
    _fill(service, learning.MIN_SAMPLES)
    service.adapt("auton-apps", "deployment/api", "oom")
    assert service.profile("auton-apps", "deployment/api", "oom")["tier"] == 0
    _fill(service, learning.MIN_SAMPLES)
    service.adapt("auton-apps", "deployment/api", "oom")
    assert service.profile("auton-apps", "deployment/api", "oom")["tier"] >= 1


def test_a_learned_tier_never_lowers_the_base_tier(service):
    _fill(service, learning.MIN_SAMPLES * 2)
    service.adapt("auton-apps", "deployment/api", "oom")
    assert service.learned_tier("auton-apps", "deployment/api", 3, "oom") == 3
    assert service.learned_tier("auton-apps", "deployment/api", 0, "oom") >= 0


def test_failures_raise_the_cooldown_and_lower_the_budget(service):
    _fill(service, learning.MIN_SAMPLES, ok=False)
    applied = service.adapt("auton-apps", "deployment/api", "oom")
    names = [change["name"] for change in applied]
    assert "raise_cooldown" in names
    assert "lower_budget" in names
    profile = service.profile("auton-apps", "deployment/api", "oom")
    assert profile["cooldown_seconds"] >= learning.BASE_COOLDOWN
    assert profile["cooldown_seconds"] <= learning.COOLDOWN_MAX
    assert profile["budget_per_hour"] >= learning.BUDGET_MIN
    assert profile["budget_per_hour"] <= learning.BASE_BUDGET


def test_repeated_failures_quarantine_the_resource(service):
    _fill(service, learning.MAX_ESCALATIONS, ok=False)
    applied = service.adapt("auton-apps", "deployment/api", "oom")
    assert "quarantine" in [change["name"] for change in applied]
    assert service.quarantined("auton-apps", "deployment/api", "oom") is True


def test_a_quarantine_is_never_raised_again(service):
    _fill(service, learning.MAX_ESCALATIONS * 2, ok=False)
    for _ in range(3):
        service.adapt("auton-apps", "deployment/api", "oom")
    profile = service.profile("auton-apps", "deployment/api", "oom")
    quarantines = [a for a in profile["adaptations"]
                   if a["name"] == "quarantine"]
    assert len(quarantines) == 1


def test_a_success_resets_the_consecutive_failures(service):
    now = time.time()
    for index in range(2):
        service.record_outcome(_outcome(ok=False, ts=now + index))
    service.record_outcome(_outcome(ok=True, ts=now + 10))
    assert learning.summarise(
        service.store.outcomes(namespace="auton-apps",
                               resource="deployment/api")
    )["consecutive_failures"] == 0


# --------------------------------------------------------------------------- #
# The envelope: the clamps hold even for a corrupt profile
# --------------------------------------------------------------------------- #

def test_the_step_is_clamped():
    assert learning.clamp_step(99) == learning.MAX_STEP
    assert learning.clamp_step(0.01) == learning.MIN_STEP
    assert learning.clamp_step("nonsense") == learning.MIN_STEP


def test_the_cooldown_is_clamped():
    assert learning.clamp_cooldown(10 ** 9) == learning.COOLDOWN_MAX
    assert learning.clamp_cooldown(0, base=600) == 600
    assert learning.clamp_cooldown("nonsense", base=600) == 600


def test_the_budget_is_clamped():
    assert learning.clamp_budget(0) == learning.BUDGET_MIN
    assert learning.clamp_budget(10 ** 6) == learning.BASE_BUDGET
    assert learning.clamp_budget("nonsense") == learning.BASE_BUDGET


def test_the_tier_is_clamped():
    assert learning.clamp_tier(9, base=1) == 3
    assert learning.clamp_tier(0, base=2) == 2
    assert learning.clamp_tier("nonsense", base=2) == 2


def test_an_unknown_adaptation_is_refused():
    with pytest.raises(learning.LearningError):
        learning.apply_change({"tier": 0}, {"name": "delete_everything"})
    # The planner only ever emits names from the allowlist.
    changes = learning.plan_adaptations({"cause": "oom", "tier": 0}, [])
    assert all(change["name"] in learning.ADAPTATIONS for change in changes)


def test_the_allowlist_is_closed():
    assert set(learning.ADAPTATIONS) == {
        "raise_oom_step", "raise_tier", "raise_cooldown", "lower_budget",
        "quarantine"}


# --------------------------------------------------------------------------- #
# The gates
# --------------------------------------------------------------------------- #

def test_learning_can_be_switched_off(store):
    service = learning.LearningService(store, enabled=False)
    _fill(service, learning.MIN_SAMPLES * 2)
    assert service.adapt("auton-apps", "deployment/api", "oom") == []
    assert service.profile("auton-apps", "deployment/api", "oom")["tier"] == 0


def test_a_disabled_policy_stops_every_adaptation(service):
    _fill(service, learning.MIN_SAMPLES * 2, ok=False)
    assert service.adapt("auton-apps", "deployment/api", "oom",
                          policy_mode="disabled") == []
    assert service.quarantined("auton-apps", "deployment/api", "oom") is False


def test_observe_only_records_but_does_not_adapt(service):
    _fill(service, learning.MIN_SAMPLES * 2, ok=False)
    assert service.adapt("auton-apps", "deployment/api", "oom",
                          policy_mode="observe-only") == []
    assert service.quarantined("auton-apps", "deployment/api", "oom") is False
    # The outcomes are still there: observe-only means "watch", not "forget".
    assert len(service.store.outcomes()) == learning.MIN_SAMPLES * 2


def test_no_adaptation_without_an_audit_record(service, monkeypatch, audit_dir):
    _fill(service, learning.MIN_SAMPLES * 2, ok=False)

    def refuse(*args, **kwargs):
        raise audit.AuditError("audit log is not writable")

    monkeypatch.setattr(audit, "begin", refuse)
    applied = service.adapt("auton-apps", "deployment/api", "oom")
    assert applied == []
    assert service.quarantined("auton-apps", "deployment/api", "oom") is False


def test_an_adaptation_writes_an_audit_record(service, audit_dir):
    _fill(service, learning.MIN_SAMPLES * 2, ok=False)
    applied = service.adapt("auton-apps", "deployment/api", "oom")
    assert applied
    rows = audit.recent(50)
    learned = [row for row in rows if row.get("action") == "learn_adapt"]
    assert learned, "the adaptation must be auditable"
    begin = [row for row in learned if row.get("phase") == "begin"]
    assert begin
    ids = {row.get("audit_id") for row in begin}
    ends = [row for row in rows
            if row.get("phase") == "end" and row.get("audit_id") in ids]
    assert ends, "the begin/end pair must be complete"


# --------------------------------------------------------------------------- #
# The service helpers
# --------------------------------------------------------------------------- #

def test_recent_actions_counts_the_repairs_of_a_resource(service):
    _fill(service, 3)
    assert service.recent_actions("auton-apps", "deployment/api") == 3
    assert service.recent_actions("auton-apps", "deployment/other") == 0


def test_a_blank_profile_is_returned_for_an_unknown_resource(service):
    profile = service.profile("auton-apps", "deployment/unknown", "oom")
    assert profile["attempts"] == 0
    assert profile["quarantined"] is False
    assert profile["memory_step"] == learning.MIN_STEP


def test_the_stats_summarise_the_outcomes(service):
    _fill(service, 2, ok=True)
    _fill(service, 1, ok=False, resource="deployment/other")
    stats = service.store.stats()
    assert stats["outcomes"] == 3
    assert stats["causes"]
    assert stats["limits"]["min_samples"] == learning.MIN_SAMPLES


def test_a_missing_directory_makes_the_service_inert(tmp_path, monkeypatch):
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv("AGENT_LEARN_DIR", str(blocker / "learning"))
    service = learning.LearningService()
    assert service.enabled is False
    assert service.record_outcome(_outcome()) is None
    assert service.adapt("auton-apps", "deployment/api", "oom") == []


# --------------------------------------------------------------------------- #
# The pipeline: the Learn stage inside the repair path
# --------------------------------------------------------------------------- #

def _evidence_document(cause: str = "oom") -> dict:
    return {
        "resource": {"kind": "Pod", "name": "api-1", "namespace": "auton-apps",
                     "uid": "uid-abc", "labels": {"app": "api"}},
        "problem": "OOMKilled" if cause == "oom" else "CrashLoopBackOff",
        "phase": "Running",
        "restart_count": 4,
        "container": {
            "name": "api", "restart_count": 4,
            "last_termination_reason": "OOMKilled", "exit_code": 137,
            "state": "waiting", "ready": False, "image": "app:1",
            "resources": {"memory_request": "256Mi", "memory_limit": "512Mi"},
            "probes": {},
            "image_pull_secret_configured": False,
        },
        "resources": {"memory_request": "256Mi", "memory_limit": "512Mi"},
        "owner": {"kind": "Deployment", "name": "api"},
        "probes": {},
        "node": {"name": "node-1", "available": True},
        "node_metrics": {},
        "events": [],
        "logs": "",
        "previous_logs": "",
        "deployment": {},
        "evidence": {"pod_available": True},
        "errors": [],
    }


@pytest.fixture()
def pipeline(monkeypatch, tmp_path):
    """The repair pipeline with the cluster mocked out."""
    incident_store = incidents.IncidentStore(str(tmp_path / "incidents"))
    monkeypatch.setattr(ops_controller, "STORE", incident_store)
    monkeypatch.setattr(ops_controller, "_diagnose", lambda item, ns: "text")
    monkeypatch.setattr(ops_controller, "_structured_evidence",
                        lambda item, ns: _evidence_document(item["cause"]))
    monkeypatch.setattr(ops_controller, "DRY_RUN", False)
    monkeypatch.setattr(ops_controller, "MAX_ACTIONS_PER_HOUR", 6)
    monkeypatch.setattr(ops_controller, "COOLDOWN", 1800)
    monkeypatch.setattr(ops_controller, "notify",
                        lambda record: record)
    calls: list[dict] = []

    def playbook(item, namespace, evidence, step=0.25):
        calls.append({"item": item, "namespace": namespace, "step": step})
        return {"action": "oom_bump_memory", "ok": True,
                "detail": "raised 512Mi -> 640Mi",
                "verification": "deployment/api ready=1/1",
                "needs_human": False}

    monkeypatch.setattr(ops_controller, "_run_playbook", playbook)
    return type("Env", (), {
        "incidents": incident_store,
        "calls": calls,
        "queue": task_store.TaskStore(ops_queue.MemoryBackend(), lease=30,
                                      max_attempts=2, backoff=[0, 0],
                                      max_queue=50),
    })


def _submit(pipeline, cause="oom"):
    item = ops_controller._finding("Pod", "api-1", cause, "critical",
                                   f"pod/api-1 reports {cause}")
    return ops_controller._submit_finding(pipeline.queue, item, "auton-apps")


def _submit_again(pipeline, cause="oom"):
    """A task that bypasses the scanner's dedupe window (a new attempt)."""
    return pipeline.queue.submit("repair", {
        "namespace": "auton-apps", "kind": "Pod", "name": "api-1",
        "cause": cause, "severity": "critical",
        "observation": f"pod/api-1 reports {cause}",
        "resource": "pod/api-1", "source": "test", "force": True,
    }, priority="critical")


def _execute(pipeline):
    claim = pipeline.queue.claim("repair", "worker-1")[0]
    return ops_controller.execute_task(pipeline.queue, claim)


def test_a_repair_records_an_outcome(pipeline):
    task = _submit(pipeline)
    result = _execute(pipeline)
    assert result["task"] == "resolved"
    outcomes = ops_controller.LEARNING.store.outcomes()
    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert outcome["ok"] is True
    assert outcome["resource"] == "pod/api-1"
    assert outcome["cause"] == "oom"
    assert outcome["incident_id"]
    incident = pipeline.incidents.get(outcome["incident_id"])
    assert incident["learning"]["outcome_id"] == outcome["id"]


def test_the_learned_step_reaches_the_playbook(pipeline):
    # Three repairs of the same cause: the next one uses a bigger bump.
    for _ in range(learning.MIN_SAMPLES):
        _submit_again(pipeline)
        _execute(pipeline)
    assert pipeline.calls[-1]["step"] == pytest.approx(0.25)

    # The profile now knows the workload keeps OOMing.
    profile = ops_controller.LEARNING.profile("auton-apps", "pod/api-1", "oom")
    assert profile["memory_step"] == pytest.approx(0.5)

    _submit_again(pipeline)
    _execute(pipeline)
    assert pipeline.calls[-1]["step"] == pytest.approx(0.5)


def test_a_quarantined_resource_is_not_repaired(pipeline, monkeypatch):
    monkeypatch.setattr(ops_controller, "_run_playbook",
                        lambda *a, **k: pytest.fail("a quarantined resource "
                                                    "must not be repaired"))
    service = ops_controller.LEARNING
    for _ in range(learning.MAX_ESCALATIONS):
        service.record_outcome(_outcome(ok=False, resource="pod/api-1"))
    service.adapt("auton-apps", "pod/api-1", "oom")

    task = _submit(pipeline)
    result = _execute(pipeline)
    assert result["task"] == "needs_human"
    stored = pipeline.queue.get(task["id"])
    assert stored["result"]["action"] == "quarantined"
    incident = pipeline.incidents.get(stored["incident_id"])
    assert incident["status"] == "needs_human"
    assert incident["actions"][0]["action"] == "quarantined"


def test_the_per_resource_budget_stops_a_single_workload(pipeline, monkeypatch):
    monkeypatch.setattr(ops_controller, "MAX_ACTIONS_PER_HOUR", 100)
    service = ops_controller.LEARNING
    profile = service.profile("auton-apps", "pod/api-1", "oom")
    profile["budget_per_hour"] = 1
    service.store.save_profile(profile)
    service.record_outcome(_outcome(resource="pod/api-1"))

    task = _submit(pipeline)
    result = _execute(pipeline)
    assert result["task"] == "skipped"
    stored = pipeline.queue.get(task["id"])
    assert stored["result"]["action"] == "resource_budget"


def test_the_learned_tier_raises_the_plan(pipeline):
    service = ops_controller.LEARNING
    profile = service.profile("auton-apps", "pod/api-1", "oom")
    profile["tier"] = 3
    service.store.save_profile(profile)

    _submit(pipeline)
    _execute(pipeline)
    incident = pipeline.incidents.list(limit=1)[0]
    assert incident["repair_plan"]["tier"] == 3
    assert "tier was raised" in incident["repair_plan"]["learned"]


def test_the_learning_key_is_the_workload_not_the_pod(monkeypatch):
    """An OOM kills a pod, but the repair (and the learning) is the workload."""
    monkeypatch.setattr(ops_controller.playbooks, "_get",
                        lambda resource, name, namespace: (
                            {"metadata": {"ownerReferences": [
                                {"kind": "ReplicaSet", "name": "api-rs"}]}},
                            ""))
    monkeypatch.setattr(ops_controller.playbooks, "workload_for_pod",
                        lambda pod, namespace: ("Deployment", "api"))
    item = {"kind": "Pod", "name": "api-1"}
    assert ops_controller._learning_resource(item, "auton-apps",
                                             "pod/api-1") == "deployment/api"


def test_the_learning_key_falls_back_to_the_pod(monkeypatch):
    monkeypatch.setattr(ops_controller.playbooks, "_get",
                        lambda resource, name, namespace: (None, "not found"))
    item = {"kind": "Pod", "name": "api-1"}
    assert ops_controller._learning_resource(item, "auton-apps",
                                             "pod/api-1") == "pod/api-1"
    # A workload finding keeps its own key.
    item = {"kind": "Deployment", "name": "api"}
    assert ops_controller._learning_resource(item, "auton-apps",
                                             "deployment/api") == "deployment/api"


def test_a_learned_tier_parks_the_repair_for_a_human(pipeline):
    """Learning may only make the platform more cautious: a raised tier
    turns an automatic repair into one that waits for an approval."""
    service = ops_controller.LEARNING
    profile = service.profile("auton-apps", "pod/api-1", "oom")
    profile["tier"] = 3
    service.store.save_profile(profile)

    task = _submit(pipeline)
    result = _execute(pipeline)
    assert result["task"] == "awaiting_approval"
    stored = pipeline.queue.get(task["id"])
    assert stored["status"] == "awaiting_approval"
    # Nothing was changed and no playbook ran.
    assert pipeline.calls == []
    incident = pipeline.incidents.get(stored["incident_id"])
    assert incident["status"] == "awaiting_approval"
    assert incident["repair_plan"]["tier"] == 3


# --------------------------------------------------------------------------- #
# The API: GET /learning
# --------------------------------------------------------------------------- #

def _request(method: str, url: str, body=None, timeout: float = 15):
    import urllib.error
    import urllib.request

    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


@pytest.fixture()
def api(monkeypatch, tmp_path):
    """The HTTP service with an isolated learning store."""
    import threading

    import agent_service

    monkeypatch.setattr(agent_service, "LEARNING_STORE",
                        learning.LearningStore(str(tmp_path / "learning")))
    httpd = agent_service.ThreadingHTTPServer(("127.0.0.1", 0),
                                               agent_service.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def test_the_learning_endpoint_reports_the_outcomes_and_profiles(api):
    import agent_service

    agent_service.LEARNING_STORE.save_outcome(_outcome())
    status, payload = _request("GET", f"{api}/learning")
    assert status == 200
    assert payload["enabled"] is True
    assert payload["outcomes"]
    assert payload["stats"]["outcomes"] == 1
    assert payload["adaptations"] == list(learning.ADAPTATIONS)


def test_the_learning_endpoint_can_be_filtered(api):
    import agent_service

    agent_service.LEARNING_STORE.save_outcome(_outcome())
    agent_service.LEARNING_STORE.save_outcome(
        _outcome(resource="deployment/other", cause="crashloop"))
    status, payload = _request("GET", f"{api}/learning?cause=oom")
    assert status == 200
    assert len(payload["outcomes"]) == 1
    assert payload["outcomes"][0]["cause"] == "oom"

