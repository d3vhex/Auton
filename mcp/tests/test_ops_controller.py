"""Tests for the autonomous ops controller (detection, lifecycle, reporting)."""
from __future__ import annotations

import json

import pytest

import incidents
import ops_controller


def _pod(name="api-1", reasons=(), deleting=False, protected=False,
         restarts=0, phase="Running", deleting_age="", age_seconds=300,
         deleting_seconds=None):
    return {
        "name": name,
        "phase": phase,
        "ready": "0/1",
        "restarts": restarts,
        "reasons": list(reasons),
        "owner": "ReplicaSet/api-rs",
        "node": "node-0",
        "started": "5m",
        "age_seconds": age_seconds,
        "deleting": deleting,
        "deleting_age": deleting_age,
        "deleting_seconds": deleting_seconds,
        "last_terminated": {},
        "protected": protected,
    }


def _deploy(name="api", problems=(), desired=1, ready="0/1", available=0,
            protected=False):
    return {
        "name": name, "ready": ready, "updated": 1, "available": available,
        "desired": desired, "problems": list(problems), "protected": protected,
    }


def _snapshot(pods=(), deployments=(), statefulsets=(), pvcs=()):
    return {
        "namespace": "auton-apps",
        "captured_at": "2026-01-01T00:00:00+00:00",
        "pods": list(pods),
        "deployments": list(deployments),
        "statefulsets": list(statefulsets),
        "pvcs": list(pvcs),
        "events": [],
    }


@pytest.fixture()
def no_workload(monkeypatch):
    monkeypatch.setattr(ops_controller, "_workload_for",
                        lambda pod, namespace: ("", ""))


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #

def test_findings_maps_pod_reasons(no_workload):
    found = ops_controller.findings(_snapshot(pods=[
        _pod("oom-pod", ["OOMKilled"], restarts=4),
        _pod("loop-pod", ["CrashLoopBackOff"]),
        _pod("bad-pod", ["ImagePullBackOff"]),
        _pod("odd-pod", ["SomeOtherReason"]),
    ]))
    by_name = {item["name"]: item for item in found}
    assert by_name["oom-pod"]["cause"] == "oom"
    assert by_name["oom-pod"]["severity"] == "critical"
    assert "restarts=4" in by_name["oom-pod"]["observation"]
    assert by_name["loop-pod"]["cause"] == "crashloop"
    assert by_name["bad-pod"]["cause"] == "image"
    assert "odd-pod" not in by_name


def test_findings_acts_on_one_cause_per_pod(no_workload):
    found = ops_controller.findings(_snapshot(pods=[
        _pod("crashing", ["CrashLoopBackOff", "OOMKilled"], restarts=5),
    ]))
    assert [(item["name"], item["cause"]) for item in found] == [
        ("crashing", "oom")]


def test_findings_reports_stuck_terminating_pods(no_workload):
    found = ops_controller.findings(_snapshot(pods=[
        _pod("stuck", deleting=True, deleting_age="17m")]))
    assert found[0]["cause"] == "stuck"


def test_findings_ignores_a_normal_termination(no_workload):
    found = ops_controller.findings(_snapshot(pods=[
        _pod("shutting-down", deleting=True, deleting_age="4s",
             deleting_seconds=4)]))
    assert found == []


def test_findings_reports_a_long_termination(no_workload):
    found = ops_controller.findings(_snapshot(pods=[
        _pod("stuck", deleting=True, deleting_age="17m",
             deleting_seconds=1020)]))
    assert [(item["name"], item["cause"]) for item in found] == [
        ("stuck", "stuck")]
    assert "17m" in found[0]["observation"]


def test_findings_ignores_protected_resources(no_workload):
    found = ops_controller.findings(_snapshot(
        pods=[_pod("keep", ["OOMKilled"], protected=True)],
        deployments=[_deploy("keep", ["Unavailable"], protected=True)],
    ))
    assert found == []


def test_findings_do_not_duplicate_a_deployment_with_a_failing_pod(monkeypatch):
    monkeypatch.setattr(ops_controller, "_workload_for",
                        lambda pod, namespace: ("Deployment", "api"))
    found = ops_controller.findings(_snapshot(
        pods=[_pod("api-1", ["CrashLoopBackOff"])],
        deployments=[_deploy("api", ["Unavailable"])],
    ))
    assert [(item["kind"], item["cause"]) for item in found] == [
        ("Pod", "crashloop")]


