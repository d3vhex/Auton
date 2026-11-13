#!/usr/bin/env python3
"""The Repair Planner: turn a diagnosis into a concrete, reviewable plan.

A diagnosis says *what is wrong*. A plan says *what would be done about it*,
in enough detail that a human can approve it and an operator can audit it
afterwards.

    {
      "plan_id": "plan-1f0c…",
      "incident_id": "inc-…",
      "namespace": "auton-apps",
      "kind": "Pod",
      "name": "api-1",
      "resource": "pod/api-1",
      "cause": "oom",
      "root_cause": "OOMKilled",
      "action": "INCREASE_MEMORY_LIMIT",
      "playbook": "oom_bump_memory",
      "tier": 2,
      "params": {"container": "hog"},
      "expected_effect": "the container's memory limit is raised by 25% …",
      "verification": "deployment/api ready=1/1 available=1",
      "reversible": true,
      "rollback": {"action": "RESTORE_MEMORY_LIMIT",
                   "params": {"memory_limit": "64Mi"}},
      "requires_human": false,
      "created_at": 1794602994.059
    }

The plan is **not** an instruction to the cluster: it is a proposal. Only the
:mod:`policy` engine may turn it into an action, and only a registered
playbook may execute it. There is no free-form command anywhere in this path.

The tier is the risk of the action, not of the symptom:

===== ======================================================================
TIER  Meaning
===== ======================================================================
0     No mutation at all: observe, report, escalate to a human.
1     Transient and recreated by a controller: restart a pod, undo a
      rollout, scale replicas back up. The workload keeps serving.
2     Changes the workload's specification: raising a memory limit
      changes what the scheduler reserves for it.
3     Data-affecting or irreversible. Nothing automatic ever reaches this
      tier without an explicit approval.
===== ======================================================================
"""
from __future__ import annotations

import hashlib
import time

import diagnosis

#: The risk tier of every action the diagnosis engine may recommend.
ACTION_TIERS: dict[str, int] = {
    diagnosis.NONE: 0,
    diagnosis.REPORT_ONLY: 0,
    diagnosis.RESTART_POD: 1,
    diagnosis.ROLLBACK_DEPLOYMENT: 1,
    diagnosis.RESTORE_REPLICAS: 1,
    diagnosis.INCREASE_MEMORY_LIMIT: 2,
}

#: The playbook that carries out each action. The values are the ``action``
#: names the playbooks return, so a plan and its audit record agree.
ACTION_PLAYBOOKS: dict[str, str] = {
    diagnosis.NONE: "report_only",
    diagnosis.REPORT_ONLY: "report_only",
    diagnosis.RESTART_POD: "delete_pod",
    diagnosis.ROLLBACK_DEPLOYMENT: "rollout_undo",
    diagnosis.RESTORE_REPLICAS: "scale_replicas",
    diagnosis.INCREASE_MEMORY_LIMIT: "oom_bump_memory",
}

#: The incident cause -> playbook mapping used by the ops controller.
CAUSE_PLAYBOOKS: dict[str, str] = {
    "oom": "oom_bump_memory",
    "stuck": "delete_pod",
    "crashloop": "delete_pod",
    "rollout": "rollout_undo",
    "replicas_zero": "scale_replicas",
}


def tier_for(action: str) -> int:
    """The risk tier of an action (0 when the action is unknown)."""
    return ACTION_TIERS.get(str(action or "").upper(), 0)


def plan_id(namespace: str, resource: str, action: str, root_cause: str) -> str:
    """A stable id for a plan: the same plan for the same problem."""
    raw = f"{namespace}|{resource}|{action}|{root_cause}".encode("utf-8")
    return "plan-" + hashlib.sha1(raw).hexdigest()[:12]


def _expected_effect(action: str, cause: str, evidence: dict) -> str:
    if action == diagnosis.INCREASE_MEMORY_LIMIT:
        limit = ((evidence.get("resources") or {}).get("memory_limit") or "?")
        return (f"the deployment's memory limit for the container is raised by "
                f"25% (currently {limit}, minimum 32Mi) and the Deployment is "
                f"rolled out")
    if action == diagnosis.RESTART_POD:
        return ("the pod is deleted so its controller recreates it from the "
                "current spec")
    if action == diagnosis.ROLLBACK_DEPLOYMENT:
        return "the Deployment is rolled back to its previous revision"
    if action == diagnosis.RESTORE_REPLICAS:
        return "the Deployment is scaled back to its desired replica count"
    if action == diagnosis.REPORT_ONLY:
        return ("nothing is changed; the finding is reported to a human "
                f"({cause})")
    return "nothing is changed"


