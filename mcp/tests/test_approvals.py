"""Tests for the approval store (a plan waits for a human)."""
from __future__ import annotations

import time

import pytest

import approvals


@pytest.fixture()
def store(tmp_path):
    return approvals.ApprovalStore(str(tmp_path / "approvals"))


def _plan(**overrides) -> dict:
    plan = {"plan_id": "plan-1", "namespace": "auton-apps", "kind": "Pod",
            "name": "api-1", "resource": "pod/api-1", "cause": "oom",
            "action": "INCREASE_MEMORY_LIMIT", "tier": 2}
    plan.update(overrides)
    return plan


def _policy(**overrides) -> dict:
    decision = {"action": "require_approval", "tier": 2, "mode": "safe-auto",
                "reason": "tier 2 needs an approval"}
    decision.update(overrides)
    return decision


# --------------------------------------------------------------------------- #
# Creation
# --------------------------------------------------------------------------- #

def test_a_created_approval_is_pending(store):
    record = store.create(plan=_plan(), policy=_policy(),
                          incident_id="inc-1", task_id="tsk-1")
    assert record["id"].startswith("apr-")
    assert record["status"] == "pending"
    assert record["incident_id"] == "inc-1"
    assert record["task_id"] == "tsk-1"
    assert record["plan"]["plan_id"] == "plan-1"
    assert record["policy"]["action"] == "require_approval"
    assert record["expires_at"] > record["created_at"]


def test_a_created_approval_has_a_timeline(store):
    record = store.create(plan=_plan(), policy=_policy())
    assert record["timeline"][0]["event"] == "created"


def test_an_invalid_id_is_refused(store):
    with pytest.raises(approvals.ApprovalError):
        store.get("../../etc/passwd")


# --------------------------------------------------------------------------- #
# Deciding
# --------------------------------------------------------------------------- #

def test_approving_marks_the_record(store):
    created = store.create(plan=_plan(), policy=_policy())
    decided = store.decide(created["id"], approve=True, by="alice",
                           comment="looks right")
    assert decided["status"] == "approved"
    assert decided["decided_by"] == "alice"
    assert decided["comment"] == "looks right"
    assert decided["decided_at"] > 0


def test_rejecting_marks_the_record(store):
    created = store.create(plan=_plan(), policy=_policy())
    decided = store.decide(created["id"], approve=False, by="bob")
    assert decided["status"] == "rejected"
    assert decided["decided_by"] == "bob"


def test_an_approval_can_only_be_decided_once(store):
    created = store.create(plan=_plan(), policy=_policy())
    store.decide(created["id"], approve=True, by="alice")
    with pytest.raises(approvals.ApprovalConflict):
        store.decide(created["id"], approve=True, by="alice")


def test_deciding_an_unknown_approval_is_an_error(store):
    with pytest.raises(approvals.ApprovalError):
        store.decide("apr-00000000", approve=True)


def test_an_expired_approval_cannot_be_decided(store):
    created = store.create(plan=_plan(), policy=_policy(), timeout=60)
    # Move the deadline into the past.
    record = store.get(created["id"])
    record["expires_at"] = time.time() - 1
    store._write(record)
    with pytest.raises(approvals.ApprovalConflict):
        store.decide(created["id"], approve=True)
    assert store.get(created["id"])["status"] == "expired"


def test_expire_due_closes_the_overdue_requests(store):
    created = store.create(plan=_plan(), policy=_policy(), timeout=60)
    record = store.get(created["id"])
    record["expires_at"] = time.time() - 1
    store._write(record)
    expired = store.expire_due()
    assert [item["id"] for item in expired] == [created["id"]]
    assert store.get(created["id"])["status"] == "expired"


# --------------------------------------------------------------------------- #
# Marking
# --------------------------------------------------------------------------- #

def test_an_approved_request_can_be_marked_executed(store):
    created = store.create(plan=_plan(), policy=_policy())
    store.decide(created["id"], approve=True)
    marked = store.mark(created["id"], "executed", by="worker",
                        note="raised the limit")
    assert marked["status"] == "executed"
    assert marked["timeline"][-1]["event"] == "executed"


def test_an_unknown_status_is_refused(store):
    created = store.create(plan=_plan(), policy=_policy())
    with pytest.raises(approvals.ApprovalError):
        store.mark(created["id"], "banana")


# --------------------------------------------------------------------------- #
# Listing
# --------------------------------------------------------------------------- #

def test_the_list_is_newest_first(store):
    first = store.create(plan=_plan(plan_id="plan-1"), policy=_policy())
    time.sleep(0.01)
    second = store.create(plan=_plan(plan_id="plan-2"), policy=_policy())
    records = store.list()
    assert [item["id"] for item in records] == [second["id"], first["id"]]


def test_the_list_can_be_filtered_by_status(store):
    pending = store.create(plan=_plan(), policy=_policy())
    approved = store.create(plan=_plan(plan_id="plan-2"), policy=_policy())
    store.decide(approved["id"], approve=True)
    assert [item["id"] for item in store.list(status="pending")] == \
        [pending["id"]]
    assert [item["id"] for item in store.list(status="approved")] == \
        [approved["id"]]


def test_the_list_can_be_filtered_by_incident(store):
    store.create(plan=_plan(), policy=_policy(), incident_id="inc-1")
    store.create(plan=_plan(plan_id="plan-2"), policy=_policy(),
                 incident_id="inc-2")
    records = store.list(incident_id="inc-1")
    assert len(records) == 1
    assert records[0]["incident_id"] == "inc-1"


def test_pending_lists_only_the_waiting_ones(store):
    store.create(plan=_plan(), policy=_policy())
    decided = store.create(plan=_plan(plan_id="plan-2"), policy=_policy())
    store.decide(decided["id"], approve=True)
    assert len(store.pending()) == 1


def test_a_broken_file_is_skipped_not_fatal(store, tmp_path):
    store.create(plan=_plan(), policy=_policy())
    (store.dir / "apr-broken.json").write_text("{not json", encoding="utf-8")
    assert len(store.list()) == 1
