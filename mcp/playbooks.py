#!/usr/bin/env python3
"""Deterministic repair playbooks for the autonomous ops controller.

The controller decides *what* is broken (rule-based, see `ops_controller.py`);
these functions do the *how*. They never call the LLM: a 7B model explaining an
OOMKill is useful, a 7B model choosing a kubectl patch is not.

Every function returns::

    {"action": str, "ok": bool, "detail": str,
     "verification": str, "needs_human": bool}

and every mutation goes through `k8s_tools` (`_audited`), which writes the
write-ahead audit record first and refuses the action if that fails. The ops
controller wraps the call in `k8s_tools.incident_scope(...)`, so the audit
records carry the incident id.

Safety rails, in addition to RBAC:

* only namespaces in ``AGENT_OPS_NAMESPACES`` (default ``auton-apps``) are
  touched, and only when the namespace also passes the k8s_tools allowlist;
* resources labelled ``auton.io/protect=true`` are never mutated;
* image/config/scheduling/PVC problems are reported, not "fixed" by deleting
  things;
* memory growth is capped (``AGENT_OPS_MAX_MEMORY``, default ``2Gi``);
* a playbook verifies its own result and reports failure instead of claiming
  success.
"""
from __future__ import annotations

import json
import os
import re

import k8s_tools

MAX_MEMORY = os.environ.get("AGENT_OPS_MAX_MEMORY", "2Gi").strip() or "2Gi"

# Evidence that means "a human has to change configuration", not "restart it".
_CONFIG_HINTS = (
    "connection refused", "could not connect", "cannot connect",
    "no such host", "name resolution", "dial tcp", "i/o timeout",
    "permission denied", "access denied", "unauthorized", "forbidden",
    "missing environment", "environment variable", "env var",
    "configmap", "secret", "no such file or directory",
    "invalid configuration", "invalid config", "authentication failed",
    "certificate", "tls",
)

_MEM_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([KMGTP]i?|)\s*$", re.I)
_MEM_UNITS = {
    "": 1, "k": 1000, "m": 1000 ** 2, "g": 1000 ** 3, "t": 1000 ** 4,
    "p": 1000 ** 5, "ki": 1024, "mi": 1024 ** 2, "gi": 1024 ** 3,
    "ti": 1024 ** 4, "pi": 1024 ** 5,
}


class PlaybookError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def parse_memory(value: str) -> int | None:
    """``256Mi``/``512M``/``1073741824`` -> bytes; None when unparseable."""
    match = _MEM_RE.match(str(value or ""))
    if not match:
        return None
    try:
        return int(float(match.group(1)) * _MEM_UNITS[match.group(2).lower()])
    except (KeyError, ValueError):
        return None


def format_memory(num_bytes: int) -> str:
    """Bytes -> the largest binary unit that stays > 1 (``2Gi``, ``768Mi``)."""
    num_bytes = int(num_bytes)
    for unit, size in (("Gi", 1024 ** 3), ("Mi", 1024 ** 2), ("Ki", 1024)):
        if num_bytes >= size:
            value = num_bytes / size
            rounded = int(value) if abs(value - int(value)) < 0.01 else round(value, 1)
            return f"{rounded}{unit}"
    return f"{num_bytes}"


def _failed(out: str) -> bool:
    return (out or "").lstrip().startswith('{"ok": false')


def _error(out: str) -> str:
    try:
        return json.loads(out).get("error", out)
    except (ValueError, TypeError):
        return out


def ops_namespaces() -> tuple[str, ...]:
    raw = os.environ.get("AGENT_OPS_NAMESPACES", "auton-apps")
    return tuple(dict.fromkeys(
        part.strip() for part in raw.split(",") if part.strip()))


def _check_namespace(namespace: str) -> str | None:
    if namespace not in ops_namespaces():
        return (f"namespace {namespace!r} is outside AGENT_OPS_NAMESPACES "
                f"({', '.join(ops_namespaces())})")
    _, ns_err = k8s_tools._target_ns(namespace)
    return ns_err


def _protected(meta: dict) -> bool:
    labels = (meta or {}).get("labels") or {}
    return str(labels.get("auton.io/protect", "")).strip().lower() in (
        "1", "true", "yes", "on")