def _verification(action: str, namespace: str, name: str,
                   evidence: dict) -> str:
    owner = (evidence.get("owner") or {}) if isinstance(evidence, dict) else {}
    workload = owner.get("name") or name
    if action == diagnosis.INCREASE_MEMORY_LIMIT:
        return f"deployment/{workload} ready=1/1 available=1"
    if action == diagnosis.RESTART_POD:
        return f"pod/{name} recreated and the deployment is available"
    if action == diagnosis.ROLLBACK_DEPLOYMENT:
        return f"deployment/{workload} rolled back and available"
    if action == diagnosis.RESTORE_REPLICAS:
        return f"deployment/{workload} available=1"
    return "no automatic change to verify"


def _rollback(action: str, evidence: dict) -> dict:
    """How to undo the action, when it can be undone."""
    if action == diagnosis.INCREASE_MEMORY_LIMIT:
        limit = ((evidence.get("resources") or {}).get("memory_limit") or "")
        if limit:
            return {"action": "RESTORE_MEMORY_LIMIT",
                    "params": {"memory_limit": limit},
                    "note": "patch the deployment's memory limit back"}
        return {"action": "RESTORE_MEMORY_LIMIT", "params": {},
                "note": "the previous limit was not recorded"}
    if action == diagnosis.RESTORE_REPLICAS:
        return {"action": "SCALE_REPLICAS",
                "params": {"replicas": 0},
                "note": "scale the deployment back to zero"}
    if action == diagnosis.RESTART_POD:
        return {"action": "NONE", "params": {},
                "note": "the controller recreates the pod; nothing to undo"}
    return {"action": "NONE", "params": {}, "note": "the action is not reversible"}


def build(item: dict, namespace: str, result: dict, evidence: dict,
          *, incident_id: str = "", labels: dict | None = None) -> dict:
    """Build the repair plan for one finding.

    ``item`` is the scanner's finding (``kind``/``name``/``cause``/…), ``result``
    is the diagnosis, ``evidence`` is the structured evidence document.
    """
    result = result or {}
    action = str(result.get("recommended_action") or diagnosis.NONE).upper()
    root_cause = str(result.get("root_cause") or item.get("cause") or "")
    cause = str(item.get("cause") or "")
    kind = str(item.get("kind") or "")
    name = str(item.get("name") or "")
    resource = f"{(kind or '').lower()}/{(name or '').strip()}"

    playbook = ACTION_PLAYBOOKS.get(action) or CAUSE_PLAYBOOKS.get(cause,
                                                                  "report_only")
    tier = tier_for(action)
    document = evidence if isinstance(evidence, dict) else {}
    # The labels come from the evidence document unless the caller has newer
    # ones: the policy engine reads them to honour `auton.io/protect=true`.
    resource_labels = labels if labels else (document.get("resource") or {}).get(
        "labels") or {}

    return {
        "plan_id": plan_id(namespace, resource, action, root_cause),
        "incident_id": incident_id or "",
        "namespace": namespace,
        "kind": kind,
        "name": name,
        "resource": resource,
        "cause": cause,
        "root_cause": root_cause,
        "confidence": float(result.get("confidence") or 0.0),
        "action": action,
        "playbook": playbook,
        "tier": tier,
        "params": {"container": str(item.get("container") or "")},
        "expected_effect": _expected_effect(action, cause, document),
        "verification": _verification(action, namespace, name, document),
        "reversible": bool(action in (diagnosis.RESTART_POD,
                                      diagnosis.ROLLBACK_DEPLOYMENT,
                                      diagnosis.RESTORE_REPLICAS,
                                      diagnosis.INCREASE_MEMORY_LIMIT)),
        "rollback": _rollback(action, document),
        "requires_human": bool(result.get("requires_human")
                               or action in (diagnosis.REPORT_ONLY,
                                             diagnosis.NONE)),
        "labels": dict(resource_labels),
        "created_at": round(time.time(), 3),
    }


def summary(plan: dict) -> str:
    """One line describing a plan, for a notification or a log."""
    if not plan:
        return "no plan"
    return (f"{plan.get('action')} on {plan.get('resource')} "
            f"(tier {plan.get('tier')}) — {plan.get('expected_effect', '')}")
