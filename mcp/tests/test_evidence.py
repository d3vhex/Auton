"""Tests for the structured Evidence Collector.

The important guarantees are: secrets are never read, unavailable evidence is
marked as unavailable (never invented), and the document has the shape the
platform spec asks for.
"""
from __future__ import annotations

import pytest

import evidence


def _pod(reasons=None, termination=None, exit_code=None, restarts=0,
         ready=False, node="node-1", owner=True):
    """A minimal Pod object as the API server would return it."""
    container_status = {
        "name": "api",
        "ready": ready,
        "restartCount": restarts,
        "image": "app:1",
        "imageID": "docker.io/library/app@sha256:abc",
        "state": {"waiting": {"reason": (reasons or ["CrashLoopBackOff"])[0]}},
    }
    if termination:
        container_status["lastState"] = {
            "terminated": {"reason": termination, "exitCode": exit_code,
                           "startedAt": "2026-01-01T00:00:00Z",
                           "finishedAt": "2026-01-01T00:00:10Z"}}
    pod = {
        "metadata": {
            "name": "api-1",
            "namespace": "auton-apps",
            "uid": "uid-abc",
            "ownerReferences": ([{"kind": "ReplicaSet", "name": "api-1-rs",
                                  "uid": "rs-uid", "controller": True}]
                                if owner else []),
        },
        "spec": {
            "nodeName": node,
            "containers": [{
                "name": "api",
                "image": "app:1",
                "resources": {"requests": {"memory": "256Mi", "cpu": "100m"},
                              "limits": {"memory": "512Mi", "cpu": "500m"}},
                "livenessProbe": {"httpGet": {"path": "/healthz", "port": 8080},
                                  "initialDelaySeconds": 10,
                                  "periodSeconds": 5,
                                  "failureThreshold": 3},
            }],
            "imagePullSecrets": [{"name": "registry-creds"}],
        },
        "status": {
            "phase": "Running",
            "containerStatuses": [container_status],
            "conditions": [{"type": "Ready", "status": "True"}],
        },
    }
    return pod


@pytest.fixture()
def fake_k8s(monkeypatch):
    """Patch every Kubernetes read the collector performs."""
    state = {"pod": _pod(), "events": [], "logs": "", "previous_logs": "",
             "node": None, "replicaset": None, "deployment": None,
             "errors": []}

    def query(args, namespace, timeout=30):
        verb = args[0] if args else ""
        kind = args[1] if len(args) > 1 else ""
        if verb == "get" and kind == "pod":
            if state["pod"] is None:
                return None, {"ok": False, "error": "pod not found"}
            return state["pod"], None
        if verb == "get" and kind == "replicaset":
            if state["replicaset"] is None:
                return None, {"ok": False, "error": "not found"}
            return state["replicaset"], None
        if verb == "get" and kind == "deployment":
            if state["deployment"] is None:
                return None, {"ok": False, "error": "not found"}
            return state["deployment"], None
        if verb == "get" and kind == "events":
            return {"items": state["events"]}, None
        if verb == "get" and kind == "node":
            if state["node"] is None:
                return None, {"ok": False, "error": "node not found"}
            return state["node"], None
        return None, {"ok": False, "error": f"unexpected call: {args}"}

    def execute(args, stdin=None, timeout=30, namespace=None):
        if "--previous" in args:
            if not state["previous_logs"]:
                return "", {"ok": False, "error": "no previous logs"}
            return state["previous_logs"], None
        if "logs" in args:
            if state["logs"] is None:
                return "", {"ok": False, "error": "logs are not available"}
            return state["logs"], None
        return "", {"ok": False, "error": "unexpected call"}

    monkeypatch.setattr(evidence, "_query", query)
    monkeypatch.setattr(evidence.k8s_tools, "_execute", execute)
    monkeypatch.setattr(evidence, "_node_metrics",
                        lambda namespace, node: ({"nodes": [], "pods": []}, False))
    return state


# --------------------------------------------------------------------------- #
# Shape
# --------------------------------------------------------------------------- #

def test_document_has_the_spec_shape(fake_k8s):
    document = evidence.collect("auton-apps", "Pod", "api-1")
    assert document["resource"] == {
        "kind": "Pod", "name": "api-1", "namespace": "auton-apps",
        "uid": "uid-abc", "labels": {}}
    assert document["problem"] == "CrashLoopBackOff"
    assert document["container"]["name"] == "api"
    assert document["container"]["restart_count"] == 0
    assert document["resources"]["memory_request"] == "256Mi"
    assert document["resources"]["memory_limit"] == "512Mi"
    assert "logs_available" in document["evidence"]
    assert "events_available" in document["evidence"]
    assert "node_metrics_available" in document["evidence"]


def test_container_state_is_reported(fake_k8s):
    fake_k8s["pod"] = _pod(termination="OOMKilled", exit_code=137, restarts=4)
    document = evidence.collect("auton-apps", "Pod", "api-1")
    assert document["container"]["restart_count"] == 4
    assert document["container"]["last_termination_reason"] == "OOMKilled"
    assert document["container"]["exit_code"] == 137


def test_probes_are_summarised_without_their_command(fake_k8s):
    document = evidence.collect("auton-apps", "Pod", "api-1")
    liveness = document["probes"]["liveness"]
    assert liveness["configured"] is True
    assert liveness["path"] == "/healthz"
    assert liveness["failure_threshold"] == 3
    assert "command" not in liveness


