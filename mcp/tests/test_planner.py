"""Tests for the Repair Planner (the plan is a proposal, never an order)."""
from __future__ import annotations

import diagnosis
import planner


def _item(**overrides) -> dict:
    item = {"kind": "Pod", "name": "api-1", "cause": "oom",
            "severity": "critical", "container": "hog"}
    item.update(overrides)
    return item


def _result(**overrides) -> dict:
    result = {"root_cause": "OOMKilled", "confidence": 0.97,
              "severity": "high", "evidence": ["exit code 137"],
              "recommended_action": diagnosis.INCREASE_MEMORY_LIMIT,
              "requires_human": False}
    result.update(overrides)
    return result


def _evidence(**overrides) -> dict:
    document = {
        "resource": {"kind": "Pod", "name": "api-1", "namespace": "auton-apps",
                     "uid": "uid-1", "labels": {"app": "api"}},
        "problem": "OOMKilled",
        "container": {"name": "hog", "exit_code": 137},
        "resources": {"memory_limit": "512Mi", "memory_request": "256Mi"},
        "owner": {"kind": "Deployment", "name": "api"},
    }
    document.update(overrides)
    return document


# --------------------------------------------------------------------------- #
# Shape
# --------------------------------------------------------------------------- #

def test_a_plan_has_the_documented_shape():
    plan = planner.build(_item(), "auton-apps", _result(), _evidence(),
                          incident_id="inc-1")
    for key in ("plan_id", "incident_id", "namespace", "kind", "name",
                "resource", "cause", "root_cause", "action", "playbook",
                "tier", "params", "expected_effect", "verification",
                "reversible", "rollback", "requires_human", "created_at"):
        assert key in plan, f"{key} missing"
    assert plan["incident_id"] == "inc-1"
    assert plan["resource"] == "pod/api-1"
    assert plan["action"] == diagnosis.INCREASE_MEMORY_LIMIT
    assert plan["playbook"] == "oom_bump_memory"


def test_the_plan_id_is_stable_for_the_same_problem():
    first = planner.build(_item(), "auton-apps", _result(), _evidence())
    second = planner.build(_item(), "auton-apps", _result(), _evidence())
    assert first["plan_id"] == second["plan_id"]
    assert first["plan_id"].startswith("plan-")


def test_a_different_problem_gets_a_different_plan_id():
    first = planner.build(_item(), "auton-apps", _result(), _evidence())
    second = planner.build(_item(name="api-2"), "auton-apps", _result(),
                           _evidence())
    assert first["plan_id"] != second["plan_id"]


# --------------------------------------------------------------------------- #
# Tiers
# --------------------------------------------------------------------------- #

def test_the_tier_follows_the_risk_of_the_action():
    assert planner.tier_for(diagnosis.NONE) == 0
    assert planner.tier_for(diagnosis.REPORT_ONLY) == 0
    assert planner.tier_for(diagnosis.RESTART_POD) == 1
    assert planner.tier_for(diagnosis.ROLLBACK_DEPLOYMENT) == 1
    assert planner.tier_for(diagnosis.RESTORE_REPLICAS) == 1
    assert planner.tier_for(diagnosis.INCREASE_MEMORY_LIMIT) == 2


def test_an_unknown_action_is_tier_zero():
    assert planner.tier_for("SOMETHING_ELSE") == 0


def test_the_memory_bump_is_tier_two():
    plan = planner.build(_item(), "auton-apps", _result(), _evidence())
    assert plan["tier"] == 2


def test_a_restart_is_tier_one():
    plan = planner.build(_item(cause="crashloop"),
                         "auton-apps",
                         _result(root_cause="ApplicationCrash",
                                 recommended_action=diagnosis.RESTART_POD),
                         _evidence())
    assert plan["tier"] == 1
    assert plan["playbook"] == "delete_pod"


def test_a_report_only_plan_is_tier_zero_and_needs_a_human():
    plan = planner.build(_item(cause="image"), "auton-apps",
                         _result(root_cause="ImagePullFailure",
                                 recommended_action=diagnosis.REPORT_ONLY,
                                 requires_human=True),
                         _evidence())
    assert plan["tier"] == 0
    assert plan["requires_human"] is True
    assert plan["playbook"] == "report_only"


# --------------------------------------------------------------------------- #
# Reversibility
# --------------------------------------------------------------------------- #

def test_a_memory_bump_records_how_to_undo_it():
    plan = planner.build(_item(), "auton-apps", _result(), _evidence())
    assert plan["reversible"] is True
    assert plan["rollback"]["action"] == "RESTORE_MEMORY_LIMIT"
    assert plan["rollback"]["params"]["memory_limit"] == "512Mi"


def test_a_report_only_plan_is_not_reversible():
    plan = planner.build(_item(cause="image"), "auton-apps",
                         _result(recommended_action=diagnosis.REPORT_ONLY),
                         _evidence())
    assert plan["reversible"] is False
    assert plan["rollback"]["action"] == "NONE"


# --------------------------------------------------------------------------- #
# Evidence
# --------------------------------------------------------------------------- #

def test_the_labels_are_carried_into_the_plan():
    plan = planner.build(_item(), "auton-apps", _result(), _evidence())
    assert plan["labels"] == {"app": "api"}


def test_the_verification_names_the_workload():
    plan = planner.build(_item(), "auton-apps", _result(), _evidence())
    assert plan["verification"] == "deployment/api ready=1/1 available=1"


def test_a_missing_evidence_document_does_not_break_the_plan():
    plan = planner.build(_item(), "auton-apps", _result(), {})
    assert plan["tier"] == 2
    assert plan["expected_effect"]
    assert plan["verification"]


def test_the_summary_is_one_line():
    plan = planner.build(_item(), "auton-apps", _result(), _evidence())
    text = planner.summary(plan)
    assert "\n" not in text
    assert "INCREASE_MEMORY_LIMIT" in text