def _get(resource: str, name: str, namespace: str) -> tuple[dict | None, str]:
    data, err = k8s_tools._query(
        ["get", resource, name, "-o", "json", "--request-timeout=20s"],
        namespace=namespace)
    if err:
        return None, err
    return data, ""


def _deployment_ready(name: str, namespace: str) -> tuple[bool, str]:
    dep, err = _get("deployment", name, namespace)
    if err:
        return False, f"cannot read deployment/{name}: {_error(err)}"
    status = dep.get("status") or {}
    desired = int((dep.get("spec") or {}).get("replicas") or 0)
    available = int(status.get("availableReplicas") or 0)
    ready = int(status.get("readyReplicas") or 0)
    ok = desired > 0 and available >= desired and ready >= desired
    return ok, f"deployment/{name} ready={ready}/{desired} available={available}"


def workload_for_pod(pod: dict, namespace: str) -> tuple[str, str]:
    """Follow pod -> ReplicaSet -> Deployment (or return the direct owner)."""
    refs = (pod.get("metadata") or {}).get("ownerReferences") or []
    if not refs:
        return "", ""
    kind = (refs[0].get("kind") or "").strip()
    name = (refs[0].get("name") or "").strip()
    if not name:
        return "", ""
    if kind == "ReplicaSet":
        rs, err = _get("replicaset", name, namespace)
        if err:
            return kind, name
        owners = (rs.get("metadata") or {}).get("ownerReferences") or []
        if owners and owners[0].get("kind") == "Deployment":
            return "Deployment", owners[0].get("name", "")
    return kind, name


def _result(action: str, ok: bool, detail: str = "",
            verification: str = "", needs_human: bool = False) -> dict:
    return {"action": action, "ok": bool(ok), "detail": detail,
            "verification": verification, "needs_human": bool(needs_human)}


# --------------------------------------------------------------------------- #
# OOMKilled: raise the deployment's memory limit, roll it out, verify
# --------------------------------------------------------------------------- #

def fix_oom(pod_name: str, namespace: str, container: str = "") -> dict:
    ns_err = _check_namespace(namespace)
    if ns_err:
        return _result("oom_bump_memory", False, ns_err, needs_human=True)
    pod, err = _get("pod", pod_name, namespace)
    if err:
        return _result("oom_bump_memory", False, _error(err), needs_human=True)

    containers = (pod.get("spec") or {}).get("containers") or []
    target = container.strip() or (containers[0].get("name") if containers else "")
    if not target:
        return _result("oom_bump_memory", False,
                       "pod has no containers", needs_human=True)

    kind, workload = workload_for_pod(pod, namespace)
    if kind != "Deployment" or not workload:
        return _result(
            "oom_bump_memory", False,
            f"owner is {kind or 'unknown'}/{workload or '-'}; only Deployments "
            "are repaired automatically", needs_human=True)

    dep, err = _get("deployment", workload, namespace)
    if err:
        return _result("oom_bump_memory", False, _error(err), needs_human=True)
    if _protected(dep.get("metadata") or {}):
        return _result("oom_bump_memory", False,
                       "deployment is labelled auton.io/protect=true",
                       needs_human=True)

    pod_containers = (dep.get("spec") or {}).get("template", {}).get(
        "spec", {}).get("containers") or []
    spec = next((c for c in pod_containers if c.get("name") == target), None)
    if spec is None:
        return _result("oom_bump_memory", False,
                       f"container {target!r} not found in deployment/{workload}",
                       needs_human=True)

    resources = spec.get("resources") or {}
    current_raw = ((resources.get("limits") or {}).get("memory") or "")
    current = parse_memory(current_raw)
    if current is None:
        current = parse_memory("256Mi")
        basis = "no memory limit set; starting from 256Mi"
    else:
        basis = f"current limit {current_raw}"
    target_bytes = current + max(int(current * 0.25), 32 * 1024 ** 2)
    target_bytes = min(target_bytes, parse_memory(MAX_MEMORY) or 2 * 1024 ** 3)
    if target_bytes <= current:
        return _result(
            "oom_bump_memory", False,
            f"{basis} is already at the {MAX_MEMORY} cap; raise the cap or fix "
            "the leak", needs_human=True)
    new_limit = format_memory(target_bytes)

    limits = dict(resources.get("limits") or {})
    limits["memory"] = new_limit
    requests = dict(resources.get("requests") or {})
    if requests.get("memory"):
        requested = parse_memory(requests["memory"]) or 0
        if requested > target_bytes:
            requests["memory"] = new_limit

    patch = {"spec": {"template": {"spec": {"containers": [{
        "name": target,
        "resources": {"limits": limits, "requests": requests},
    }]}}}}
    out = k8s_tools.k8s_patch("deployment", workload, json.dumps(patch),
                              namespace=namespace)
    if _failed(out):
        return _result("oom_bump_memory", False, _error(out), needs_human=True)

    wait = k8s_tools.k8s_wait("deployments", workload, "available", 150,
                              namespace=namespace)
    ready, detail = _deployment_ready(workload, namespace)
    if not ready:
        return _result(
            "oom_bump_memory", False,
            f"raised {target} memory {basis} -> {new_limit}, but the rollout did "
            f"not become available: {detail} ({wait})",
            verification=detail, needs_human=True)
    return _result(
        "oom_bump_memory", True,
        f"raised container {target} memory {basis} -> {new_limit} and restarted "
        f"deployment/{workload}",
        verification=detail)


