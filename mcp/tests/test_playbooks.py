"""Tests for the deterministic repair playbooks (no cluster needed)."""
from __future__ import annotations

import json

import pytest

import k8s_tools
import playbooks

NOT_FOUND = json.dumps({"ok": False, "error": "Error from server (NotFound): pods \"x\" not found"})


def _deployment(name="api", replicas=1, available=1, ready=1,
                container="api", limits=None, requests=None, labels=None):
    return {
        "metadata": {"name": name, "labels": labels or {}},
        "spec": {
            "replicas": replicas,
            "template": {"spec": {"containers": [{
                "name": container,
                "resources": {"limits": limits or {},
                              "requests": requests or {}},
            }]}},
        },
        "status": {"availableReplicas": available, "readyReplicas": ready},
    }


def _pod(name="api-1", owner="api-rs", labels=None):
    return {
        "metadata": {
            "name": name,
            "labels": labels or {},
            "ownerReferences": ([{"kind": "ReplicaSet", "name": owner}]
                                if owner else []),
        },
        "spec": {"containers": [{"name": "api"}]},
    }


def _rs(name="api-rs", owner="api"):
    return {"metadata": {"name": name,
                         "ownerReferences": [{"kind": "Deployment",
                                              "name": owner}]}}


class Cluster:
    """A tiny fake kubectl: documents keyed by (resource, name)."""

    def __init__(self):
        self.docs: dict[tuple[str, str], dict] = {}
        self.calls: list[tuple] = []

    def add(self, resource, name, doc):
        self.docs[(resource, name)] = doc

    def get(self, resource, name, namespace):
        doc = self.docs.get((resource, name))
        if doc is None:
            return None, NOT_FOUND
        return doc, ""

    def drop(self, resource, name):
        self.docs.pop((resource, name), None)


@pytest.fixture()
def cluster(monkeypatch):
    fake = Cluster()
    monkeypatch.setenv("AGENT_OPS_NAMESPACES", "auton-apps")
    monkeypatch.setattr(k8s_tools, "NAMESPACES", ("auton", "auton-apps"))
    monkeypatch.setattr(playbooks, "_get", fake.get)

    def patch(resource, name, body, patch_type="strategic", namespace=""):
        fake.calls.append(("patch", resource, name, json.loads(body), namespace))
        return "patched"

    def scale(deployment, replicas, namespace=""):
        fake.calls.append(("scale", deployment, replicas, namespace))
        dep = fake.docs.get(("deployment", deployment.split("/")[-1]))
        if dep is not None:
            dep["spec"]["replicas"] = replicas
            dep["status"]["availableReplicas"] = replicas
            dep["status"]["readyReplicas"] = replicas
        return "scaled"

    def rollout(action, deployment="", namespace=""):
        fake.calls.append(("rollout", action, deployment, namespace))
        return "rolled back"

    def delete(resource, name, confirm=False, namespace=""):
        fake.calls.append(("delete", resource, name, namespace))
        fake.drop("pod", name)
        return "deleted"

    def wait(resource, name, condition="ready", timeout=60, namespace=""):
        fake.calls.append(("wait", resource, name, condition))
        return "(ok, condition met)"

    def audited(action, argv, **kwargs):
        fake.calls.append(("audited", action, argv, kwargs))
        if action == "k8s_delete":
            fake.drop("pod", argv[2])
        return "deleted"

    monkeypatch.setattr(k8s_tools, "k8s_patch", patch)
    monkeypatch.setattr(k8s_tools, "k8s_scale", scale)
    monkeypatch.setattr(k8s_tools, "k8s_rollout", rollout)
    monkeypatch.setattr(k8s_tools, "k8s_delete", delete)
    monkeypatch.setattr(k8s_tools, "k8s_wait", wait)
    monkeypatch.setattr(k8s_tools, "_audited", audited)
    return fake


# --------------------------------------------------------------------------- #
# Memory helpers
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("value,expected", [
    ("256Mi", 256 * 1024 ** 2),
    ("1Gi", 1024 ** 3),
    ("512M", 512 * 1000 ** 2),
    ("1.5Gi", int(1.5 * 1024 ** 3)),
    ("1024", 1024),
    ("", None),
    ("lots", None),
])
def test_parse_memory(value, expected):
    assert playbooks.parse_memory(value) == expected