def test_findings_classifies_deployment_problems(no_workload):
    found = ops_controller.findings(_snapshot(deployments=[
        _deploy("rolled", ["ProgressDeadlineExceeded"]),
        _deploy("zero", ["Unavailable"], desired=0),
        _deploy("down", ["Unavailable"], desired=2, ready="0/2"),
    ]))
    causes = {item["name"]: item["cause"] for item in found}
    assert causes == {"rolled": "rollout", "zero": "replicas_zero",
                      "down": "unavailable"}


def test_findings_reports_statefulsets_and_unbound_pvcs(no_workload):
    found = ops_controller.findings(_snapshot(
        statefulsets=[_deploy("db", ["Unavailable"])],
        pvcs=[{"name": "data", "phase": "Pending", "storage_class": "local-path"},
              {"name": "ok", "phase": "Bound", "storage_class": "local-path"}],
    ))
    causes = {(item["kind"], item["name"]): item["cause"] for item in found}
    assert causes[("StatefulSet", "db")] == "statefulset"
    assert causes[("PersistentVolumeClaim", "data")] == "pvc"
    assert ("PersistentVolumeClaim", "ok") not in causes


def test_findings_deduplicate_repeated_reasons(no_workload):
    found = ops_controller.findings(_snapshot(pods=[
        _pod("api-1", ["CrashLoopBackOff", "CrashLoopBackOff"])]))
    assert len(found) == 1


def test_findings_ignores_pods_inside_startup_grace(no_workload):
    found = ops_controller.findings(_snapshot(pods=[
        _pod("starting", ["Pending"], phase="Pending", age_seconds=3),
        _pod("warming", ["NotReady"], age_seconds=90),
    ]))
    assert found == []


def test_findings_reports_pods_after_startup_grace(no_workload):
    found = ops_controller.findings(_snapshot(pods=[
        _pod("stuck", ["Pending"], phase="Pending"),
        _pod("unready", ["NotReady"]),
    ]))
    assert {item["name"]: item["cause"] for item in found} == {
        "stuck": "scheduling", "unready": "not_ready"}


def test_findings_unschedulable_is_reported_immediately(no_workload):
    found = ops_controller.findings(_snapshot(pods=[
        _pod("nosched", ["Unschedulable", "Pending"], phase="Pending",
             age_seconds=2),
    ]))
    assert [(item["name"], item["cause"]) for item in found] == [
        ("nosched", "scheduling")]


def test_findings_young_pod_does_not_flag_its_deployment(monkeypatch):
    monkeypatch.setattr(ops_controller, "_workload_for",
                        lambda pod, namespace: ("Deployment", "api"))
    found = ops_controller.findings(_snapshot(
        pods=[_pod("api-1", ["Pending"], phase="Pending", age_seconds=4)],
        deployments=[_deploy("api", ["Unavailable"])],
    ))
    assert found == []


def test_findings_startup_grace_can_be_disabled(no_workload, monkeypatch):
    monkeypatch.setattr(ops_controller, "STARTUP_GRACE", 0)
    found = ops_controller.findings(_snapshot(pods=[
        _pod("starting", ["Pending"], phase="Pending", age_seconds=3)]))
    assert [(item["name"], item["cause"]) for item in found] == [
        ("starting", "scheduling")]


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #

@pytest.fixture()
def report(monkeypatch, tmp_path):
    """A temp incident store with diagnostics and the network stubbed out."""
    store = incidents.IncidentStore(str(tmp_path / "incidents"))
    monkeypatch.setattr(ops_controller, "STORE", store)
    monkeypatch.setattr(ops_controller, "_diagnose", lambda item, ns: "")
    monkeypatch.setattr(ops_controller, "DRY_RUN", False)
    monkeypatch.setattr(ops_controller, "MAX_ACTIONS_PER_HOUR", 6)
    return store


@pytest.fixture()
def sent(report, monkeypatch):
    """Collect the records `handle` hands to `notify`."""
    delivered = []
    monkeypatch.setattr(ops_controller, "notify",
                        lambda record: delivered.append(record) or record)
    return delivered


def _item(kind="Pod", name="api-1", cause="oom", severity="critical"):
    return ops_controller._finding(kind, name, cause, severity,
                                   f"{kind.lower()}/{name} is broken")


