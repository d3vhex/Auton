"""Tests for the Incident Manager v2 (model, fingerprint, state machine)."""
from __future__ import annotations

import pytest

import incidents


@pytest.fixture()
def store(tmp_path):
    return incidents.IncidentStore(str(tmp_path / "incidents"))


def _make(store, **overrides):
    fields = dict(namespace="auton-apps", kind="Pod", name="api-1",
                  cause="oom", observation="pod reports OOMKilled",
                  severity="critical")
    fields.update(overrides)
    return store.create(**fields)


# --------------------------------------------------------------------------- #
# Model
# --------------------------------------------------------------------------- #

def test_record_carries_the_v2_fields(store):
    record = _make(store)
    assert record["id"].startswith("inc-")
    assert record["incident_id"] == record["id"]
    assert record["fingerprint"]
    assert record["cluster_id"] == "default"
    assert record["type"] == "oom"
    assert record["severity"] == "critical"
    assert record["status"] == "detected"
    assert record["state"] == "DETECTED"
    assert record["resource_kind"] == "Pod"
    assert record["resource_name"] == "api-1"
    assert record["resource"] == "pod/api-1"
    assert record["first_seen"] == record["created_at"]
    assert record["last_seen"] == record["created_at"]
    assert record["occurrence_count"] == 1
    assert record["diagnosis"] is None
    assert record["repair_plan"] is None
    assert record["resolution"] is None


def test_v1_and_v2_names_stay_in_sync(store):
    record = _make(store)
    record["kind"] = "Deployment"
    record["name"] = "web"
    store.save(record)
    loaded = store.get(record["id"])
    assert loaded["resource_kind"] == "Deployment"
    assert loaded["resource_name"] == "web"
    assert loaded["resource"] == "deployment/web"


# --------------------------------------------------------------------------- #
# Fingerprint
# --------------------------------------------------------------------------- #

def test_fingerprint_is_stable():
    first = incidents.fingerprint("Pod", "auton-apps", "api-1", "oom")
    second = incidents.fingerprint("Pod", "auton-apps", "api-1", "oom")
    assert first == second
    assert len(first) == 16


def test_fingerprint_separates_resource_and_problem():
    base = incidents.fingerprint("Pod", "auton-apps", "api-1", "oom")
    assert base != incidents.fingerprint("Pod", "auton-apps", "api-2", "oom")
    assert base != incidents.fingerprint("Pod", "auton-apps", "api-1", "crashloop")
    assert base != incidents.fingerprint("Pod", "other", "api-1", "oom")
    assert base != incidents.fingerprint("Deployment", "auton-apps", "api-1", "oom")


def test_fingerprint_ignores_the_kind_case():
    assert (incidents.fingerprint("Pod", "ns", "n", "oom")
            == incidents.fingerprint("pod", "ns", "n", "oom"))


def test_same_problem_on_the_same_resource_shares_a_fingerprint(store):
    first = _make(store, name="api-1")
    second = _make(store, name="api-2")
    # The fingerprint is the *problem* on the resource, so two different
    # pods are two different fingerprints.
    assert first["fingerprint"] != second["fingerprint"]


def test_find_by_fingerprint(store):
    record = _make(store)
    found = store.find_by_fingerprint(record["fingerprint"])
    assert found is not None
    assert found["id"] == record["id"]
    assert store.find_by_fingerprint("does-not-exist") is None


# --------------------------------------------------------------------------- #
# Deduplication and occurrence counting
# --------------------------------------------------------------------------- #

def test_note_seen_counts_occurrences_without_a_new_incident(store):
    record = _make(store)
    updated = store.note_seen(record["id"], note="seen again")
    assert updated["occurrence_count"] == 2
    assert updated["last_seen"] >= updated["first_seen"]
    assert len(store.list()) == 1


def test_a_recurrence_opens_a_new_incident_linked_to_the_old_one(store):
    first = _make(store)
    store.update(first["id"], status="resolved")

    second = store.create(
        namespace="auton-apps", kind="Pod", name="api-1", cause="oom",
        severity="critical", fingerprint_value=first["fingerprint"],
        previous=first)
    assert second["id"] != first["id"]
    assert second["previous_incident_id"] == first["id"]
    assert second["occurrence_count"] == 2
    assert second["first_seen"] >= first["first_seen"]


def test_link_recurrence_records_the_follow_up(store):
    first = _make(store)
    second = _make(store, name="api-2")
    store.link_recurrence(first["id"], second["id"])
    assert second["id"] in store.get(first["id"])["recurrence_ids"]


# --------------------------------------------------------------------------- #
# State machine
# --------------------------------------------------------------------------- #

def test_legal_transition_is_recorded_in_the_timeline(store):
    record = _make(store)
    moved = store.transition(record["id"], "triage", by="scanner",
                             note="triaged")
    assert moved["status"] == "triage"
    assert moved["state"] == "TRIAGING"
    timeline = moved["timeline"]
    assert timeline[-1]["to"] == "TRIAGING"
    assert timeline[-1]["by"] == "scanner"
    assert timeline[-1]["note"] == "triaged"


def test_illegal_transition_raises_instead_of_writing(store):
    record = _make(store)
    with pytest.raises(incidents.TransitionError):
        store.transition(record["id"], "resolved")
    # Nothing was written.
    assert store.get(record["id"])["status"] == "detected"