@pytest.mark.parametrize("value,expected", [
    (2 * 1024 ** 3, "2Gi"),
    (768 * 1024 ** 2, "768Mi"),
    (320 * 1024 ** 2, "320Mi"),
    (2048, "2Ki"),
    (500, "500"),
])
def test_format_memory(value, expected):
    assert playbooks.format_memory(value) == expected


# --------------------------------------------------------------------------- #
# OOM: raise the limit, verify the rollout
# --------------------------------------------------------------------------- #

def test_fix_oom_raises_limit_by_25_percent(cluster):
    cluster.add("pod", "api-1", _pod())
    cluster.add("replicaset", "api-rs", _rs())
    cluster.add("deployment", "api", _deployment(
        limits={"memory": "256Mi"}, requests={"memory": "128Mi"}))

    result = playbooks.fix_oom("api-1", "auton-apps")

    assert result["ok"] is True
    assert result["needs_human"] is False
    assert "256Mi" in result["detail"] and "320Mi" in result["detail"]
    patch = next(c for c in cluster.calls if c[0] == "patch")
    _, resource, name, body, namespace = patch
    assert (resource, name, namespace) == ("deployment", "api", "auton-apps")
    resources = body["spec"]["template"]["spec"]["containers"][0]["resources"]
    assert resources["limits"]["memory"] == "320Mi"
    # The request is only touched when it would exceed the new limit.
    assert resources["requests"]["memory"] == "128Mi"
    assert any(c[0] == "wait" and c[3] == "available" for c in cluster.calls)


def test_fix_oom_starts_from_256mi_without_a_limit(cluster):
    cluster.add("pod", "api-1", _pod())
    cluster.add("replicaset", "api-rs", _rs())
    cluster.add("deployment", "api", _deployment())
    result = playbooks.fix_oom("api-1", "auton-apps")
    assert result["ok"] is True
    assert "no memory limit" in result["detail"]
    assert "320Mi" in result["detail"]


def test_fix_oom_raises_request_when_it_exceeds_the_new_limit(cluster):
    cluster.add("pod", "api-1", _pod())
    cluster.add("replicaset", "api-rs", _rs())
    cluster.add("deployment", "api", _deployment(
        limits={"memory": "256Mi"}, requests={"memory": "400Mi"}))
    playbooks.fix_oom("api-1", "auton-apps")
    patch = next(c for c in cluster.calls if c[0] == "patch")
    resources = patch[3]["spec"]["template"]["spec"]["containers"][0]["resources"]
    assert resources["limits"]["memory"] == "320Mi"
    assert resources["requests"]["memory"] == "320Mi"


def test_fix_oom_refuses_at_the_memory_cap(cluster, monkeypatch):
    monkeypatch.setattr(playbooks, "MAX_MEMORY", "256Mi")
    cluster.add("pod", "api-1", _pod())
    cluster.add("replicaset", "api-rs", _rs())
    cluster.add("deployment", "api", _deployment(limits={"memory": "256Mi"}))
    result = playbooks.fix_oom("api-1", "auton-apps")
    assert result["ok"] is False
    assert result["needs_human"] is True
    assert "cap" in result["detail"]
    assert not [c for c in cluster.calls if c[0] == "patch"]


def test_fix_oom_skips_protected_deployment(cluster):
    cluster.add("pod", "api-1", _pod())
    cluster.add("replicaset", "api-rs", _rs())
    cluster.add("deployment", "api", _deployment(
        limits={"memory": "256Mi"},
        labels={"auton.io/protect": "true"}))
    result = playbooks.fix_oom("api-1", "auton-apps")
    assert result["needs_human"] is True
    assert "protect" in result["detail"]
    assert not [c for c in cluster.calls if c[0] == "patch"]


def test_fix_oom_only_repairs_deployments(cluster):
    cluster.add("pod", "api-1", _pod(owner=""))
    result = playbooks.fix_oom("api-1", "auton-apps")
    assert result["needs_human"] is True
    assert "only Deployments" in result["detail"]


def test_playbooks_refuse_namespaces_outside_the_ops_allowlist(cluster):
    result = playbooks.fix_oom("api-1", "auton")
    assert result["ok"] is False
    assert "outside AGENT_OPS_NAMESPACES" in result["detail"]
    assert cluster.calls == []


