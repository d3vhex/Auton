#!/usr/bin/env python3
"""The approval store: repair plans that wait for a human.

When the policy engine answers ``require_approval``, the repair does not run.
The plan is written here as an *approval request* and the incident moves to
``AWAITING_APPROVAL``. Nothing in the cluster changes until somebody decides.

One atomic JSON file per request, under ``AGENT_APPROVALS_DIR`` (default:
``AGENT_INCIDENTS_DIR/../approvals``), so the worker and the API share the
same directory on the workspace PVC.

    {
      "id": "apr-1f0c…",
      "incident_id": "inc-…",
      "task_id": "tsk-…",
      "plan": {…},
      "policy": {"action": "require_approval", "tier": 2, "reason": "…"},
      "status": "pending",
      "created_at": 1794602994.059,
      "expires_at": 1794606594.059,
      "decided_at": 0,
      "decided_by": "",
      "comment": "",
      "timeline": [{"ts": …, "iso": "…", "event": "created",
                    "by": "worker", "note": "…"}]
    }

States
------

``pending``    waiting for a decision
``approved``   a human said yes; the repair may run
``rejected``   a human said no; the incident goes to ``needs_human``
``expired``    nobody decided in time
``executed``   the repair ran after the approval
``cancelled``  the incident was closed before the approval was decided

Only ``pending`` may be decided, and only once: the second attempt fails
instead of silently re-running the decision.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import time

_ID = re.compile(r"^apr-[0-9a-f]{8,32}$")

#: The states an approval may be in.
STATUSES = ("pending", "approved", "rejected", "expired", "executed",
            "cancelled")

#: The states that can still be decided.
DECIDABLE = frozenset({"pending"})

#: The states that are finished.
TERMINAL = frozenset({"rejected", "expired", "executed", "cancelled"})


class ApprovalError(RuntimeError):
    """The approval store is unavailable (disk full, bad path …)."""


class ApprovalConflict(ApprovalError):
    """The approval was already decided."""


def default_dir() -> str:
    """Where the approval files live."""
    configured = os.environ.get("AGENT_APPROVALS_DIR", "").strip()
    if configured:
        return configured
    incidents_dir = os.environ.get("AGENT_INCIDENTS_DIR", "").strip()
    if incidents_dir:
        return os.path.join(os.path.dirname(incidents_dir.rstrip("/")),
                            "approvals")
    audit_dir = os.environ.get("AGENT_AUDIT_DIR", "").strip()
    if not audit_dir:
        audit_dir = os.path.join(os.getcwd(), ".ops")
    return os.path.join(audit_dir, "approvals")


def _iso(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts or time.time()))


def new_id(incident_id: str, plan_id: str) -> str:
    raw = f"{incident_id}|{plan_id}|{time.time()}".encode("utf-8")
    return "apr-" + hashlib.sha1(raw).hexdigest()[:12]


def valid_id(approval_id: str) -> bool:
    return bool(_ID.match(approval_id or ""))


class ApprovalStore:
    """Atomic, file-backed approval requests."""

    def __init__(self, directory: str | None = None, *, max_records: int = 500):
        self.dir = pathlib.Path(directory or default_dir())
        self.max_records = max(50, int(max_records))

    # -- plumbing --------------------------------------------------------- #
    def _path(self, approval_id: str) -> pathlib.Path:
        if not valid_id(approval_id):
            raise ApprovalError(f"invalid approval id {approval_id!r}")
        return self.dir / f"{approval_id}.json"

    def _write(self, record: dict) -> dict:
        path = self._path(record["id"])
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(record, fh, indent=2, sort_keys=True)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except OSError as exc:
            raise ApprovalError(f"cannot write {path}: {exc}") from None
        return record

    @staticmethod
    def _note(record: dict, event: str, by: str = "", note: str = "") -> None:
        record.setdefault("timeline", []).append({
            "ts": round(time.time(), 3),
            "iso": _iso(),
            "event": event,
            "by": by or "system",
            "note": note,
        })

    # -- writes ----------------------------------------------------------- #
    def create(self, *, plan: dict, policy: dict, incident_id: str = "",
               task_id: str = "", timeout: int = 3600, by: str = "worker",
               note: str = "") -> dict:
        """Create a pending approval request for a plan."""
        now = time.time()
        approval_id = new_id(incident_id, str(plan.get("plan_id") or ""))
        record = {
            "id": approval_id,
            "incident_id": incident_id or "",
            "task_id": task_id or "",
            "plan": dict(plan or {}),
            "policy": dict(policy or {}),
            "status": "pending",
            "created_at": round(now, 3),
            "expires_at": round(now + max(60, int(timeout)), 3),
            "decided_at": 0,
            "decided_by": "",
            "comment": "",
        }
        self._note(record, "created", by,
                   note or f"tier {plan.get('tier', '?')} needs an approval")
        self._write(record)
        return record

    def decide(self, approval_id: str, *, approve: bool, by: str = "api",
               comment: str = "") -> dict:
        """Approve or reject a pending request.

        Raises :class:`ApprovalConflict` when the request was already decided:
        a second click must never run the repair twice.
        """
        record = self.get(approval_id)
        if record is None:
            raise ApprovalError(f"unknown approval {approval_id}")
        if record.get("status") not in DECIDABLE:
            raise ApprovalConflict(
                f"approval {approval_id} is already {record.get('status')}")
        now = time.time()
        expires = float(record.get("expires_at") or 0)
        if expires and now > expires:
            record["status"] = "expired"
            record["decided_at"] = round(now, 3)
            record["decided_by"] = by
            record["comment"] = comment
            self._note(record, "expired", by,
                       "the approval expired before it was decided")
            self._write(record)
            raise ApprovalConflict(
                f"approval {approval_id} expired at {_iso(expires)}")
        record["status"] = "approved" if approve else "rejected"
        record["decided_at"] = round(now, 3)
        record["decided_by"] = by
        record["comment"] = str(comment or "")[:500]
        self._note(record, record["status"], by, record["comment"])
        return self._write(record)

    def mark(self, approval_id: str, status: str, *, by: str = "system",
             note: str = "") -> dict:
        """Move an approval to a finished state (``executed``/``cancelled``)."""
        if status not in STATUSES:
            raise ApprovalError(f"unknown approval status {status!r}")
        record = self.get(approval_id)
        if record is None:
            raise ApprovalError(f"unknown approval {approval_id}")
        record["status"] = status
        self._note(record, status, by, note)
        return self._write(record)

    def expire_due(self, *, now: float | None = None) -> list[dict]:
        """Expire every request whose deadline has passed."""
        moment = now if now is not None else time.time()
        expired: list[dict] = []
        for record in self.list(status="pending"):
            expires = float(record.get("expires_at") or 0)
            if expires and moment > expires:
                record["status"] = "expired"
                record["decided_at"] = round(moment, 3)
                record["decided_by"] = "system"
                self._note(record, "expired", "system",
                           "the approval expired before it was decided")
                try:
                    expired.append(self._write(record))
                except ApprovalError:
                    continue
        return expired

    # -- reads ------------------------------------------------------------ #
    def get(self, approval_id: str) -> dict | None:
        path = self._path(approval_id)
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as exc:
            raise ApprovalError(f"cannot read {path}: {exc}") from None

    def list(self, *, status: str = "", incident_id: str = "",
             limit: int = 50) -> list[dict]:
        """The approval requests, newest first."""
        wanted = {part.strip() for part in str(status or "").split(",")
                  if part.strip()}
        records: list[dict] = []
        try:
            paths = sorted(self.dir.glob("apr-*.json"))
        except OSError as exc:
            raise ApprovalError(f"cannot list {self.dir}: {exc}") from None
        for path in paths:
            try:
                with open(path, encoding="utf-8") as fh:
                    record = json.load(fh)
            except (OSError, json.JSONDecodeError):
                continue
            if wanted and record.get("status") not in wanted:
                continue
            if incident_id and record.get("incident_id") != incident_id:
                continue
            records.append(record)
        records.sort(key=lambda item: float(item.get("created_at") or 0),
                     reverse=True)
        if limit and limit > 0:
            records = records[:int(limit)]
        return records

    def pending(self, *, limit: int = 50) -> list[dict]:
        return self.list(status="pending", limit=limit)

    def prune(self, *, keep: int = 0) -> int:
        """Delete the oldest finished requests; returns how many were removed."""
        keep = max(0, int(keep))
        records = self.list(limit=0)
        finished = [item for item in records if item.get("status") in TERMINAL]
        finished.sort(key=lambda item: float(item.get("created_at") or 0))
        removed = 0
        for record in finished[:max(0, len(finished) - keep)]:
            try:
                self._path(record["id"]).unlink()
                removed += 1
            except OSError:
                continue
        return removed