def test_a_closed_incident_may_not_be_silently_reopened(store):
    record = _make(store)
    store.update(record["id"], status="resolved")
    with pytest.raises(incidents.TransitionError):
        store.update(record["id"], status="detected")
    assert store.get(record["id"])["status"] == "resolved"


def test_reopen_is_the_way_back_from_a_closed_incident(store):
    record = _make(store)
    store.update(record["id"], status="failed")
    moved = store.reopen(record["id"], "queued", by="worker",
                         note="retry")
    assert moved["status"] == "queued"
    # The timeline always says it was a reopen.
    assert "reopened" in moved["timeline"][-1]["note"]
    assert "retry" in moved["timeline"][-1]["note"]
    assert moved["timeline"][-1]["from"] == "FAILED"
    assert moved["timeline"][-1]["to"] == "QUEUED"


def test_a_failed_repair_may_escalate_to_a_human(store):
    record = _make(store)
    store.update(record["id"], status="failed")
    moved = store.transition(record["id"], "needs_human")
    assert moved["status"] == "needs_human"


def test_path_to_follows_the_pipeline():
    path = incidents.path_to("detected", "executing")
    assert path == ["triage", "diagnosing", "planning", "policy_check",
                    "queued", "executing"]
    assert incidents.path_to("executing", "resolved") == ["verifying", "resolved"]
    # Nothing leads out of a closed incident.
    assert incidents.path_to("resolved", "queued") == []
    assert incidents.path_to("failed", "needs_human") == ["needs_human"]


def test_path_to_rejects_unknown_states():
    assert incidents.path_to("detected", "nonsense") == []


# --------------------------------------------------------------------------- #
# Auto-resolve
# --------------------------------------------------------------------------- #

def test_one_healthy_pass_does_not_close_an_incident(store):
    record = _make(store)
    updated = store.note_healthy(record["id"], threshold=3)
    assert updated["status"] == "detected"
    assert updated["healthy_checks"] == 1


def test_n_healthy_passes_close_the_incident(store):
    record = _make(store)
    for _ in range(3):
        updated = store.note_healthy(record["id"], threshold=3)
    assert updated["status"] == "resolved"
    assert updated["state"] == "RESOLVED"
    assert updated["resolution"]["reason"] == "healthy"
    assert updated["resolution"]["healthy_checks"] == 3


def test_a_healthy_pass_after_a_close_stays_closed(store):
    record = _make(store)
    for _ in range(3):
        updated = store.note_healthy(record["id"], threshold=3)
    again = store.note_healthy(record["id"], threshold=3)
    assert again["status"] == "resolved"


# --------------------------------------------------------------------------- #
# Severity history
# --------------------------------------------------------------------------- #

def test_severity_changes_are_recorded(store):
    record = _make(store)
    updated = store.set_severity(record["id"], "warning", by="scanner",
                                 note="less severe now")
    assert updated["severity"] == "warning"
    changes = updated["severity_changes"]
    assert changes[-1]["from"] == "critical"
    assert changes[-1]["to"] == "warning"
    assert changes[-1]["note"] == "less severe now"


# --------------------------------------------------------------------------- #
# Queries
# --------------------------------------------------------------------------- #

def test_list_filters_by_severity_cause_and_namespace(store):
    _make(store, name="a", severity="critical", cause="oom")
    _make(store, name="b", severity="warning", cause="crashloop")
    _make(store, name="c", namespace="other", severity="critical", cause="oom")

    assert len(store.list(severity="critical")) == 2
    assert len(store.list(cause="crashloop")) == 1
    assert len(store.list(namespace="other")) == 1
    assert len(store.list(severity="critical,warning")) == 3


def test_open_incidents_lists_the_live_ones(store):
    open_record = _make(store, name="open")
    closed = _make(store, name="closed")
    store.update(closed["id"], status="resolved")
    assert [r["id"] for r in store.open_incidents()] == [open_record["id"]]


def test_timeline_starts_with_the_incident_opening(store):
    record = _make(store)
    timeline = record["timeline"]
    assert timeline
    assert timeline[0]["to"] == "DETECTED"
    assert timeline[0]["from"] == ""


# --------------------------------------------------------------------------- #
# Index
# --------------------------------------------------------------------------- #

def test_index_mirrors_records_and_can_be_rebuilt(tmp_path):
    import ops_queue

    backend = ops_queue.MemoryBackend()
    store = incidents.IncidentStore(str(tmp_path / "incidents"), index=backend)
    record = _make(store)
    key = store._index_key("fingerprint", record["fingerprint"])
    assert backend.get(key) == record["id"]
    assert backend.zcard(store._index_key("all")) == 1

    # A fresh store can rebuild the index from the files.
    other = incidents.IncidentStore(str(tmp_path / "incidents"), index=backend)
    assert other.rebuild_index() == 1


def test_an_index_failure_never_blocks_a_write(tmp_path):
    class Broken:
        def set(self, *args, **kwargs):
            raise RuntimeError("index is down")

        def zadd(self, *args, **kwargs):
            raise RuntimeError("index is down")

    store = incidents.IncidentStore(str(tmp_path / "incidents"), index=Broken())
    record = _make(store)
    assert store.get(record["id"])["id"] == record["id"]
