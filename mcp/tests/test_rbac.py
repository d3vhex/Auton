"""Static checks on the deployed RBAC/manifests — the actual security boundary.

These read the YAML in ../yamls, so a future edit that quietly grants secrets
or PVC deletion fails the suite instead of relying on review.
"""
from __future__ import annotations

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


def test_target_namespaces_match_the_agent_allowlist():
    roles = {doc["metadata"]["namespace"] for doc in _docs("auton-rbac.yaml")
             if doc.get("kind") == "Role"}
    deployment = next(doc for doc in _docs("agent-deployment.yaml")
                      if doc.get("kind") == "Deployment")
    env = {item["name"]: item.get("value")
           for item in deployment["spec"]["template"]["spec"]["containers"][0]["env"]
           if "value" in item}
    allowed = {n.strip() for n in env["AGENT_NAMESPACES"].split(",") if n.strip()}
    assert roles == {"auton", "auton-apps"}
    assert allowed == roles


def test_roles_never_grant_secrets_or_pvc_delete():
    for doc in _docs("auton-rbac.yaml"):
        if doc.get("kind") != "Role":
            continue
        rules = _rules(doc)
        assert not _allows(rules, "secrets", "get"), doc["metadata"]["namespace"]
        assert not _allows(rules, "secrets", "list")
        # PVC delete is refused by RBAC even if the tool guard is bypassed.
        assert not _allows(rules, "persistentvolumeclaims", "delete")
        assert not _allows(rules, "serviceaccounts", "create")
        assert not _allows(rules, "roles", "create")
        assert not _allows(rules, "rolebindings", "create")
        # Evidence collection uses exec, but only the agent's own Role grants
        # it; the ops controller reaches a pod through logs/diagnose instead.
        if doc["metadata"]["name"] == "auton-agent":
            assert _allows(rules, "pods/exec", "create")


def test_cluster_role_is_read_only():
    cluster_roles = [doc for doc in _docs("auton-rbac.yaml")
                     if doc.get("kind") == "ClusterRole"]
    assert cluster_roles, "auton-cluster-reader is missing"
    for doc in cluster_roles:
        for rule in _rules(doc):
            assert set(rule.get("verbs") or []) <= {"get", "list", "watch"}


def test_agent_pod_is_hardened():
    deployment = next(doc for doc in _docs("agent-deployment.yaml")
                      if doc.get("kind") == "Deployment")
    pod = deployment["spec"]["template"]["spec"]
    container = pod["containers"][0]
    ctx = container["securityContext"]
    assert ctx["runAsNonRoot"] is True
    assert ctx["readOnlyRootFilesystem"] is True
    assert ctx["allowPrivilegeEscalation"] is False
    assert ctx["capabilities"]["drop"] == ["ALL"]
    assert any(volume["name"] == "tmp" for volume in pod["volumes"])
    mounts = {mount["name"] for mount in container["volumeMounts"]}
    assert {"workspace", "tmp"} <= mounts
    assert pod["initContainers"], "workspace ownership must be fixed by an init container"


def test_auton_apps_namespace_has_guardrails():
    kinds = {doc["kind"] for doc in _docs("auton-apps.yaml")}
    assert {"Namespace", "ResourceQuota", "LimitRange"} <= kinds


def test_ops_role_is_playbook_shaped():
    docs = _docs("auton-rbac.yaml")
    repair = next(d for d in docs if d["kind"] == "Role"
                  and d["metadata"]["name"] == "auton-ops")
    assert repair["metadata"]["namespace"] == "auton-apps"
    rules = _rules(repair)
    # What the playbooks actually do.
    assert _allows(rules, "pods", "delete")
    assert _allows(rules, "deployments", "patch")
    assert _allows(rules, "deployments/scale", "update")
    assert _allows(rules, "pods/log", "get")
    # And what they must never do.
    assert not _allows(rules, "pods", "create")
    assert not _allows(rules, "configmaps", "patch")
    assert not _allows(rules, "pods/exec", "create")
    assert not _allows(rules, "persistentvolumeclaims", "delete")


def test_ops_reader_role_is_read_only():
    reader = next(d for d in _docs("auton-rbac.yaml")
                  if d["kind"] == "Role"
                  and d["metadata"]["name"] == "auton-ops-reader")
    assert reader["metadata"]["namespace"] == "auton"
    for rule in _rules(reader):
        assert set(rule.get("verbs") or []) <= {"get", "list", "watch"}


def test_ops_identity_is_bound_in_both_namespaces_and_cluster_read():
    docs = _docs("auton-rbac.yaml")
    subjects = {
        (doc["metadata"]["name"], doc["metadata"].get("namespace"),
         tuple((s.get("name"), s.get("namespace"))
               for s in doc.get("subjects") or []))
        for doc in docs
        if doc.get("kind") in ("RoleBinding", "ClusterRoleBinding")
    }
    assert ("auton-ops", "auton-apps",
            (("auton-ops", "auton"),)) in subjects
    assert ("auton-ops-reader", "auton",
            (("auton-ops", "auton"),)) in subjects
    assert ("auton-ops-cluster-reader", None,
            (("auton-ops", "auton"),)) in subjects


def test_ops_deployment_uses_its_own_identity_and_is_hardened():
    deployment = next(doc for doc in _docs("auton-ops-deployment.yaml")
                      if doc.get("kind") == "Deployment")
    assert deployment["metadata"]["name"] == "auton-ops"
    assert deployment["metadata"]["namespace"] == "auton"
    pod = deployment["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == "auton-ops"
    container = pod["containers"][0]
    assert container["command"][-1] == "ops_controller.py"
    ctx = container["securityContext"]
    assert ctx["runAsNonRoot"] is True
    assert ctx["readOnlyRootFilesystem"] is True
    assert ctx["allowPrivilegeEscalation"] is False
    assert ctx["capabilities"]["drop"] == ["ALL"]
    mounts = {mount["name"]: mount for mount in container["volumeMounts"]}
    assert mounts["workspace"]["mountPath"] == "/workspace"
    assert "tmp" in mounts
    env = {item["name"]: item for item in container["env"]}
    assert env["AGENT_OPS_NAMESPACES"]["value"] == "auton-apps"
    assert env["AGENT_AUDIT_DIR"]["value"] == "/workspace/.ops"
    # The API token is a Secret reference, never a literal.
    assert env["AGENT_API_TOKEN"]["valueFrom"]["secretKeyRef"]["name"] == \
        "auton-api-token"
    assert "value" not in env["AGENT_API_TOKEN"]
    # No model mount: the controller never calls an LLM.
    assert not any(name.startswith("model-") for name in mounts)
    assert pod["initContainers"], "workspace ownership must be fixed by an init container"