# --------------------------------------------------------------------------- #
# Stuck Terminating pod: force delete once
# --------------------------------------------------------------------------- #

def fix_stuck(pod_name: str, namespace: str, age: str = "") -> dict:
    ns_err = _check_namespace(namespace)
    if ns_err:
        return _result("stuck_force_delete", False, ns_err, needs_human=True)
    pod, err = _get("pod", pod_name, namespace)
    if err:
        # Already gone is a success: the pod finally terminated.
        if "NotFound" in err:
            return _result("stuck_force_delete", True,
                           "pod is already gone", "pod no longer exists")
        return _result("stuck_force_delete", False, _error(err), needs_human=True)
    if _protected(pod.get("metadata") or {}):
        return _result("stuck_force_delete", False,
                       "pod is labelled auton.io/protect=true",
                       needs_human=True)

    out = k8s_tools._audited(
        "k8s_delete",
        ["delete", "pod", pod_name, "--grace-period=0", "--force",
         "--request-timeout=30s"],
        timeout=60, namespace=namespace,
        plan=f"force delete pod/{pod_name} stuck Terminating {age}".strip(),
        args={"resource": "pod", "name": pod_name, "grace_period": 0,
              "force": True, "reason": "stuck Terminating"},
        evidence=f"pod/{pod_name} deletionTimestamp age={age}",
    )
    if _failed(out):
        return _result("stuck_force_delete", False, _error(out), needs_human=True)
    _, check_err = _get("pod", pod_name, namespace)
    if check_err and "NotFound" in check_err:
        return _result("stuck_force_delete", True,
                       f"force-deleted pod/{pod_name}",
                       "pod no longer exists")
    return _result("stuck_force_delete", False,
                   f"delete was accepted but pod/{pod_name} is still present",
                   verification="pod still present", needs_human=True)


# --------------------------------------------------------------------------- #
# CrashLoopBackOff: config-ish causes need a human, otherwise recreate once
# --------------------------------------------------------------------------- #

