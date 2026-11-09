"""Tests for the write-ahead audit log (the gate every mutation goes through)."""
from __future__ import annotations

import json
import sqlite3

import pytest

import audit


@pytest.fixture()
def audit_home(tmp_path, monkeypatch):
    home = tmp_path / "ops"
    monkeypatch.setenv("AGENT_AUDIT_DIR", str(home))
    return home


def _jsonl(home):
    path = home / "audit.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def test_begin_and_finish_are_linked(audit_home):
    audit_id = audit.begin(
        "k8s_scale", {"deployment": "api", "replicas": 2},
        plan="fix replicas", dry_run="deployment/api scaled", incident_id="inc-1",
    )
    audit.finish(audit_id, result="scaled", status="ok")
    rows = _jsonl(audit_home)
    assert [r["phase"] for r in rows] == ["begin", "end"]
    assert rows[0]["audit_id"] == rows[1]["audit_id"] == audit_id
    assert rows[0]["incident_id"] == "inc-1"
    assert rows[0]["args"]["replicas"] == 2
    assert rows[1]["status"] == "ok"


def test_jsonl_is_authoritative_and_indexed(audit_home):
    audit_id = audit.begin("k8s_patch", {"name": "api"})
    audit.finish(audit_id, result="ok")
    with sqlite3.connect(audit_home / "audit.db") as conn:
        ids = [row[0] for row in conn.execute(
            "SELECT audit_id FROM audit ORDER BY seq")]
    assert ids == [audit_id, audit_id]


def test_recent_returns_oldest_first_and_filters_by_incident(audit_home):
    first = audit.begin("k8s_delete", {"name": "a"}, incident_id="inc-a")
    audit.finish(first)
    second = audit.begin("k8s_scale", {"name": "b"}, incident_id="inc-b")
    audit.finish(second)
    rows = audit.recent(10)
    assert [r["audit_id"] for r in rows] == [first, first, second, second]
    only_b = audit.recent(10, incident_id="inc-b")
    assert {r["audit_id"] for r in only_b} == {second}
    assert audit.incident_actions("inc-a")[0]["action"] == "k8s_delete"


def test_record_is_a_single_event(audit_home):
    audit.record("escalate", status="needs_human",
                 result="PVC delete requires a human", incident_id="inc-9")
    rows = audit.recent(5)
    assert len(rows) == 1
    assert rows[0]["phase"] == "event"
    assert rows[0]["status"] == "needs_human"


def test_unwritable_audit_dir_raises(tmp_path, monkeypatch):
    blocked = tmp_path / "not-a-dir"
    blocked.write_text("x")
    monkeypatch.setenv("AGENT_AUDIT_DIR", str(blocked))
    with pytest.raises(audit.AuditError):
        audit.begin("k8s_apply", {"manifest": "kind: ConfigMap"})


def test_default_dir_is_under_the_workspace(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_AUDIT_DIR", raising=False)
    monkeypatch.setenv("AGENT_WORKSPACE", str(tmp_path))
    audit.begin("k8s_scale", {"replicas": 1})
    assert (tmp_path / ".ops" / "audit.jsonl").is_file()


def test_fields_are_clipped(audit_home):
    audit.begin("k8s_apply", {"blob": "x" * 50_000},
                evidence="e" * 50_000, plan="p" * 50_000)
    row = audit.recent(1)[0]
    assert len(row["evidence"]) < 2_500
    assert len(row["plan"]) < 4_500
    assert len(row["evidence_hash"]) == 64
