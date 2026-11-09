#!/usr/bin/env python3
"""Append-only audit log for agent mutations (JSONL + SQLite index).

The rule is **write-ahead**: a mutating tool must call ``begin()`` *before* it
runs kubectl, and the call is refused if the record cannot be written. That is
what makes "nothing happens without a record" a property of the code rather
than a promise in the prompt.

Layout (under ``AGENT_AUDIT_DIR``, default ``$AGENT_WORKSPACE/.ops``)::

    audit.jsonl   append-only, one JSON object per phase (begin/end), fsynced
    audit.db      SQLite index for queries (same rows, WAL mode)

Every record carries an ``audit_id`` linking the begin/end pair; incidents (see
``incidents.py``) reference those ids. Args are stored as JSON with ``default=str``
so a stray object cannot break the log; callers must not pass secrets.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import sqlite3
import threading
import time
import uuid

MAX_FIELD = 4000          # per text field; enough for a kubectl error, cheap on disk
MAX_EVIDENCE = 2000

_LOCK = threading.RLock()


class AuditError(RuntimeError):
    """The audit log could not be written; mutations must stop."""


def base_dir() -> pathlib.Path:
    raw = os.environ.get("AGENT_AUDIT_DIR", "").strip()
    if raw:
        return pathlib.Path(raw).expanduser()
    workspace = os.environ.get("AGENT_WORKSPACE", "./workspace")
    return pathlib.Path(workspace).expanduser() / ".ops"


def _paths() -> tuple[pathlib.Path, pathlib.Path]:
    directory = base_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise AuditError(f"cannot create audit dir {directory}: {exc}") from exc
    if not directory.is_dir():
        raise AuditError(f"audit dir {directory} is not a directory")
    return directory / "audit.jsonl", directory / "audit.db"


def _clip(value: str, limit: int = MAX_FIELD) -> str:
    value = str(value or "")
    if len(value) <= limit:
        return value
    return value[:limit] + f"… ({len(value) - limit} chars clipped)"


def _clean(value, limit: int = MAX_FIELD):
    """Truncate long strings inside an args payload (keep the shape)."""
    if isinstance(value, str):
        return _clip(value, limit)
    if isinstance(value, dict):
        return {str(k): _clean(v, limit) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v, limit) for v in value]
    return value


def _index(record: dict, db_path: pathlib.Path) -> None:
    """Mirror a record into SQLite. The JSONL is authoritative; the index is
    best-effort so a locked DB cannot block an otherwise-audited action."""
    try:
        with sqlite3.connect(str(db_path), timeout=5) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS audit ("
                " seq INTEGER PRIMARY KEY AUTOINCREMENT,"
                " audit_id TEXT NOT NULL, ts REAL NOT NULL, phase TEXT NOT NULL,"
                " action TEXT, actor TEXT, incident_id TEXT, status TEXT,"
                " args TEXT, plan TEXT, dry_run TEXT, result TEXT,"
                " evidence TEXT, evidence_hash TEXT)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS audit_ts ON audit(ts)"
            )
            conn.execute(
                "INSERT INTO audit (audit_id, ts, phase, action, actor,"
                " incident_id, status, args, plan, dry_run, result, evidence,"
                " evidence_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    record["audit_id"], record["ts"], record["phase"],
                    record.get("action"), record.get("actor"),
                    record.get("incident_id"), record.get("status"),
                    json.dumps(record.get("args"), default=str),
                    record.get("plan"), record.get("dry_run"),
                    record.get("result"), record.get("evidence"),
                    record.get("evidence_hash"),
                ),
            )
    except sqlite3.Error:
        # Log to stderr, never block: the append-only file already has the row.
        print(f"[audit] sqlite index write failed ({db_path})", flush=True)


def _append(record: dict) -> None:
    jsonl, db_path = _paths()
    line = json.dumps(record, sort_keys=True, default=str) + "\n"
    try:
        with _LOCK:
            with open(jsonl, "a", encoding="utf-8") as fh:
                fh.write(line)
                fh.flush()
                os.fsync(fh.fileno())
    except OSError as exc:
        raise AuditError(f"cannot write {jsonl}: {exc}") from exc
    _index(record, db_path)


def begin(action: str, args: dict | None = None, *, plan: str = "",
          dry_run: str = "", incident_id: str = "", actor: str = "agent",
          evidence: str = "") -> str:
    """Write the intent record and return its audit id.

    Raises ``AuditError`` if the record cannot be persisted — callers must then
    refuse the mutation.
    """
    evidence = _clip(evidence or "", MAX_EVIDENCE)
    record = {
        "audit_id": uuid.uuid4().hex[:16],
        "ts": round(time.time(), 3),
        "phase": "begin",
        "action": (action or "").strip(),
        "actor": (actor or "agent").strip(),
        "incident_id": (incident_id or "").strip(),
        "args": _clean(args or {}),
        "plan": _clip(plan),
        "dry_run": _clip(dry_run),
        "evidence": evidence,
        "evidence_hash": (hashlib.sha256(evidence.encode("utf-8")).hexdigest()
                          if evidence else ""),
        "status": "started",
    }
    _append(record)
    return record["audit_id"]


def finish(audit_id: str, *, result: str = "", status: str = "ok",
           actor: str = "agent", incident_id: str = "") -> None:
    """Write the outcome record (best effort: the action already happened)."""
    record = {
        "audit_id": audit_id,
        "ts": round(time.time(), 3),
        "phase": "end",
        "action": "",
        "actor": actor,
        "incident_id": (incident_id or "").strip(),
        "status": (status or "ok").strip(),
        "result": _clip(result),
    }
    try:
        _append(record)
    except AuditError as exc:
        print(f"[audit] CRITICAL: could not record outcome for {audit_id}: {exc}",
              flush=True)
        raise


def record(action: str, *, status: str = "ok", result: str = "",
           args: dict | None = None, actor: str = "agent",
           incident_id: str = "") -> str:
    """Single-shot event (observations, decisions, escalations)."""
    record = {
        "audit_id": uuid.uuid4().hex[:16],
        "ts": round(time.time(), 3),
        "phase": "event",
        "action": (action or "").strip(),
        "actor": (actor or "agent").strip(),
        "incident_id": (incident_id or "").strip(),
        "args": _clean(args or {}),
        "status": (status or "ok").strip(),
        "result": _clip(result),
    }
    _append(record)
    return record["audit_id"]


def _read_jsonl(limit: int) -> list[dict]:
    jsonl, _ = _paths()
    if not jsonl.exists():
        return []
    rows: list[dict] = []
    try:
        with open(jsonl, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError as exc:
        raise AuditError(f"cannot read {jsonl}: {exc}") from exc
    return rows[-limit:]


def recent(limit: int = 50, incident_id: str = "") -> list[dict]:
    """Last N audit records, oldest first; optionally filtered by incident."""
    limit = max(1, min(int(limit), 500))
    rows = [r for r in _read_jsonl(limit if not incident_id else 5000)
            if not incident_id or r.get("incident_id") == incident_id]
    return rows[-limit:]


def incident_actions(incident_id: str) -> list[dict]:
    """Every begin/end pair for one incident (used by the report builder)."""
    return [r for r in recent(500, incident_id=incident_id)
            if r.get("phase") in ("begin", "end", "event")]