def fix_crashloop(pod_name: str, namespace: str, evidence: str = "") -> dict:
    ns_err = _check_namespace(namespace)
    if ns_err:
        return _result("crashloop_recreate_pod", False, ns_err, needs_human=True)
    pod, err = _get("pod", pod_name, namespace)
    if err:
        return _result("crashloop_recreate_pod", False, _error(err),
                       needs_human=True)
    if _protected(pod.get("metadata") or {}):
        return _result("crashloop_recreate_pod", False,
                       "pod is labelled auton.io/protect=true",
                       needs_human=True)

    lowered = (evidence or "").lower()
    hint = next((h for h in _CONFIG_HINTS if h in lowered), "")
    if hint:
        return _result(
            "crashloop_recreate_pod", False,
            f"logs point at configuration/dependency ({hint!r}); restarting the "
            "pod will not fix it", needs_human=True)

    kind, workload = workload_for_pod(pod, namespace)
    if kind != "Deployment":
        return _result(
            "crashloop_recreate_pod", False,
            f"owner is {kind or 'unknown'}/{workload or '-'}; not deleting a pod "
            "that no controller will recreate", needs_human=True)

    dep, dep_err = _get("deployment", workload, namespace)
    if dep_err:
        return _result("crashloop_recreate_pod", False, _error(dep_err),
                       needs_human=True)
    if _protected(dep.get("metadata") or {}):
        return _result("crashloop_recreate_pod", False,
                       "deployment is labelled auton.io/protect=true",
                       needs_human=True)

    out = k8s_tools.k8s_delete("pods", pod_name, confirm=True,
                               namespace=namespace)
    if _failed(out):
        return _result("crashloop_recreate_pod", False, _error(out),
                       needs_human=True)
    wait = k8s_tools.k8s_wait("deployments", workload, "available", 150,
                              namespace=namespace)
    ready, detail = _deployment_ready(workload, namespace)
    if not ready:
        return _result(
            "crashloop_recreate_pod", False,
            f"deleted pod/{pod_name}, but deployment/{workload} is still not "
            f"available: {detail} ({wait})", verification=detail,
            needs_human=True)
    return _result("crashloop_recreate_pod", True,
                   f"deleted pod/{pod_name}; the ReplicaSet recreated it and "
                   f"deployment/{workload} recovered",
                   verification=detail)


# --------------------------------------------------------------------------- #
# Failed rollout: undo to the previous ReplicaSet
# --------------------------------------------------------------------------- #

def fix_rollout(deployment: str, namespace: str, reason: str = "") -> dict:
    ns_err = _check_namespace(namespace)
    if ns_err:
        return _result("rollout_undo", False, ns_err, needs_human=True)
    dep, err = _get("deployment", deployment, namespace)
    if err:
        return _result("rollout_undo", False, _error(err), needs_human=True)
    if _protected(dep.get("metadata") or {}):
        return _result("rollout_undo", False,
                       "deployment is labelled auton.io/protect=true",
                       needs_human=True)

    out = k8s_tools.k8s_rollout("undo", f"deployment/{deployment}",
                                namespace=namespace)
    if _failed(out):
        return _result("rollout_undo", False,
                       f"rollout undo failed: {_error(out)}", needs_human=True)
    wait = k8s_tools.k8s_wait("deployments", deployment, "available", 150,
                              namespace=namespace)
    ready, detail = _deployment_ready(deployment, namespace)
    if not ready:
        return _result(
            "rollout_undo", False,
            f"undo completed but deployment/{deployment} is still unavailable "
            f"({reason}): {detail} ({wait})", verification=detail,
            needs_human=True)
    return _result("rollout_undo", True,
                   f"undid the failed rollout of deployment/{deployment} "
                   f"({reason})", verification=detail)


# --------------------------------------------------------------------------- #
# Deployment unavailable without a pod-level cause: scale a zeroed deployment
# --------------------------------------------------------------------------- #

def fix_replicas(deployment: str, namespace: str) -> dict:
    ns_err = _check_namespace(namespace)
    if ns_err:
        return _result("replicas_restore", False, ns_err, needs_human=True)
    dep, err = _get("deployment", deployment, namespace)
    if err:
        return _result("replicas_restore", False, _error(err), needs_human=True)
    if _protected(dep.get("metadata") or {}):
        return _result("replicas_restore", False,
                       "deployment is labelled auton.io/protect=true",
                       needs_human=True)
    desired = int((dep.get("spec") or {}).get("replicas") or 0)
    if desired > 0:
        return _result("replicas_restore", False,
                       f"spec.replicas is already {desired}; scaling is not the "
                       "fix", needs_human=True)
    out = k8s_tools.k8s_scale(f"deployment/{deployment}", 1, namespace=namespace)
    if _failed(out):
        return _result("replicas_restore", False, _error(out), needs_human=True)
    wait = k8s_tools.k8s_wait("deployments", deployment, "available", 150,
                              namespace=namespace)
    ready, detail = _deployment_ready(deployment, namespace)
    if not ready:
        return _result("replicas_restore", False,
                       f"scaled deployment/{deployment} 0 -> 1 but it is not "
                       f"available: {detail} ({wait})", verification=detail,
                       needs_human=True)
    return _result("replicas_restore", True,
                   f"scaled deployment/{deployment} 0 -> 1",
                   verification=detail)