def test_owner_chain_is_resolved(fake_k8s):
    fake_k8s["replicaset"] = {
        "metadata": {"name": "api-1-rs", "namespace": "auton-apps",
                     "ownerReferences": [{"kind": "Deployment", "name": "api",
                                          "uid": "dep-uid", "controller": True}]},
        "spec": {}, "status": {}}
    document = evidence.collect("auton-apps", "Pod", "api-1")
    assert document["owner"]["kind"] == "Deployment"
    assert document["owner"]["name"] == "api"
    assert len(document["owner"]["chain"]) == 2


def test_events_are_collected(fake_k8s):
    fake_k8s["events"] = [{"reason": "BackOff", "message": "Back-off restarting",
                           "type": "Warning", "count": 3,
                           "lastTimestamp": "2026-01-01T00:00:00Z"}]
    document = evidence.collect("auton-apps", "Pod", "api-1")
    assert document["events"][0]["reason"] == "BackOff"
    assert document["events"][0]["count"] == 3
    assert document["evidence"]["events_available"] is True


def test_logs_are_collected(fake_k8s):
    fake_k8s["logs"] = "starting up"
    fake_k8s["previous_logs"] = "crashed: boom"
    document = evidence.collect("auton-apps", "Pod", "api-1")
    assert document["logs"] == "starting up"
    assert document["previous_logs"] == "crashed: boom"
    assert document["evidence"]["logs_available"] is True
    assert document["evidence"]["previous_logs_available"] is True


# --------------------------------------------------------------------------- #
# Secrets are never read
# --------------------------------------------------------------------------- #

def test_no_secret_object_is_ever_requested(fake_k8s):
    requested = []
    original = evidence._query

    def spy(args, namespace, timeout=30):
        requested.append(args)
        return original(args, namespace, timeout)

    evidence._query = spy
    try:
        evidence.collect("auton-apps", "Pod", "api-1")
    finally:
        evidence._query = original
    for args in requested:
        assert "secret" not in " ".join(args).lower()


def test_only_the_presence_of_a_pull_secret_is_reported(fake_k8s):
    document = evidence.collect("auton-apps", "Pod", "api-1")
    assert document["container"]["image_pull_secret_configured"] is True
    # The name of the pull secret must not leak into the evidence.
    assert "registry-creds" not in str(document)


def test_the_labels_are_collected_for_the_policy_engine(fake_k8s):
    fake_k8s["pod"]["metadata"]["labels"] = {"app": "api",
                                              "auton.io/protect": "true"}
    document = evidence.collect("auton-apps", "Pod", "api-1")
    assert document["resource"]["labels"]["app"] == "api"
    assert document["resource"]["labels"]["auton.io/protect"] == "true"


def test_the_document_contains_no_credential_like_values(fake_k8s):
    fake_k8s["pod"]["spec"]["containers"][0]["env"] = [
        {"name": "PASSWORD", "value": "hunter2"}]
    document = evidence.collect("auton-apps", "Pod", "api-1")
    assert "hunter2" not in str(document)
    assert "PASSWORD" not in str(document)


# --------------------------------------------------------------------------- #
# Unavailable evidence is marked, never invented
# --------------------------------------------------------------------------- #

def test_a_missing_pod_is_reported_as_an_error(fake_k8s):
    fake_k8s["pod"] = None
    document = evidence.collect("auton-apps", "Pod", "api-1")
    assert document["evidence"]["pod_available"] is False
    assert document["errors"]
    assert document["container"] == {}
    assert document["resources"] == {}


def test_missing_logs_are_marked_unavailable(fake_k8s):
    fake_k8s["logs"] = None
    fake_k8s["previous_logs"] = None
    document = evidence.collect("auton-apps", "Pod", "api-1")
    assert document["evidence"]["logs_available"] is False
    assert document["evidence"]["previous_logs_available"] is False
    assert document["logs"] == ""


def test_an_empty_but_readable_log_is_still_available(fake_k8s):
    fake_k8s["logs"] = ""
    document = evidence.collect("auton-apps", "Pod", "api-1")
    assert document["evidence"]["logs_available"] is True
    assert document["logs"] == ""


def test_a_missing_node_is_marked_unavailable(fake_k8s):
    fake_k8s["node"] = None
    document = evidence.collect("auton-apps", "Pod", "api-1")
    assert document["evidence"]["node_available"] is False
    assert document["node"]["available"] is False


def test_node_pressure_is_reported(fake_k8s):
    fake_k8s["node"] = {
        "metadata": {"name": "node-1"},
        "status": {
            "allocatable": {"memory": "8Gi", "cpu": "4"},
            "capacity": {"memory": "8Gi", "cpu": "4"},
            "conditions": [
                {"type": "Ready", "status": "True", "reason": "", "message": ""},
                {"type": "MemoryPressure", "status": "False", "reason": "",
                 "message": ""},
            ]}}
    document = evidence.collect("auton-apps", "Pod", "api-1")
    assert document["evidence"]["node_available"] is True
    assert document["node"]["allocatable"]["memory"] == "8Gi"
    assert document["node"]["pressure"]["MemoryPressure"] == "False"


# --------------------------------------------------------------------------- #
# Summary rendering
# --------------------------------------------------------------------------- #

def test_summary_mentions_the_important_facts(fake_k8s):
    fake_k8s["pod"] = _pod(termination="OOMKilled", exit_code=137)
    document = evidence.collect("auton-apps", "Pod", "api-1")
    text = evidence.summarize(document)
    assert "Pod/api-1" in text
    assert "OOMKilled" in text
    assert "512Mi" in text
    assert "available:" in text


def test_summary_of_an_empty_document_is_empty():
    assert evidence.summarize({}) == ""
