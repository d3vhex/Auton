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


def test_only_the_api_identity_may_touch_secrets_and_only_the_model_one():
    """The single secrets exception: PATCH the provider Secret, nothing else."""
    for doc in _docs("auton-rbac.yaml"):
        if doc.get("kind") != "Role":
            continue
        secret_rules = [rule for rule in _rules(doc)
                        if "secrets" in (rule.get("resources") or [])]
        if not secret_rules:
            continue
        assert doc["metadata"]["name"] == "auton-api", doc["metadata"]["name"]
        for rule in secret_rules:
            assert set(rule.get("verbs") or []) <= {"patch"}
            assert rule.get("resourceNames") == ["auton-model"]


def test_api_role_is_provider_settings_shaped():
    role = next(d for d in _docs("auton-rbac.yaml")
                if d.get("kind") == "Role"
                and d["metadata"]["name"] == "auton-api")
    assert role["metadata"]["namespace"] == "auton"
    rules = _rules(role)
    configmap_rules = [r for r in rules if "configmaps" in (r.get("resources") or [])]
    assert configmap_rules, "the API must be able to write the provider ConfigMap"
    # Only two named ConfigMaps are writable: the provider settings and the
    # repair policy. Everything else stays out of reach.
    writable = set()
    for rule in configmap_rules:
        assert set(rule.get("verbs") or []) <= {"patch"}
        names = set(rule.get("resourceNames") or [])
        assert names <= {"auton-model-config", "auton-policy"}, names
        writable |= names
    assert writable == {"auton-model-config", "auton-policy"}
    # Nothing else: no workloads, no exec, no PVCs.
    resources = {res for rule in rules for res in (rule.get("resources") or [])}
    assert resources == {"configmaps", "secrets"}


def test_api_identity_exists_and_is_bound_in_auton():
    docs = _docs("auton-rbac.yaml")
    accounts = {doc["metadata"]["name"] for doc in docs
                if doc.get("kind") == "ServiceAccount"
                and doc["metadata"].get("namespace") == "auton"}
    assert {"auton-agent", "auton-api", "auton-scanner",
            "auton-worker"} <= accounts

    bound = {
        (doc["metadata"]["name"], doc["metadata"].get("namespace"),
         tuple((s.get("name"), s.get("namespace"))
               for s in doc.get("subjects") or []))
        for doc in docs
        if doc.get("kind") == "RoleBinding"
    }
    assert ("auton-api", "auton", (("auton-api", "auton"),)) in bound


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


def test_api_deployment_is_a_producer_with_the_narrow_identity():
    deployment = next(doc for doc in _docs("agent-deployment.yaml")
                      if doc.get("kind") == "Deployment")
    pod = deployment["spec"]["template"]["spec"]
    # The API no longer runs kubectl: the agent loop moved to auton-ops.
    assert pod["serviceAccountName"] == "auton-api"
    container = pod["containers"][0]
    env = {item["name"]: item for item in container["env"]}
    assert env["AGENT_QUEUE"]["value"] == "redis"
    assert env["AGENT_REDIS_URL"]["value"].startswith("redis://redis")
    assert env["AGENT_QUEUE_PREFIX"]["value"] == "auton"


