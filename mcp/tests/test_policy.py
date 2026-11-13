"""Tests for the Policy Engine (modes, tiers, rules, fail-closed)."""
from __future__ import annotations

import json

import pytest

import policy


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """A policy directory the test can write into."""
    directory = tmp_path / "policy"
    directory.mkdir()
    monkeypatch.setenv("AGENT_POLICY_DIR", str(directory))
    return directory


def _write(directory, document: dict, mode: str = "") -> None:
    (directory / "policy.json").write_text(json.dumps(document), encoding="utf-8")
    if mode:
        (directory / "mode").write_text(mode, encoding="utf-8")


def _plan(**overrides) -> dict:
    plan = {
        "namespace": "auton-apps",
        "kind": "Pod",
        "name": "api-1",
        "resource": "pod/api-1",
        "cause": "oom",
        "action": "INCREASE_MEMORY_LIMIT",
        "tier": 2,
        "labels": {},
    }
    plan.update(overrides)
    return plan


# --------------------------------------------------------------------------- #
# Defaults and validation
# --------------------------------------------------------------------------- #

def test_the_built_in_policy_keeps_todays_behaviour():
    document = policy.validate({})
    assert document["mode"] == "safe-auto"
    # Everything the platform can do today runs unattended; the irreversible
    # tier waits for a human.
    assert document["auto_approve_tiers"] == [0, 1, 2]
    assert document["approval_tiers"] == [3]


def test_an_unknown_mode_is_rejected():
    with pytest.raises(policy.PolicyError):
        policy.validate({"mode": "yolo"})


def test_an_unknown_tier_is_rejected():
    with pytest.raises(policy.PolicyError):
        policy.validate({"auto_approve_tiers": [7]})


def test_a_rule_with_an_unknown_match_field_is_rejected():
    with pytest.raises(policy.PolicyError):
        policy.validate({"rules": [{"match": {"pod": "x"}}]})


def test_a_rule_with_an_unknown_action_is_rejected():
    with pytest.raises(policy.PolicyError):
        policy.validate({"rules": [{"match": {"cause": "oom"},
                                    "action": "rm-rf"}]})


def test_an_empty_rule_is_rejected():
    with pytest.raises(policy.PolicyError):
        policy.validate({"rules": [{"reason": "no match"}]})


def test_the_validation_normalises_the_types():
    document = policy.validate({"mode": "SAFE-AUTO",
                                "auto_approve_tiers": ["1", 1, "0"],
                                "namespace_tiers": {"auton-apps": "2"}})
    assert document["mode"] == "safe-auto"
    assert document["auto_approve_tiers"] == [0, 1]
    assert document["namespace_tiers"] == {"auton-apps": 2}


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #

def test_the_default_policy_is_used_when_no_document_exists(env):
    document, source = policy.load()
    assert source == "default"
    assert document["mode"] == "safe-auto"


def test_the_document_is_read_from_the_projected_volume(env):
    _write(env, {"mode": "observe-only"})
    document, source = policy.load()
    assert source == "configmap"
    assert document["mode"] == "observe-only"


def test_the_flat_mode_key_wins_over_the_document(env):
    _write(env, {"mode": "safe-auto"}, mode="disabled")
    document, _ = policy.load()
    assert document["mode"] == "disabled"


