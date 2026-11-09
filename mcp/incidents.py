#!/usr/bin/env python3
"""Incident records shared by the ops controller, the HTTP API and the UI.

One JSON file per incident under ``AGENT_INCIDENTS_DIR`` (default
``$AGENT_AUDIT_DIR/incidents``), written atomically (tmp + ``os.replace``) so a
crash never leaves a half record. The directory is on the workspace PVC, so the
history survives a pod restart.

The record is deliberately plain data (no Python objects) because it is:
  * returned by ``GET /incidents`` and rendered by the UI,
  * POSTed to n8n for Slack/Telegram notifications,
  * attached to audit records via ``incident_id``.

Statuses:
  detected    problem found, no action taken yet
  acting      a playbook is running (written before the mutation)
  resolved    playbook ran and verification passed
  needs_human the agent must not fix this (secrets, config, capacity, PVC …)
  failed      the playbook errored or verification failed
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import time
import uuid

OPEN_STATUSES = ("detected", "acting")
CLOSED_STATUSES = ("resolved", "needs_human", "failed")


class IncidentError(RuntimeError):
    """The incident store is unavailable (disk full, bad path …)."""


def default_dir() -> str:
    configured = os.environ.get("AGENT_INCIDENTS_DIR", "").strip()
    if configured:
        return configured
    audit_dir = os.environ.get("AGENT_AUDIT_DIR", "").strip()
    if not audit_dir:
        audit_dir = os.path.join(os.getcwd(), ".ops")
    return os.path.join(audit_dir, "incidents")


def _iso(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts or time.time()))


def resource_key(kind: str, name: str) -> str:
    """``Pod/api-1`` -> ``pod/api-1`` (stable, matches audit/kubectl style)."""
    return f"{(kind or '').strip().lower()}/{(name or '').strip()}"


class IncidentStore:
    def __init__(self, directory: str | None = None, max_records: int = 500):
        self.directory = pathlib.Path(directory or default_dir())
        self.max_records = max(1, int(max_records))
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise IncidentError(f"cannot create incident dir {self.directory}: {exc}")

    # -- files ------------------------------------------------------------- #
    def _path(self, incident_id: str) -> pathlib.Path:
        safe = (incident_id or "").strip()
        # Ids come from uuid4().hex and file stems; anything else (path
        # separators, dot-prefixed names) is refused instead of silently
        # rewritten into a different file.
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,80}", safe):
            raise IncidentError("invalid incident id")
        return self.directory / f"{safe}.json"

    def _write(self, record: dict) -> dict:
        record["updated_at"] = round(time.time(), 3)
        record["updated_iso"] = _iso(record["updated_at"])
        path = self._path(record["id"])
        tmp = path.with_suffix(f".tmp-{os.getpid()}")
        data = json.dumps(record, indent=2, sort_keys=True)
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except OSError as exc:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise IncidentError(f"cannot write incident {record['id']}: {exc}")
        return record

    def save(self, record: dict) -> dict:
        if not record.get("id"):
            raise IncidentError("incident has no id")
        return self._write(record)

    def get(self, incident_id: str) -> dict | None:
        try:
            with open(self._path(incident_id), "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, json.JSONDecodeError):
            return None

    def delete(self, incident_id: str) -> None:
        try:
            self._path(incident_id).unlink()
        except OSError:
            pass

    # -- queries ----------------------------------------------------------- #
    def list(self, limit: int = 50, status: str = "") -> list[dict]:
        try:
            limit = max(1, min(int(limit), 500))
        except (TypeError, ValueError):
            limit = 50
        wanted = {s.strip() for s in (status or "").split(",") if s.strip()}
        records: list[dict] = []
        try:
            files = list(self.directory.glob("inc-*.json"))
        except OSError as exc:
            raise IncidentError(f"cannot list incidents: {exc}")
        for path in files:
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    record = json.load(fh)
            except (OSError, json.JSONDecodeError):
                continue
            if wanted and record.get("status") not in wanted:
                continue
            records.append(record)
        # The id tiebreaker keeps the order stable when two incidents are
        # created inside the same millisecond (glob order is arbitrary).
        records.sort(key=lambda r: (r.get("created_at") or 0, r.get("id") or ""),
                     reverse=True)
        return records[:limit]

    def find_recent(self, resource: str, cause: str,
                    within: float = 900) -> dict | None:
        """An open incident, or a closed one newer than `within` seconds."""
        cutoff = time.time() - max(0.0, float(within))
        for record in self.list(limit=500):
            if (record.get("resource") == resource
                    and record.get("cause") == cause
                    and (record.get("status") in OPEN_STATUSES
                         or (record.get("updated_at") or 0) >= cutoff)):
                return record
        return None

    # -- lifecycle --------------------------------------------------------- #
    def create(self, *, namespace: str, kind: str, name: str, cause: str,
               observation: str = "", severity: str = "warning",
               evidence: str = "") -> dict:
        now = time.time()
        record = {
            "id": f"inc-{uuid.uuid4().hex[:10]}",
            "created_at": round(now, 3),
            "created_iso": _iso(now),
            "updated_at": round(now, 3),
            "updated_iso": _iso(now),
            "namespace": namespace,
            "kind": kind,
            "name": name,
            "resource": resource_key(kind, name),
            "cause": cause,
            "severity": severity,
            "status": "detected",
            "observation": observation,
            "evidence": evidence,
            "actions": [],
            "verification": "",
            "needs_human": False,
            "report": "",
            "notified": False,
            "attempts": 0,
        }
        return self._write(record)

    def update(self, incident_id: str, **fields) -> dict | None:
        record = self.get(incident_id)
        if record is None:
            return None
        record.update(fields)
        return self._write(record)

    def prune(self) -> int:
        """Keep the newest `max_records`; delete the rest. Returns the count."""
        records = self.list(limit=500)
        removed = 0
        for record in records[self.max_records:]:
            self.delete(record["id"])
            removed += 1
        return removed

    # -- notification queue ------------------------------------------------ #
    def _pending_dir(self) -> pathlib.Path:
        path = self.directory / "_pending"
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise IncidentError(f"cannot create pending dir {path}: {exc}")
        return path

    def queue_report(self, record: dict) -> None:
        """Persist a report that the API could not accept yet (retry later)."""
        path = self._pending_dir() / f"{record['id']}.json"
        tmp = path.with_suffix(f".tmp-{os.getpid()}")
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(record, fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except OSError as exc:
            raise IncidentError(f"cannot queue report {record['id']}: {exc}")

    def pending_reports(self, limit: int = 100) -> list[dict]:
        path = self._pending_dir()
        try:
            files = sorted(path.glob("*.json"))[:max(1, int(limit))]
        except OSError as exc:
            raise IncidentError(f"cannot read pending reports: {exc}")
        records = []
        for file in files:
            try:
                with open(file, "r", encoding="utf-8") as fh:
                    records.append(json.load(fh))
            except (OSError, json.JSONDecodeError):
                self.drop_report(file.stem)
        return records

    def drop_report(self, incident_id: str) -> None:
        try:
            (self._pending_dir() / f"{incident_id}.json").unlink()
        except OSError:
            pass