def test_ops_executor_runs_agent_worker_with_the_agent_identity():
    deployment = next(doc for doc in _docs("auton-ops-deployment.yaml")
                      if doc.get("kind") == "Deployment")
    assert deployment["metadata"]["name"] == "auton-ops"
    assert deployment["spec"]["replicas"] == 1
    pod = deployment["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == "auton-agent"
    container = pod["containers"][0]
    assert container["command"][-1] == "agent_worker.py"
    ctx = container["securityContext"]
    assert ctx["runAsNonRoot"] is True
    assert ctx["readOnlyRootFilesystem"] is True
    assert ctx["allowPrivilegeEscalation"] is False
    assert ctx["capabilities"]["drop"] == ["ALL"]
    mounts = {mount["name"]: mount for mount in container["volumeMounts"]}
    # It needs the model (it calls the LLM) and the shared workspace (audit).
    assert "workspace" in mounts
    assert "model-config" in mounts and "model-secret" in mounts
    env = {item["name"]: item for item in container["env"]}
    assert env["AGENT_QUEUE"]["value"] == "redis"
    assert env["AGENT_REDIS_URL"]["value"].startswith("redis://redis")
    assert env["AGENT_API_TOKEN"]["valueFrom"]["secretKeyRef"]["name"] == \
        "auton-api-token"
    # The repair playbooks are not executed here (scanner/worker own them).
    assert "ops_worker.py" not in container["command"]
    assert "ops_scanner.py" not in container["command"]


def test_auton_apps_namespace_has_guardrails():
    kinds = {doc["kind"] for doc in _docs("auton-apps.yaml")}
    assert {"Namespace", "ResourceQuota", "LimitRange"} <= kinds


def test_worker_role_is_playbook_shaped():
    docs = _docs("auton-rbac.yaml")
    repair = next(d for d in docs if d["kind"] == "Role"
                  and d["metadata"]["name"] == "auton-worker")
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


def test_scanner_role_is_read_only():
    scanner = next(d for d in _docs("auton-rbac.yaml")
                   if d["kind"] == "Role"
                   and d["metadata"]["name"] == "auton-scanner")
    assert scanner["metadata"]["namespace"] == "auton-apps"
    for rule in _rules(scanner):
        assert set(rule.get("verbs") or []) <= {"get", "list", "watch"}


def test_worker_and_scanner_are_bound_only_in_auton_apps():
    docs = _docs("auton-rbac.yaml")
    subjects = {
        (doc["metadata"]["name"], doc["metadata"].get("namespace"),
         tuple((s.get("name"), s.get("namespace"))
               for s in doc.get("subjects") or []))
        for doc in docs
        if doc.get("kind") in ("RoleBinding", "ClusterRoleBinding")
    }
    assert ("auton-worker", "auton-apps",
            (("auton-worker", "auton"),)) in subjects
    assert ("auton-scanner", "auton-apps",
            (("auton-scanner", "auton"),)) in subjects
    # No cluster-scoped write identity for either of them.
    assert not any(name.startswith(("auton-worker", "auton-scanner"))
                   and namespace is None
                   for name, namespace, _ in subjects)


def test_worker_deployment_is_hardened_and_queue_driven():
    deployment = next(doc for doc in _docs("auton-worker-deployment.yaml")
                      if doc.get("kind") == "Deployment")
    assert deployment["metadata"]["name"] == "auton-worker"
    assert deployment["metadata"]["namespace"] == "auton"
    pod = deployment["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == "auton-worker"
    container = pod["containers"][0]
    assert container["command"][-1] == "ops_worker.py"
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
    assert env["AGENT_QUEUE"]["value"] == "redis"
    assert env["AGENT_REDIS_URL"]["value"].startswith("redis://redis")
    # The API token is a Secret reference, never a literal.
    assert env["AGENT_API_TOKEN"]["valueFrom"]["secretKeyRef"]["name"] == \
        "auton-api-token"
    assert "value" not in env["AGENT_API_TOKEN"]
    # No model mount: the worker never calls an LLM.
    assert not any(name.startswith("model-") for name in mounts)
    assert pod["initContainers"], "workspace ownership must be fixed by an init container"


def test_scanner_deployment_is_read_only_and_queue_driven():
    deployment = next(doc for doc in _docs("auton-scanner-deployment.yaml")
                      if doc.get("kind") == "Deployment")
    assert deployment["metadata"]["name"] == "auton-scanner"
    pod = deployment["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == "auton-scanner"
    container = pod["containers"][0]
    assert container["command"][-1] == "ops_scanner.py"
    ctx = container["securityContext"]
    assert ctx["runAsNonRoot"] is True
    assert ctx["readOnlyRootFilesystem"] is True
    assert ctx["capabilities"]["drop"] == ["ALL"]
    env = {item["name"]: item for item in container["env"]}
    assert env["AGENT_K8S_WRITE"]["value"] == "0"
    assert env["AGENT_QUEUE"]["value"] == "redis"
    # It must not mount the shared workspace/incident PVC.
    mounts = {mount["name"] for mount in container["volumeMounts"]}
    assert "workspace" not in mounts