def test_an_invalid_json_document_is_an_error(env):
    (env / "policy.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(policy.PolicyError):
        policy.load()


def test_describe_reports_the_error_instead_of_raising(env):
    (env / "policy.json").write_text("{not json", encoding="utf-8")
    described = policy.describe()
    assert described["ok"] is False
    assert "JSON" in described["error"]


# --------------------------------------------------------------------------- #
# Modes
# --------------------------------------------------------------------------- #

def test_disabled_denies_everything(env):
    _write(env, {"mode": "disabled"})
    decision = policy.decide(_plan())
    assert decision["action"] == "deny"
    assert decision["allowed"] is False
    assert "disabled" in decision["reason"]


def test_disabled_denies_even_when_a_rule_says_auto(env):
    _write(env, {"mode": "disabled",
                 "rules": [{"match": {"cause": "oom"}, "action": "auto"}]})
    assert policy.decide(_plan())["action"] == "deny"


def test_observe_only_never_acts(env):
    _write(env, {"mode": "observe-only"})
    decision = policy.decide(_plan())
    assert decision["action"] == "observe"
    assert decision["allowed"] is False


def test_safe_auto_allows_an_auto_tier(env):
    _write(env, {"mode": "safe-auto", "auto_approve_tiers": [0, 1, 2]})
    decision = policy.decide(_plan())
    assert decision["action"] == "auto"
    assert decision["allowed"] is True
    assert decision["tier"] == 2


def test_safe_auto_asks_for_an_approval_on_a_higher_tier(env):
    _write(env, {"mode": "safe-auto", "auto_approve_tiers": [0, 1],
                 "approval_tiers": [2, 3]})
    decision = policy.decide(_plan())
    assert decision["action"] == "require_approval"
    assert decision["requires_approval"] is True
    assert decision["allowed"] is False


def test_approval_required_asks_for_even_a_low_tier(env):
    _write(env, {"mode": "approval-required"})
    decision = policy.decide(_plan(tier=1, action="RESTART_POD"))
    assert decision["action"] == "require_approval"


def test_approval_required_allows_tier_zero(env):
    _write(env, {"mode": "approval-required"})
    decision = policy.decide(_plan(tier=0, action="REPORT_ONLY"))
    assert decision["action"] == "auto"


def test_a_denied_tier_is_refused_even_in_safe_auto(env):
    _write(env, {"mode": "safe-auto", "deny_tiers": [2],
                 "auto_approve_tiers": [0, 1, 2]})
    decision = policy.decide(_plan())
    assert decision["action"] == "deny"


def test_an_unknown_tier_fails_closed(env):
    _write(env, {"mode": "safe-auto", "auto_approve_tiers": [0, 1]})
    decision = policy.decide(_plan(tier=3))
    assert decision["action"] == "require_approval"


# --------------------------------------------------------------------------- #
# Protected targets
# --------------------------------------------------------------------------- #

def test_a_protected_namespace_is_never_repaired(env):
    _write(env, {"mode": "safe-auto",
                 "protected_namespaces": ["kube-system"]})
    decision = policy.decide(_plan(namespace="kube-system"))
    assert decision["action"] == "deny"
    assert "protected" in decision["reason"]


def test_a_protected_label_is_never_repaired(env):
    _write(env, {"mode": "safe-auto",
                 "protected_labels": [{"key": "auton.io/protect",
                                       "value": "true"}]})
    decision = policy.decide(_plan(labels={"auton.io/protect": "true"}))
    assert decision["action"] == "deny"


def test_a_protected_label_with_a_different_value_is_not_protected(env):
    _write(env, {"mode": "safe-auto",
                 "protected_labels": [{"key": "auton.io/protect",
                                       "value": "true"}]})
    decision = policy.decide(_plan(labels={"auton.io/protect": "false"}))
    assert decision["action"] == "auto"


# --------------------------------------------------------------------------- #
# Rules
# --------------------------------------------------------------------------- #

def test_a_matching_rule_can_raise_the_tier(env):
    _write(env, {"mode": "safe-auto", "auto_approve_tiers": [0, 1, 2],
                 "rules": [{"match": {"cause": "oom"}, "tier": 3}],
                 "approval_tiers": [3]})
    decision = policy.decide(_plan())
    assert decision["tier"] == 3
    assert decision["action"] == "require_approval"


def test_a_rule_can_deny_a_specific_cause(env):
    _write(env, {"mode": "safe-auto",
                 "rules": [{"match": {"cause": "crashloop"}, "action": "deny",
                            "reason": "crashloops are for humans"}]})
    decision = policy.decide(_plan(cause="crashloop"))
    assert decision["action"] == "deny"
    assert "humans" in decision["reason"]


def test_a_rule_can_force_an_approval(env):
    _write(env, {"mode": "safe-auto",
                 "rules": [{"match": {"namespace": "auton-apps"},
                            "action": "require_approval"}]})
    decision = policy.decide(_plan())
    assert decision["action"] == "require_approval"


def test_a_rule_that_does_not_match_is_ignored(env):
    _write(env, {"mode": "safe-auto",
                 "rules": [{"match": {"cause": "rollout"}, "action": "deny"}]})
    assert policy.decide(_plan(cause="oom"))["action"] == "auto"


def test_the_first_matching_rule_wins(env):
    _write(env, {"mode": "safe-auto", "rules": [
        {"match": {"cause": "oom"}, "action": "deny"},
        {"match": {"cause": "oom"}, "action": "auto"},
    ]})
    assert policy.decide(_plan())["action"] == "deny"


def test_a_resource_rule_matches_on_the_kind_prefix(env):
    _write(env, {"mode": "safe-auto",
                 "rules": [{"match": {"resource": "deployment/"},
                            "action": "deny"}]})
    assert policy.decide(_plan(resource="deployment/api"))["action"] == "deny"
    assert policy.decide(_plan(resource="pod/api"))["action"] == "auto"


def test_a_rule_cannot_unlock_a_denied_tier(env):
    _write(env, {"mode": "safe-auto", "deny_tiers": [3],
                 "rules": [{"match": {"cause": "oom"}, "action": "auto",
                            "tier": 3}]})
    assert policy.decide(_plan())["action"] == "deny"


# --------------------------------------------------------------------------- #
# Namespace tiers
# --------------------------------------------------------------------------- #

def test_a_namespace_tier_overrides_the_plan_tier(env):
    _write(env, {"mode": "safe-auto", "auto_approve_tiers": [0, 1],
                 "approval_tiers": [2], "namespace_tiers": {"auton-apps": 1}})
    decision = policy.decide(_plan(tier=2))
    assert decision["tier"] == 1
    assert decision["action"] == "auto"


# --------------------------------------------------------------------------- #
# Fail closed
# --------------------------------------------------------------------------- #

def test_an_invalid_policy_denies_everything(env):
    (env / "policy.json").write_text("{not json", encoding="utf-8")
    decision = policy.decide(_plan())
    assert decision["action"] == "deny"
    assert "invalid" in decision["reason"]


def test_a_policy_that_cannot_be_parsed_never_allows_a_tier_zero(env):
    (env / "policy.json").write_text("[]", encoding="utf-8")
    decision = policy.decide(_plan(tier=0, action="REPORT_ONLY"))
    assert decision["action"] == "deny"
