"""Tests for the incident store (atomic JSON records + report queue)."""
from __future__ import annotations

import json
import pathlib

import pytest

import incidents


@pytest.fixture()
def store(tmp_path):
    return incidents.IncidentStore(str(tmp_path / "incidents"))


def _make(store, **overrides):
    fields = dict(namespace="auton-apps", kind="Pod", name="api-1",
                  cause="crashloop", observation="pod is crash-looping",
                  severity="critical")
    fields.update(overrides)
    return store.create(**fields)


def test_create_get_and_list(store):
    record = _make(store)
    assert record["id"].startswith("inc-")
    assert record["status"] == "detected"
    assert record["resource"] == "pod/api-1"
    assert record["created_iso"].endswith("Z")

    loaded = store.get(record["id"])
    assert loaded["id"] == record["id"]
    assert store.get("inc-nope") is None
    assert [r["id"] for r in store.list()] == [record["id"]]


def test_list_filters_and_orders_newest_first(store):
    first = _make(store, name="a")
    second = _make(store, name="b")
    # Force a visible ordering difference without sleeping.
    second["created_at"] = first["created_at"] + 1
    store.save(second)
    store.update(first["id"], status="resolved")

    assert [r["id"] for r in store.list()] == [second["id"], first["id"]]
    resolved = store.list(status="resolved")
    assert [r["id"] for r in resolved] == [first["id"]]
    assert store.list(status="resolved,failed") == resolved
    assert store.list(status="needs_human") == []


def test_update_round_trip(store):
    record = _make(store)
    updated = store.update(record["id"], status="resolved",
                           verification="deployment ready=1/1")
    assert updated["status"] == "resolved"
    assert store.get(record["id"])["verification"] == "deployment ready=1/1"
    assert store.update("inc-missing", status="resolved") is None


def test_atomic_write_leaves_no_tmp_file(store):
    record = _make(store)
    files = list(pathlib.Path(store.directory).iterdir())
    assert sorted(p.suffix for p in files) == [".json"]
    # The record is valid JSON on disk, not a partial write.
    assert json.loads((store.directory / f"{record['id']}.json").read_text())[
        "id"] == record["id"]


def test_find_recent_respects_open_status_and_cooldown(store, monkeypatch):
    record = _make(store)
    assert store.find_recent("pod/api-1", "crashloop")["id"] == record["id"]
    assert store.find_recent("pod/api-1", "oom") is None
    assert store.find_recent("pod/api-2", "crashloop") is None

    # A closed incident is only reused inside the cooldown window.
    store.update(record["id"], status="resolved")
    now = record["updated_at"]
    assert store.find_recent("pod/api-1", "crashloop",
                             within=900)["id"] == record["id"]
    monkeypatch.setattr(incidents.time, "time", lambda: now + 1000)
    assert store.find_recent("pod/api-1", "crashloop", within=900) is None


def test_open_incident_matches_even_when_old(store, monkeypatch):
    record = _make(store)
    store.update(record["id"], status="acting")
    monkeypatch.setattr(incidents.time, "time", lambda: record["updated_at"] + 10_000)
    assert store.find_recent("pod/api-1", "crashloop", within=10)["id"] == record["id"]


def test_prune_keeps_newest(store):
    made = []
    for index in range(4):
        record = _make(store, name=f"p{index}")
        record["created_at"] = 1000 + index
        store.save(record)
        made.append(record["id"])
    store.max_records = 2
    assert store.prune() == 2
    remaining = {r["id"] for r in store.list()}
    assert remaining == {made[2], made[3]}


def test_pending_report_queue(store):
    record = _make(store)
    store.queue_report(record)
    assert [r["id"] for r in store.pending_reports()] == [record["id"]]
    store.drop_report(record["id"])
    assert store.pending_reports() == []


def test_bad_pending_file_is_dropped(store):
    record = _make(store)
    path = pathlib.Path(store._pending_dir()) / f"{record['id']}.json"
    path.write_text("{not json", encoding="utf-8")
    assert store.pending_reports() == []
    assert not path.exists()


def test_invalid_ids_are_refused(store):
    with pytest.raises(incidents.IncidentError):
        store._path("../escape")
    with pytest.raises(incidents.IncidentError):
        store.save({"id": ""})


def test_default_dir_uses_audit_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_AUDIT_DIR", str(tmp_path / "audit"))
    monkeypatch.delenv("AGENT_INCIDENTS_DIR", raising=False)
    assert incidents.default_dir() == str(tmp_path / "audit" / "incidents")
    monkeypatch.setenv("AGENT_INCIDENTS_DIR", str(tmp_path / "explicit"))
    assert incidents.default_dir() == str(tmp_path / "explicit")


def test_resource_key_normalises_kind():
    assert incidents.resource_key("Pod", "api-1") == "pod/api-1"
    assert incidents.resource_key("Deployment", "web") == "deployment/web"