def test_handle_runs_the_playbook_and_resolves(report, sent, monkeypatch):
    store = report
    monkeypatch.setattr(ops_controller, "_run_playbook", lambda *a: {
        "action": "oom_bump_memory", "ok": True,
        "detail": "raised memory 256Mi -> 320Mi",
        "verification": "deployment/api ready=1/1", "needs_human": False})
    monkeypatch.setattr(ops_controller.audit, "incident_actions",
                        lambda incident_id: [{"audit_id": "a1"},
                                             {"audit_id": "a1"},
                                             {"audit_id": "a2"}])

    ops_controller.handle(_item(), "auton-apps")

    records = store.list()
    assert len(records) == 1
    record = records[0]
    assert record["status"] == "resolved"
    assert record["actions"][0]["ok"] is True
    assert record["audit_ids"] == ["a1", "a2"]
    assert "resolved automatically" in record["report"]
    assert sent and sent[0]["id"] == record["id"]


def test_handle_report_only_causes_do_not_run_a_playbook(report, sent, monkeypatch):
    store = report
    monkeypatch.setattr(ops_controller, "_run_playbook",
                        lambda *a: pytest.fail("report-only must not repair"))

    ops_controller.handle(_item(cause="image"), "auton-apps")

    record = store.list()[0]
    assert record["status"] == "needs_human"
    assert record["needs_human"] is True
    assert record["actions"][0]["action"] == "report_only"
    assert "Fix the image reference" in record["report"]
    assert sent


def test_handle_dry_run_changes_nothing(report, sent, monkeypatch):
    store = report
    monkeypatch.setattr(ops_controller, "DRY_RUN", True)
    monkeypatch.setattr(ops_controller, "_run_playbook",
                        lambda *a: pytest.fail("dry run must not repair"))

    ops_controller.handle(_item(), "auton-apps")

    record = store.list()[0]
    assert record["status"] == "detected"
    assert record["actions"][0]["action"] == "dry_run"
    assert "dry run" in record["verification"]
    assert sent


def test_handle_respects_the_cooldown(report, sent, monkeypatch):
    store = report
    store.create(namespace="auton-apps", kind="Pod", name="api-1",
                 cause="oom", observation="old")
    monkeypatch.setattr(ops_controller, "_run_playbook",
                        lambda *a: pytest.fail("cooldown must skip the repair"))

    ops_controller.handle(_item(), "auton-apps")

    assert len(store.list()) == 1
    assert sent == []


def test_handle_respects_the_hourly_budget(report, sent, monkeypatch):
    store = report
    old = store.create(namespace="auton-apps", kind="Pod", name="other",
                       cause="oom", observation="already repaired")
    store.update(old["id"], status="resolved",
                 actions=[{"action": "oom_bump_memory", "ok": True}])
    monkeypatch.setattr(ops_controller, "MAX_ACTIONS_PER_HOUR", 1)
    monkeypatch.setattr(ops_controller, "_run_playbook",
                        lambda *a: pytest.fail("budget must skip the repair"))

    ops_controller.handle(_item(), "auton-apps")

    assert [r["id"] for r in store.list()] == [old["id"]]
    assert sent == []


def test_handle_records_a_playbook_exception(report, monkeypatch):
    store = report

    def boom(*a):
        raise RuntimeError("kubectl exploded")

    monkeypatch.setattr(ops_controller, "_run_playbook", boom)

    ops_controller.handle(_item(), "auton-apps")

    record = store.list()[0]
    assert record["status"] == "needs_human"
    assert record["actions"][0]["action"] == "playbook_error"
    assert "kubectl exploded" in record["actions"][0]["detail"]


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #

def test_build_report_includes_action_and_verification():
    record = {
        "resource": "pod/api-1", "namespace": "auton-apps", "cause": "oom",
        "observation": "OOMKilled", "status": "resolved",
        "verification": "deployment/api ready=1/1",
        "actions": [{"action": "oom_bump_memory", "ok": True,
                     "detail": "raised memory 256Mi -> 320Mi"}],
    }
    text = ops_controller.build_report(record)
    assert "pod/api-1 in auton-apps" in text
    assert "Observed: OOMKilled" in text
    assert "raised memory 256Mi -> 320Mi" in text
    assert "Verified: deployment/api ready=1/1" in text
    assert "resolved automatically" in text


