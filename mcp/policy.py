#!/usr/bin/env python3
"""The Policy Engine: what the platform may do, and when it may do it.

The repair path is deterministic, but "deterministic" is not the same as
"allowed". This module is the gate between a *plan* and an *action*.

Two things live here:

**The policy document** — a JSON object stored in ConfigMap ``auton-policy``
(key ``policy.json``). It is mounted into every pod as a projected volume, so
no ServiceAccount needs permission to read it, and it is read **per decision**
so an operator can tighten it without a restart.

    {
      "mode": "safe-auto",
      "default_tier": 1,
      "auto_approve_tiers": [0, 1, 2],
      "approval_tiers": [3],
      "deny_tiers": [],
      "protected_namespaces": ["kube-system", "kube-public"],
      "protected_labels": [{"key": "auton.io/protect", "value": "true"}],
      "namespace_tiers": {"auton-apps": 1},
      "rules": [
        {"match": {"cause": "oom"}, "tier": 2},
        {"match": {"namespace": "auton"}, "action": "deny",
         "reason": "the platform never repairs itself"}
      ],
      "approval_timeout_seconds": 3600,
      "max_plan_age_seconds": 900
    }

**The decision** — ``decide(plan)`` answers one question: may this plan run
now, and if not, why?

    {"action": "auto" | "require_approval" | "deny" | "observe",
     "allowed": bool,
     "mode": "safe-auto",
     "tier": 2,
     "reason": "...",
     "rule": "rule #1",
     "requires_approval": bool,
     "source": "configmap" | "default"}

Modes
------

``disabled``
    Nothing runs. Every decision is a denial. This is the off switch.
``observe-only``
    Plans are computed and recorded, but never executed. Shadow mode.
``safe-auto``
    The tiers in ``auto_approve_tiers`` run unattended; the tiers in
    ``approval_tiers`` wait for a human; the tiers in ``deny_tiers`` are
    refused.
``approval-required``
    Every mutating tier waits for a human. Tier 0 (no mutation) is allowed,
    because there is nothing to approve.

Fail closed
-----------

An unreadable or invalid policy document is treated as ``disabled``: the
decision is a denial that names the problem, and :func:`describe` reports the
error so the UI can show it. A typo in the ConfigMap must never silently
*widen* what the platform is allowed to do.

The module is deliberately free of Kubernetes calls — it is pure policy logic,
so it is testable without a cluster.
"""
from __future__ import annotations

import json
import os
import pathlib

#: The operating modes of the engine.
MODES = ("disabled", "observe-only", "safe-auto", "approval-required")

#: What the engine may decide.
ACTIONS = ("auto", "require_approval", "deny", "observe")

#: The risk tiers a plan can be in.
TIERS = (0, 1, 2, 3)

#: The ConfigMap the policy lives in.
CONFIGMAP_NAME = "auton-policy"
CONFIGMAP_KEY = "policy.json"

#: The built-in policy: today's behaviour, with the irreversible tier gated.
DEFAULT_POLICY: dict = {
    "mode": "safe-auto",
    "default_tier": 1,
    "auto_approve_tiers": [0, 1, 2],
    "approval_tiers": [3],
    "deny_tiers": [],
    "protected_namespaces": ["kube-system", "kube-public", "kube-node-lease"],
    "protected_labels": [{"key": "auton.io/protect", "value": "true"}],
    "namespace_tiers": {},
    "rules": [],
    "approval_timeout_seconds": 3600,
    "max_plan_age_seconds": 900,
}


class PolicyError(ValueError):
    """The policy document is invalid; the message is safe to show."""


# --------------------------------------------------------------------------- #
# Location
# --------------------------------------------------------------------------- #

def policy_dir() -> str:
    """Where the projected ConfigMap is mounted."""
    return (os.environ.get("AGENT_POLICY_DIR", "/etc/auton-policy").strip()
            or "/etc/auton-policy")


def policy_file() -> pathlib.Path:
    """The policy document inside the projected volume."""
    return pathlib.Path(policy_dir()) / CONFIGMAP_KEY


def mode_file() -> pathlib.Path:
    """The flat ``mode`` key, so one ``kubectl patch`` can flip the engine."""
    return pathlib.Path(policy_dir()) / "mode"


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #

def _int_tiers(value, what: str) -> list[int]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise PolicyError(f"{what} must be a list of tiers")
    tiers: list[int] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int):
            try:
                item = int(str(item).strip())
            except (TypeError, ValueError):
                raise PolicyError(
                    f"{what} must contain tier numbers 0-3") from None
        if item not in TIERS:
            raise PolicyError(f"{what} must contain tier numbers 0-3")
        tiers.append(item)
    return sorted(set(tiers))


def _int(value, what: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise PolicyError(f"{what} must be an integer")
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        raise PolicyError(f"{what} must be an integer") from None
    if not minimum <= number <= maximum:
        raise PolicyError(f"{what} must be between {minimum} and {maximum}")
    return number


def _string_list(value, what: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise PolicyError(f"{what} must be a list of strings")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise PolicyError(f"{what} must be a list of strings")
        text = item.strip()
        if text:
            out.append(text)
    return out


def _rules(value) -> list[dict]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise PolicyError("rules must be a list")
    rules: list[dict] = []
    for raw in value:
        if not isinstance(raw, dict):
            raise PolicyError("every rule must be an object")
        match = raw.get("match") or {}
        if not isinstance(match, dict):
            raise PolicyError("rule.match must be an object")
        unknown = set(match) - {"cause", "namespace", "kind", "resource",
                                "action", "tier"}
        if unknown:
            raise PolicyError("unknown match field(s): "
                              + ", ".join(sorted(unknown)))
        rule = {"match": {key: str(value).strip()
                          for key, value in match.items()
                          if str(value).strip()}}
        action = str(raw.get("action", "") or "").strip().lower()
        if action:
            if action not in ACTIONS:
                raise PolicyError("rule.action must be one of "
                                  + ", ".join(ACTIONS))
            rule["action"] = action
        if raw.get("tier") is not None:
            rule["tier"] = _int_tiers([raw["tier"]], "rule.tier")[0]
        reason = str(raw.get("reason", "") or "").strip()
        if reason:
            rule["reason"] = reason[:300]
        if not rule["match"] and "tier" not in rule and "action" not in rule:
            raise PolicyError("a rule must have a match, a tier or an action")
        rules.append(rule)
    return rules


def validate(raw: dict) -> dict:
    """Normalise a policy document; raises :class:`PolicyError`.

    The returned document is the *effective* policy: every key is present and
    has the right type, so the decision code never has to guess.
    """
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise PolicyError("the policy must be a JSON object")

    policy = dict(DEFAULT_POLICY)
    policy.update(raw)

    mode = str(policy.get("mode", "") or "").strip().lower()
    if mode not in MODES:
        raise PolicyError("mode must be one of " + ", ".join(MODES))
    policy["mode"] = mode

    policy["default_tier"] = _int_tiers([policy.get("default_tier", 1)],
                                         "default_tier")[0]
    policy["auto_approve_tiers"] = _int_tiers(policy.get("auto_approve_tiers"),
                                               "auto_approve_tiers")
    policy["approval_tiers"] = _int_tiers(policy.get("approval_tiers"),
                                           "approval_tiers")
    policy["deny_tiers"] = _int_tiers(policy.get("deny_tiers"), "deny_tiers")
    policy["protected_namespaces"] = _string_list(
        policy.get("protected_namespaces"), "protected_namespaces")

    labels = policy.get("protected_labels") or []
    if not isinstance(labels, (list, tuple)):
        raise PolicyError("protected_labels must be a list")
    normalised: list[dict] = []
    for item in labels:
        if isinstance(item, str):
            item = {"key": item, "value": ""}
        if not isinstance(item, dict):
            raise PolicyError("every protected label must be an object")
        key = str(item.get("key", "") or "").strip()
        if not key:
            raise PolicyError("a protected label needs a key")
        normalised.append({"key": key,
                           "value": str(item.get("value", "") or "").strip()})
    policy["protected_labels"] = normalised

    namespace_tiers = policy.get("namespace_tiers") or {}
    if not isinstance(namespace_tiers, dict):
        raise PolicyError("namespace_tiers must be an object")
    policy["namespace_tiers"] = {
        str(name).strip(): _int_tiers([tier], "namespace_tiers")[0]
        for name, tier in namespace_tiers.items() if str(name).strip()
    }

    policy["rules"] = _rules(policy.get("rules"))
    policy["approval_timeout_seconds"] = _int(
        policy.get("approval_timeout_seconds", 3600),
        "approval_timeout_seconds", minimum=60, maximum=604800)
    policy["max_plan_age_seconds"] = _int(
        policy.get("max_plan_age_seconds", 900), "max_plan_age_seconds",
        minimum=0, maximum=604800)
    return policy


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #

def load() -> tuple[dict, str]:
    """Read the policy document.

    Returns ``(policy, source)``. ``source`` is ``"configmap"`` when the
    document came from the projected volume and ``"default"`` when the built-in
    policy is in use. An invalid document raises :class:`PolicyError` — the
    caller turns that into a denial (fail closed).
    """
    path = policy_file()
    raw: dict | None = None
    source = "default"
    if path.is_file():
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise PolicyError(f"cannot read {path}: {exc}") from None
        if text:
            try:
                raw = json.loads(text)
            except json.JSONDecodeError as exc:
                raise PolicyError(f"{path.name} is not valid JSON: "
                                  f"{exc.msg} (line {exc.lineno})") from None
            if not isinstance(raw, dict):
                # A document that is not an object is a broken document, not an
                # absent one: falling back to the default would silently widen
                # what the platform may do.
                raise PolicyError(f"{path.name} must contain a JSON object")
            source = "configmap"

    policy = validate(raw if raw is not None else {})
    if raw is None:
        # No document at all: fall back to the env override, then the default.
        env_mode = os.environ.get("AGENT_POLICY_MODE", "").strip().lower()
        if env_mode:
            if env_mode not in MODES:
                raise PolicyError("AGENT_POLICY_MODE must be one of "
                                  + ", ".join(MODES))
            policy["mode"] = env_mode

    # The flat `mode` key wins over the document, so one `kubectl patch` can
    # flip the engine without touching the JSON.
    flat = mode_file()
    if flat.is_file():
        try:
            text = flat.read_text(encoding="utf-8").strip().lower()
        except OSError:
            text = ""
        if text:
            if text not in MODES:
                raise PolicyError("the mode key must be one of "
                                  + ", ".join(MODES))
            policy["mode"] = text
            source = "configmap"
    return policy, source


# --------------------------------------------------------------------------- #
# Decisions
# --------------------------------------------------------------------------- #

def _decision(action: str, policy: dict, tier: int, reason: str,
              rule: str = "", source: str = "configmap") -> dict:
    return {
        "action": action,
        "allowed": action == "auto",
        "mode": policy.get("mode", "safe-auto"),
        "tier": int(tier),
        "reason": reason,
        "rule": rule,
        "requires_approval": action == "require_approval",
        "source": source,
    }


def _matches(rule: dict, plan: dict) -> bool:
    """Does this rule apply to the plan? Every match field must match."""
    match = rule.get("match") or {}
    for key, wanted in match.items():
        if key == "tier":
            if int(plan.get("tier", -1)) != int(wanted):
                return False
            continue
        if key == "cause":
            actual = str(plan.get("cause", ""))
        elif key == "namespace":
            actual = str(plan.get("namespace", ""))
        elif key == "kind":
            actual = str(plan.get("kind", ""))
        elif key == "resource":
            actual = str(plan.get("resource", ""))
        elif key == "action":
            actual = str(plan.get("action", ""))
        else:
            return False
        wanted_text = str(wanted).strip()
        if key == "resource":
            # A resource match is a prefix match on "kind/name".
            if not actual.lower().startswith(wanted_text.lower()):
                return False
            continue
        if actual.lower() != wanted_text.lower():
            return False
    return True


def _protected(plan: dict, policy: dict) -> str:
    """Is the target protected? Returns the reason or an empty string."""
    namespace = str(plan.get("namespace", ""))
    if namespace in policy.get("protected_namespaces", []):
        return f"namespace {namespace!r} is protected by the policy"
    labels = plan.get("labels") or {}
    for entry in policy.get("protected_labels", []):
        actual = labels.get(entry["key"])
        if actual is None:
            continue
        wanted = entry.get("value", "")
        if not wanted or str(actual) == wanted:
            return (f"the resource is labelled "
                    f"{entry['key']}={actual!r}, which the policy protects")
    return ""


def _tier_for(plan: dict, policy: dict) -> int:
    namespace = str(plan.get("namespace", ""))
    tiers = policy.get("namespace_tiers") or {}
    if namespace in tiers:
        return int(tiers[namespace])
    if plan.get("tier") is not None:
        try:
            return int(plan["tier"])
        except (TypeError, ValueError):
            pass
    return int(policy.get("default_tier", 1))


def decide(plan: dict, policy: dict | None = None) -> dict:
    """Decide what may happen to ``plan``.

    The plan is the output of :mod:`planner`. The decision is the output of
    this module. Nothing else may act on a plan without one.
    """
    if not isinstance(plan, dict):
        plan = {}

    source = "default"
    if policy is None:
        try:
            policy, source = load()
        except PolicyError as exc:
            # Fail closed: an invalid policy denies everything.
            broken = dict(DEFAULT_POLICY)
            broken["mode"] = "disabled"
            return _decision("deny", broken, 3,
                             f"the policy is invalid, so nothing is allowed: "
                             f"{exc}", source="invalid")
    else:
        policy = validate(policy)

    mode = policy["mode"]
    tier = _tier_for(plan, policy)

    # 1. The off switch wins over everything else.
    if mode == "disabled":
        return _decision("deny", policy, tier,
                         "the policy engine is disabled", source=source)

    # 2. A protected target is never repaired, whatever the mode says.
    protected = _protected(plan, policy)
    if protected:
        return _decision("deny", policy, tier, protected, source=source)

    # 3. The first matching rule may set the tier and/or the action.
    rule_action = ""
    rule_reason = ""
    rule_name = ""
    for index, rule in enumerate(policy.get("rules", []), 1):
        if not _matches(rule, plan):
            continue
        rule_name = f"rule #{index}"
        if "tier" in rule:
            tier = int(rule["tier"])
        if "action" in rule:
            rule_action = rule["action"]
        rule_reason = str(rule.get("reason", "") or "")
        break

    # 4. Shadow mode: compute the plan, never act on it.
    if mode == "observe-only":
        return _decision("observe", policy, tier,
                         rule_reason or "the policy is in observe-only mode",
                         rule_name, source=source)

    # 5. An explicit rule action overrides the mode-derived one.
    if rule_action == "deny":
        return _decision("deny", policy, tier,
                         rule_reason or f"denied by {rule_name}",
                         rule_name, source=source)
    if rule_action == "observe":
        return _decision("observe", policy, tier,
                         rule_reason or f"observed by {rule_name}",
                         rule_name, source=source)
    if rule_action == "require_approval":
        return _decision("require_approval", policy, tier,
                         rule_reason or f"requires approval by {rule_name}",
                         rule_name, source=source)
    if rule_action == "auto":
        # An explicit "auto" may not unlock a tier the mode denies outright.
        if mode == "approval-required" and tier > 0 and tier not in \
                policy.get("auto_approve_tiers", []):
            return _decision("require_approval", policy, tier,
                             rule_reason or
                             f"{rule_name} allows this, but the policy mode "
                             f"is approval-required", rule_name, source=source)
        if tier in policy.get("deny_tiers", []):
            return _decision("deny", policy, tier,
                             f"tier {tier} is denied by the policy",
                             rule_name, source=source)
        return _decision("auto", policy, tier,
                         rule_reason or f"allowed by {rule_name}",
                         rule_name, source=source)

    # 6. The mode decides.
    if tier in policy.get("deny_tiers", []):
        return _decision("deny", policy, tier,
                         f"tier {tier} is denied by the policy",
                         rule_name, source=source)
    if mode == "approval-required":
        if tier <= 0:
            return _decision("auto", policy, tier,
                             "tier 0 does not change anything", rule_name,
                             source=source)
        return _decision("require_approval", policy, tier,
                         f"tier {tier} needs an approval in "
                         f"approval-required mode", rule_name, source=source)
    if tier in policy.get("approval_tiers", []):
        return _decision("require_approval", policy, tier,
                         f"tier {tier} needs an approval", rule_name,
                         source=source)
    if tier in policy.get("auto_approve_tiers", []):
        return _decision("auto", policy, tier,
                         f"tier {tier} is approved automatically", rule_name,
                         source=source)
    # Unknown tier: fail closed.
    return _decision("require_approval", policy, tier,
                     f"tier {tier} is not listed in auto_approve_tiers, so a "
                     f"human decides", rule_name, source=source)


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #

def describe() -> dict:
    """The effective policy, for ``GET /policy``."""
    try:
        policy, source = load()
        return {"ok": True, "source": source, "policy": policy,
                "error": "", "modes": list(MODES), "actions": list(ACTIONS),
                "tiers": list(TIERS)}
    except PolicyError as exc:
        return {"ok": False, "source": "invalid", "error": str(exc),
                "policy": dict(DEFAULT_POLICY), "modes": list(MODES),
                "actions": list(ACTIONS), "tiers": list(TIERS)}
