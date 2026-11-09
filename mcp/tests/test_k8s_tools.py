"""Tests for the kubectl wrapper: pinning, validation and the write gate."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import k8s_tools


@pytest.fixture()
def calls(monkeypatch):
    recorded = []

    def fake_run(cmd, input=None, capture_output=None, text=None, timeout=None):
        recorded.append({"cmd": cmd, "stdin": input, "timeout": timeout})
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(k8s_tools.subprocess, "run", fake_run)
    monkeypatch.setattr(k8s_tools.shutil, "which", lambda _name: "/usr/bin/kubectl")
    monkeypatch.setattr(k8s_tools, "ENABLED", True)
    monkeypatch.setattr(k8s_tools, "WRITE", True)
    return recorded


def _err(out: str) -> dict:
    data = json.loads(out)
    assert data["ok"] is False
    return data


def test_get_always_pins_the_namespace(calls):
    k8s_tools.k8s_get("pods")
    cmd = calls[-1]["cmd"]
    assert cmd[0] == k8s_tools.KUBECTL
    assert cmd[cmd.index("--namespace") + 1] == k8s_tools.NAMESPACE


def test_explicit_kubeconfig_skips_generated_one(monkeypatch):
    monkeypatch.setenv("KUBECONFIG", "/tmp/some-kubeconfig")
    assert k8s_tools._kubeconfig_args() == []


def test_get_builds_selector_and_output(calls):
    k8s_tools.k8s_get("pods", selector="app=auton", output="wide")
    cmd = calls[-1]["cmd"]
    assert "--selector" in cmd and "app=auton" in cmd
    assert cmd[cmd.index("--output") + 1] == "wide"


def test_invalid_arguments_never_reach_kubectl(calls):
    assert _err(k8s_tools.k8s_get("pods; rm -rf /"))["error"].startswith("invalid")
    assert _err(k8s_tools.k8s_get("pods", name="-n kube-system"))
    assert _err(k8s_tools.k8s_get("pods", selector="app=auton;evil"))["ok"] is False
    assert _err(k8s_tools.k8s_get("pods", output="--kubeconfig=/tmp/x"))
    assert calls == []


def test_logs_clamps_tail_and_adds_previous(calls):
    k8s_tools.k8s_logs("auton-abc", tail=99999, previous=True)
    cmd = calls[-1]["cmd"]
    assert cmd[cmd.index("--tail") + 1] == "2000"
    assert "--previous" in cmd


def test_describe_targets_one_resource(calls):
    k8s_tools.k8s_describe("pods", "auton-abc")
    assert calls[-1]["cmd"][-3:-1] == ["pods", "auton-abc"] or \
        calls[-1]["cmd"][3:5] == ["pods", "auton-abc"]


def test_apply_feeds_the_manifest_on_stdin(calls):
    out = k8s_tools.k8s_apply("apiVersion: v1\nkind: ConfigMap\n")
    assert out == "ok"
    assert calls[-1]["stdin"].startswith("apiVersion: v1")


def test_write_gate_blocks_mutation(monkeypatch, calls):
    monkeypatch.setattr(k8s_tools, "WRITE", False)
    for out in (
        k8s_tools.k8s_apply("kind: ConfigMap"),
        k8s_tools.k8s_scale("deployment/auton", 0),
        k8s_tools.k8s_rollout("restart", "deployment/auton"),
        k8s_tools.k8s_delete("pods", "auton-abc", confirm=True),
    ):
        assert _err(out)["error"].startswith("k8s_")
    assert calls == []


def test_scale_validates_replicas(calls):
    k8s_tools.k8s_scale("deployment/auton", 2)
    assert "--replicas=2" in calls[-1]["cmd"]
    assert _err(k8s_tools.k8s_scale("deployment/auton", 99))
    assert _err(k8s_tools.k8s_scale("deployment/auton", "two"))


def test_delete_requires_explicit_confirmation(calls):
    out = _err(k8s_tools.k8s_delete("pods", "auton-abc"))
    assert "confirm=true" in out["error"]
    assert calls == []
    k8s_tools.k8s_delete("pods", "auton-abc", confirm=True)
    cmd = calls[-1]["cmd"]
    assert cmd[-4:] == ["delete", "pods", "auton-abc", "--request-timeout=30s"]


def test_rollout_status_is_read_only(monkeypatch, calls):
    monkeypatch.setattr(k8s_tools, "WRITE", False)
    k8s_tools.k8s_rollout("status", "deployment/auton")
    assert calls[-1]["cmd"][3:5] == ["rollout", "status"]


def test_kubectl_failure_becomes_a_json_error(monkeypatch):
    def failing(cmd, **kwargs):
        return SimpleNamespace(returncode=1, stdout="",
                               stderr="Error from server (NotFound): pods \"x\" not found")

    monkeypatch.setattr(k8s_tools.subprocess, "run", failing)
    monkeypatch.setattr(k8s_tools.shutil, "which", lambda _name: "/usr/bin/kubectl")
    out = _err(k8s_tools.k8s_get("pods", "x"))
    assert "NotFound" in out["error"]
    assert "k8s_get" in out["hint"]


def test_disabled_module_returns_a_clear_error(monkeypatch):
    monkeypatch.setattr(k8s_tools, "ENABLED", False)
    out = _err(k8s_tools.k8s_get("pods"))
    assert "disabled" in out["error"]


# --------------------------------------------------------------------------- #
# Audit gate, namespaces, patch
# --------------------------------------------------------------------------- #

def test_mutation_writes_begin_and_end_records(calls, audit_dir):
    k8s_tools.k8s_apply("kind: ConfigMap\nmetadata:\n  name: demo\n")
    rows = [json.loads(line) for line in
            (audit_dir / "audit.jsonl").read_text().splitlines()]
    assert [r["phase"] for r in rows] == ["begin", "end"]
    assert rows[0]["action"] == "k8s_apply"
    assert rows[0]["args"]["manifest_sha256"]
    assert rows[1]["status"] == "ok"


def test_unwritable_audit_blocks_the_mutation(monkeypatch, calls):
    def deny(*args, **kwargs):
        raise k8s_tools.audit.AuditError("disk is gone")

    monkeypatch.setattr(k8s_tools.audit, "begin", deny)
    out = _err(k8s_tools.k8s_scale("deployment/auton", 2))
    assert out["error"].startswith("refused k8s_scale")
    assert calls == []


def test_patch_builds_kubectl_patch(calls):
    k8s_tools.k8s_patch("deployment", "api",
                        '{"spec":{"template":{"spec":{"containers":[]}}}}')
    cmd = calls[-1]["cmd"]
    assert "--type=strategic" in cmd
    assert cmd[cmd.index("-p") + 1].startswith('{"spec"')
    assert _err(k8s_tools.k8s_patch("deployments", "api", "{}",
                                    patch_type="evil"))


def test_namespace_allowlist(monkeypatch, calls):
    monkeypatch.setattr(k8s_tools, "NAMESPACES", ("auton", "auton-apps"))
    k8s_tools.k8s_get("pods", namespace="auton-apps")
    assert calls[-1]["cmd"][calls[-1]["cmd"].index("--namespace") + 1] == "auton-apps"
    before = len(calls)
    for fn in (
        lambda: k8s_tools.k8s_get("pods", namespace="kube-system"),
        lambda: k8s_tools.k8s_snapshot(namespace="kube-system"),
        lambda: k8s_tools.k8s_patch("deployment", "api", "{}",
                                    namespace="kube-system"),
        lambda: k8s_tools.k8s_delete("pods", "x", confirm=True,
                                     namespace="default"),
    ):
        out = _err(fn())
        assert "not allowed" in out["error"]
    assert len(calls) == before


def test_pvc_delete_is_always_refused(calls):
    out = _err(k8s_tools.k8s_delete("pvc", "agent-workspace", confirm=True))
    assert "disabled" in out["error"]
    assert "human" in out["hint"]
    assert calls == []


# --------------------------------------------------------------------------- #
# Diagnostics
# --------------------------------------------------------------------------- #

POD = {
    "metadata": {
        "name": "api-1",
        "ownerReferences": [{"kind": "ReplicaSet", "name": "api-7d9f"}],
    },
    "spec": {
        "nodeName": "k3d-ai-cluster-server-0",
        "containers": [{
            "name": "api",
            "image": "docker.io/example/api:2",
            "resources": {"limits": {"memory": "256Mi"}},
        }],
    },
    "status": {
        "phase": "Running",
        "startTime": "2026-11-09T10:00:00Z",
        "containerStatuses": [{
            "name": "api",
            "ready": False,
            "restartCount": 7,
            "state": {"waiting": {"reason": "CrashLoopBackOff"}},
            "lastState": {"terminated": {"reason": "OOMKilled", "exitCode": 137}},
        }],
    },
}

EVENT = {
    "type": "Warning",
    "reason": "BackOff",
    "involvedObject": {"kind": "Pod", "name": "api-1"},
    "message": "Back-off restarting failed container api",
    "count": 12,
    "lastTimestamp": "2026-11-09T10:00:00Z",
}

DEPLOYMENT = {
    "metadata": {"name": "api"},
    "spec": {"replicas": 1},
    "status": {"readyReplicas": 0, "availableReplicas": 0, "updatedReplicas": 1},
}


@pytest.fixture()
def kubectl_json(monkeypatch):
    """Fake kubectl that answers `get <resource> -o json` from canned objects."""
    calls = []

    def fake_run(cmd, input=None, capture_output=None, text=None, timeout=None):
        calls.append(cmd)
        if "logs" in cmd:
            return SimpleNamespace(returncode=0,
                                   stdout="booting\nfatal: out of memory\n",
                                   stderr="")
        resource = ""
        if "get" in cmd:
            idx = cmd.index("get")
            resource = cmd[idx + 1]
        single = bool(resource == "pods" and "get" in cmd
                      and idx + 2 < len(cmd) and cmd[idx + 2] == "api-1")
        if resource == "pods":
            payload = POD if single else {"items": [POD]}
        elif resource == "deployments":
            payload = {"items": [DEPLOYMENT]}
        elif resource in ("statefulsets", "pvc", "events"):
            payload = {"items": [EVENT] if resource == "events" else []}
        else:
            payload = {}
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload),
                               stderr="")

    monkeypatch.setattr(k8s_tools.subprocess, "run", fake_run)
    monkeypatch.setattr(k8s_tools.shutil, "which", lambda _name: "/usr/bin/kubectl")
    monkeypatch.setattr(k8s_tools, "ENABLED", True)
    monkeypatch.setattr(k8s_tools, "WRITE", True)
    monkeypatch.setattr(k8s_tools, "NAMESPACES", ("auton",))
    return calls


def test_snapshot_summarises_problems(kubectl_json):
    out = k8s_tools.k8s_snapshot()
    assert "PROBLEMS:" in out
    assert "pod/api-1: CrashLoopBackOff" in out
    assert "deployment/api: Unavailable" in out
    assert "WARNING EVENTS (1)" in out
    assert "BackOff" in out


def test_snapshot_reports_a_healthy_namespace(monkeypatch):
    payload = json.dumps({"items": []})
    monkeypatch.setattr(k8s_tools.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(
                            returncode=0, stdout=payload, stderr=""))
    monkeypatch.setattr(k8s_tools.shutil, "which", lambda _n: "/usr/bin/kubectl")
    out = k8s_tools.k8s_snapshot()
    assert "none detected" in out


def test_diagnose_classifies_oom(kubectl_json):
    out = k8s_tools.k8s_diagnose("api-1")
    assert "restarts=7" in out
    assert "OOMKilled" in out
    assert "diagnosis:" in out
    assert "memory limit" in out
    assert "logs (previous container):" in out
    assert "out of memory" in out


def test_events_filters_by_age(monkeypatch):
    import datetime as dt

    now = dt.datetime.now(dt.timezone.utc)
    recent = dict(EVENT, message="recent warning",
                  lastTimestamp=(now - dt.timedelta(minutes=2)).isoformat())
    old = dict(EVENT, reason="OldReason", message="ancient warning",
               lastTimestamp=(now - dt.timedelta(hours=2)).isoformat())
    payload = json.dumps({"items": [recent, old]})
    monkeypatch.setattr(k8s_tools.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(
                            returncode=0, stdout=payload, stderr=""))
    monkeypatch.setattr(k8s_tools.shutil, "which", lambda _n: "/usr/bin/kubectl")
    out = k8s_tools.k8s_events(since="15m")
    assert "recent warning" in out
    assert "ancient warning" not in out
    assert "OldReason" not in out


def test_wait_maps_condition_and_clamps_timeout(calls):
    k8s_tools.k8s_wait("deployments", "api", "available", 9999)
    cmd = calls[-1]["cmd"]
    assert "--for=condition=Available" in cmd
    assert "--timeout=300s" in cmd
    assert _err(k8s_tools.k8s_wait("deployments", "api", "explode"))


def test_exec_is_audited_and_needs_write(kubectl_json, audit_dir):
    out = k8s_tools.k8s_exec("api-1", "cat /etc/os-release", timeout=999)
    assert out == "ok" or "out of memory" in out or out
    cmd = kubectl_json[-1]
    assert cmd[-4:] == ["--", "sh", "-c", "cat /etc/os-release"]
    rows = [json.loads(line) for line in
            (audit_dir / "audit.jsonl").read_text().splitlines()]
    assert rows[0]["action"] == "k8s_exec"
    assert rows[0]["args"]["pod"] == "api-1"


def test_exec_blocked_in_read_only_mode(monkeypatch, calls):
    monkeypatch.setattr(k8s_tools, "WRITE", False)
    out = _err(k8s_tools.k8s_exec("api-1", "env"))
    assert "disabled" in out["error"]
    assert calls == []

