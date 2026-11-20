"""Tests for the executor write scope (Phase 4).

The LLM executor (`auton-ops`) is read-only by default: it may read every
namespace it is allowed to see, but it may not change anything. That is the
safer default for the component that talks to a 7B model — a hallucinated
`kubectl patch` must not become a real change.

These tests pin both halves of the boundary:

* the **RBAC** — the `auton-agent` Role grants get/list/watch only, and the
  write verbs live in a separate, opt-in manifest;
* the **code** — the k8s tools refuse every mutation in read-only mode, and
  the repair path (the worker) is not affected by the executor's scope.
"""
from __future__ import annotations

import importlib
import os
import pathlib

import pytest

yaml = pytest.importorskip("yaml")

ROOT = pathlib.Path(__file__).resolve().parents[2]
YAMLS = ROOT / "yamls"


def _docs(name: str) -> list[dict]:
    return [doc for doc in yaml.safe_load_all((YAMLS / name).read_text())
            if doc]


def _rules(doc: dict) -> list[dict]:
    return doc.get("rules") or []


def _allows(rules: list[dict], resource: str, verb: str) -> bool:
    for rule in rules:
        if resource not in (rule.get("resources") or []):
            continue
        if "*" in (rule.get("verbs") or []) or verb in (rule.get("verbs") or []):
            return True
    return False


def _scope(**env: str) -> tuple[bool, str | None]:
    """Import k8s_tools with a fresh environment and capture its write gate.

    `k8s_tools` reads the environment at import time, and reloading it mutates
    the module in place — so the values are captured *inside* the window where
    the environment is in effect, before the module is restored.
    """
    saved = {key: os.environ.get(key) for key in
             ("AGENT_K8S_WRITE", "AGENT_K8S_ENABLED")}
    for key, value in env.items():
        os.environ[key] = value
    try:
        module = importlib.reload(importlib.import_module("k8s_tools"))
        write = module.WRITE
        refused = module._require_write("k8s_patch")
        return write, refused
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        importlib.reload(importlib.import_module("k8s_tools"))


# --------------------------------------------------------------------------- #
# The RBAC boundary
# --------------------------------------------------------------------------- #

class TestRbac:
    def test_the_agent_role_is_read_only(self):
        for doc in _docs("auton-rbac.yaml"):
            if doc.get("kind") != "Role":
                continue
            if doc["metadata"]["name"] != "auton-agent":
                continue
            for rule in _rules(doc):
                assert set(rule.get("verbs") or []) <= {"get", "list", "watch"}, \
                    f"{doc['metadata']['namespace']}: {rule}"

    def test_the_write_verbs_live_in_a_separate_opt_in_manifest(self):
        docs = _docs("auton-rbac-write.yaml")
        roles = [d for d in docs if d["kind"] == "Role"]
        bindings = [d for d in docs if d["kind"] == "RoleBinding"]
        assert {r["metadata"]["namespace"] for r in roles} == {"auton", "auton-apps"}
        assert {b["metadata"]["namespace"] for b in bindings} == \
            {"auton", "auton-apps"}
        # The binding must point at the write role and the executor identity.
        for binding in bindings:
            assert binding["roleRef"]["name"] == "auton-agent-write"
            subjects = binding["subjects"]
            assert len(subjects) == 1
            assert subjects[0]["name"] == "auton-agent"
            assert subjects[0]["namespace"] == "auton"

    def test_the_write_role_grants_the_expected_verbs(self):
        role = next(d for d in _docs("auton-rbac-write.yaml")
                    if d["kind"] == "Role")
        rules = _rules(role)
        assert _allows(rules, "pods", "delete")
        assert _allows(rules, "deployments", "patch")
        assert _allows(rules, "deployments/scale", "update")
        assert _allows(rules, "pods/exec", "create")
        # And still never the dangerous ones.
        assert not _allows(rules, "persistentvolumeclaims", "delete")
        assert not _allows(rules, "secrets", "get")
        assert not _allows(rules, "roles", "create")
        assert not _allows(rules, "rolebindings", "create")
        assert not _allows(rules, "serviceaccounts", "create")

    def test_the_read_only_role_has_no_exec(self):
        for doc in _docs("auton-rbac.yaml"):
            if doc.get("kind") != "Role":
                continue
            if doc["metadata"]["name"] != "auton-agent":
                continue
            assert not _allows(_rules(doc), "pods/exec", "create")

    def test_the_cluster_reader_stays_read_only(self):
        for doc in _docs("auton-rbac.yaml"):
            if doc.get("kind") != "ClusterRole":
                continue
            for rule in _rules(doc):
                assert set(rule.get("verbs") or []) <= {"get", "list", "watch"}


# --------------------------------------------------------------------------- #
# The manifests
# --------------------------------------------------------------------------- #

class TestManifests:
    def test_the_executor_is_deployed_read_only(self):
        deployment = next(d for d in _docs("auton-ops-deployment.yaml")
                          if d["kind"] == "Deployment")
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        env = {item["name"]: item.get("value")
               for item in container["env"] if "value" in item}
        assert env["AGENT_K8S_WRITE"] == "0"
        assert env["AGENT_K8S_ENABLED"] == "1"

    def test_the_api_is_a_read_only_producer(self):
        deployment = next(d for d in _docs("agent-deployment.yaml")
                          if d["kind"] == "Deployment")
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        env = {item["name"]: item.get("value")
               for item in container["env"] if "value" in item}
        assert env["AGENT_K8S_WRITE"] == "0"

    def test_the_worker_keeps_its_write_rights(self):
        # The repair path must not depend on the executor's scope: the
        # deterministic, policy-gated worker is the only component that
        # mutates a workload, and it keeps its own identity.
        deployment = next(d for d in _docs("auton-worker-deployment.yaml")
                          if d["kind"] == "Deployment")
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        env = {item["name"]: item.get("value")
               for item in container["env"] if "value" in item}
        assert env["AGENT_K8S_WRITE"] == "1"


# --------------------------------------------------------------------------- #
# The code gate
# --------------------------------------------------------------------------- #

class TestWriteGate:
    def test_read_only_by_default(self):
        write, _ = _scope(AGENT_K8S_WRITE="0")
        assert write is False

    def test_writes_are_refused_in_read_only_mode(self):
        _, blocked = _scope(AGENT_K8S_WRITE="0")
        assert blocked is not None
        assert "read-only" in blocked
        # The hint points at the operator-level switch, not at "edit the pod".
        assert "set-agent-writes.sh" in blocked

    def test_writes_are_allowed_when_the_pod_opts_in(self):
        write, blocked = _scope(AGENT_K8S_WRITE="1")
        assert write is True
        assert blocked is None

    def test_an_unset_flag_is_read_only(self):
        write, _ = _scope(AGENT_K8S_WRITE="")
        assert write is False