def test_playbooks_refuse_namespaces_the_tools_cannot_target(cluster, monkeypatch):
    monkeypatch.setattr(k8s_tools, "NAMESPACES", ("auton",))
    result = playbooks.fix_oom("api-1", "auton-apps")
    assert result["ok"] is False
    assert "not allowed" in result["detail"]


# --------------------------------------------------------------------------- #
# Stuck Terminating pod
# --------------------------------------------------------------------------- #

def test_fix_stuck_force_deletes_and_verifies(cluster):
    cluster.add("pod", "api-1", _pod())
    result = playbooks.fix_stuck("api-1", "auton-apps", age="12m")
    assert result["ok"] is True
    audited = next(c for c in cluster.calls if c[0] == "audited")
    assert "--force" in audited[2]
    assert "--grace-period=0" in audited[2]
    assert "12m" in audited[3]["plan"]


def test_fix_stuck_treats_already_gone_as_success(cluster):
    result = playbooks.fix_stuck("api-1", "auton-apps")
    assert result["ok"] is True
    assert "already gone" in result["detail"]


def test_fix_stuck_skips_protected_pod(cluster):
    cluster.add("pod", "api-1", _pod(labels={"auton.io/protect": "true"}))
    result = playbooks.fix_stuck("api-1", "auton-apps")
    assert result["needs_human"] is True
    assert not [c for c in cluster.calls if c[0] == "audited"]


# --------------------------------------------------------------------------- #
# CrashLoopBackOff: config evidence goes to a human
# --------------------------------------------------------------------------- #

def test_fix_crashloop_reports_configuration_errors(cluster):
    cluster.add("pod", "api-1", _pod())
    result = playbooks.fix_crashloop(
        "api-1", "auton-apps",
        evidence="dial tcp 10.0.0.2:5432: connect: connection refused")
    assert result["ok"] is False
    assert result["needs_human"] is True
    assert "configuration" in result["detail"]
    assert not [c for c in cluster.calls if c[0] == "delete"]


def test_fix_crashloop_recreates_a_pod_owned_by_a_deployment(cluster):
    cluster.add("pod", "api-1", _pod())
    cluster.add("replicaset", "api-rs", _rs())
    cluster.add("deployment", "api", _deployment())
    result = playbooks.fix_crashloop("api-1", "auton-apps",
                                     evidence="exit code 1")
    assert result["ok"] is True
    assert "ReplicaSet recreated it" in result["detail"]
    assert any(c[0] == "delete" for c in cluster.calls)


def test_fix_crashloop_will_not_delete_a_bare_pod(cluster):
    cluster.add("pod", "api-1", _pod(owner=""))
    result = playbooks.fix_crashloop("api-1", "auton-apps", evidence="exit 1")
    assert result["needs_human"] is True
    assert not [c for c in cluster.calls if c[0] == "delete"]


# --------------------------------------------------------------------------- #
# Rollout undo and replica restore
# --------------------------------------------------------------------------- #

def test_fix_rollout_undoes_and_verifies(cluster):
    cluster.add("deployment", "api", _deployment())
    result = playbooks.fix_rollout("api", "auton-apps", reason="new image")
    assert result["ok"] is True
    assert any(c[0] == "rollout" and c[1] == "undo" for c in cluster.calls)
    assert "new image" in result["detail"]


def test_fix_rollout_reports_when_still_unavailable(cluster):
    cluster.add("deployment", "api", _deployment(available=0, ready=0))
    result = playbooks.fix_rollout("api", "auton-apps")
    assert result["ok"] is False
    assert result["needs_human"] is True


def test_fix_replicas_scales_a_zeroed_deployment(cluster):
    cluster.add("deployment", "api", _deployment(replicas=0, available=0, ready=0))
    result = playbooks.fix_replicas("api", "auton-apps")
    assert result["ok"] is True
    assert any(c[0] == "scale" and c[2] == 1 for c in cluster.calls)


def test_fix_replicas_refuses_when_already_scaled(cluster):
    cluster.add("deployment", "api", _deployment(replicas=2, available=0, ready=0))
    result = playbooks.fix_replicas("api", "auton-apps")
    assert result["needs_human"] is True
    assert not [c for c in cluster.calls if c[0] == "scale"]