def test_build_report_adds_the_human_task():
    record = {
        "resource": "deployment/api", "namespace": "auton-apps",
        "cause": "image", "observation": "ImagePullBackOff",
        "status": "needs_human", "needs_human": True,
        "actions": [{"action": "report_only", "ok": False, "detail": "x"}],
    }
    text = ops_controller.build_report(record)
    assert "Human action required: Fix the image reference" in text
    assert "waiting for a human" in text


def test_notification_payload_shape():
    record = {
        "id": "inc-1", "severity": "critical", "status": "resolved",
        "namespace": "auton-apps", "resource": "pod/api-1",
        "observation": "OOMKilled", "verification": "ready",
        "report": "text", "created_iso": "2026-01-01T00:00:00Z",
        "actions": [{"action": "oom_bump_memory", "ok": True,
                     "detail": "raised memory"}],
    }
    payload = ops_controller.notification_payload(record)
    assert payload["incident_id"] == "inc-1"
    assert payload["url"].endswith("/incidents/inc-1")
    assert payload["action"] == "raised memory"
    assert set(payload) >= {"severity", "status", "namespace", "resource",
                            "summary", "verification", "report"}


def test_notify_queues_when_the_api_is_down(report, monkeypatch):
    store = report
    record = store.create(namespace="auton-apps", kind="Pod", name="api-1",
                          cause="oom")
    monkeypatch.setattr(ops_controller, "_send_api", lambda rec: False)
    monkeypatch.setattr(ops_controller, "_send_n8n", lambda rec: True)

    ops_controller.notify(record)

    assert [r["id"] for r in store.pending_reports()] == [record["id"]]


def test_flush_pending_delivers_queued_reports(report, monkeypatch):
    store = report
    record = store.create(namespace="auton-apps", kind="Pod", name="api-1",
                          cause="oom")
    store.queue_report(record)
    monkeypatch.setattr(ops_controller, "_send_api", lambda rec: True)
    monkeypatch.setattr(ops_controller, "_send_n8n", lambda rec: True)

    ops_controller.flush_pending()

    assert store.pending_reports() == []


# --------------------------------------------------------------------------- #
# One pass
# --------------------------------------------------------------------------- #

def test_run_once_handles_every_finding(monkeypatch):
    snapshot = _snapshot(pods=[
        _pod("a", ["OOMKilled"]), _pod("b", ["ImagePullBackOff"])])
    monkeypatch.setattr(ops_controller, "NAMESPACES", ("auton-apps",))
    monkeypatch.setattr(ops_controller, "flush_pending", lambda: None)
    monkeypatch.setattr(ops_controller.k8s_tools, "_collect",
                        lambda namespace: (snapshot, None))
    monkeypatch.setattr(ops_controller, "_workload_for",
                        lambda pod, namespace: ("", ""))
    handled = []
    monkeypatch.setattr(ops_controller, "handle",
                        lambda item, ns: handled.append((item, ns)))
    monkeypatch.setattr(ops_controller.STORE, "prune", lambda: 0)

    summary = ops_controller.run_once()

    assert summary["findings"] == 2
    assert summary["errors"] == 0
    assert [item["name"] for item, _ in handled] == ["a", "b"]


def test_run_once_counts_snapshot_errors(monkeypatch):
    monkeypatch.setattr(ops_controller, "NAMESPACES", ("auton-apps",))
    monkeypatch.setattr(ops_controller, "flush_pending", lambda: None)
    monkeypatch.setattr(ops_controller.k8s_tools, "_collect",
                        lambda namespace: (None, "{}"))
    monkeypatch.setattr(ops_controller.STORE, "prune", lambda: 0)

    summary = ops_controller.run_once()

    assert summary["errors"] == 1
    assert summary["findings"] == 0


def test_post_sends_json_and_bearer(monkeypatch):
    seen = {}

    class Response:
        status = 202

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def urlopen(request, timeout=None):
        seen["url"] = request.full_url
        seen["body"] = json.loads(request.data)
        seen["auth"] = request.get_header("Authorization")
        return Response()

    monkeypatch.setattr(ops_controller.urllib.request, "urlopen", urlopen)

    status = ops_controller._post("http://auton:8080/incidents/ingest",
                                  {"id": "inc-1"}, token="secret")

    assert status == 202
    assert seen == {"url": "http://auton:8080/incidents/ingest",
                    "body": {"id": "inc-1"},
                    "auth": "Bearer secret"}
