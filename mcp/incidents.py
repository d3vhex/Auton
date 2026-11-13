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

Model v2 (the autonomous-operations contract)
----------------------------------------------
Every incident carries the fields the platform spec requires::

    {
      "incident_id": "inc-1a2b3c4d5e",
      "fingerprint": "9f2c1a8b4d7e6035",
      "cluster_id": "default",
      "type": "OOMKilled",
      "severity": "critical",
      "status": "detected",
      "namespace": "auton-apps",
      "resource_kind": "Pod",
      "resource_name": "api-7fd89",
      "resource_uid": "…",
      "first_seen": 1794588911.1,
      "last_seen": 1794588913.7,
      "occurrence_count": 1,
      "diagnosis": null,
      "repair_plan": null,
      "resolution": null
    }

The v1 fields (``id``, ``kind``, ``name``, ``resource``, ``cause``,
``evidence``, ``actions``, ``verification``, ``report`` …) are all still there
and still authoritative, so the HTTP API, the UI, the n8n notification payload
and the existing tests keep working unchanged. The v2 names are additive
aliases: ``incident_id`` mirrors ``id``, ``resource_kind``/``resource_name``
mirror ``kind``/``name``.

The lifecycle is the 12-state machine from the spec::

    DETECTED → TRIAGING → DIAGNOSING → PLANNING → POLICY_CHECK
                  │            │            │           ├─→ AWAITING_APPROVAL
                  │            │            │           ├─→ SUPPRESSED
                  │            │            │           ↓
                  │            │            │         QUEUED → EXECUTING → VERIFYING
                  │            │            │                        ├─→ RESOLVED
                  │            │            │                        ├─→ FAILED
                  └────────────┴────────────┴────────────────────────┴─→ NEEDS_HUMAN

``status`` keeps the lowercase spelling the API already exposed (``acting`` is
the legacy name for ``executing``); ``state`` carries the canonical uppercase
name from the spec.

Two different guarantees are enforced, on purpose:

* ``transition()`` walks the strict adjacency table above. An illegal move
  raises ``IncidentError`` — it is never a silent write.
* ``save()`` guards the *terminal* states only: a closed incident may not be
  re-opened by accident. That check lives at the single choke point (the file
  write) so that legacy code paths which assign ``record["status"]`` directly
  cannot bypass it.

Deduplication is by fingerprint (``sha1(cluster|kind|namespace|name|cause)``):
the same problem on the same resource is one incident, no matter how many
scanners saw it or how often it is reported. While the incident is open, a
repeat sighting bumps ``occurrence_count`` and ``last_seen`` instead of
creating a second record. After it closed, a recurrence opens a *new* incident
that points back at the previous one (``previous_incident_id``) and continues
the occurrence count.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import time
import uuid

# --------------------------------------------------------------------------- #
# States
# --------------------------------------------------------------------------- #

#: Canonical states, in the order the pipeline moves through them.
STATES = (
    "detected",
    "triage",
    "diagnosing",
    "planning",
    "policy_check",
    "awaiting_approval",
    "suppressed",
    "queued",
    "executing",
    "verifying",
    "resolved",
    "failed",
    "needs_human",
)

#: The spec's uppercase spelling, stored in the record as ``state``.
STATE_LABELS = {
    "detected": "DETECTED",
    "triage": "TRIAGING",
    "diagnosing": "DIAGNOSING",
    "planning": "PLANNING",
    "policy_check": "POLICY_CHECK",
    "awaiting_approval": "AWAITING_APPROVAL",
    "suppressed": "SUPPRESSED",
    "queued": "QUEUED",
    "executing": "EXECUTING",
    "verifying": "VERIFYING",
    "resolved": "RESOLVED",
    "failed": "FAILED",
    "needs_human": "NEEDS_HUMAN",
}

#: ``executing`` was called ``acting`` before the pipeline was split.
LEGACY_STATUS = {"executing": "acting"}
LEGACY_TO_STATE = {legacy: state for state, legacy in LEGACY_STATUS.items()}

#: States in which the problem is still live.
OPEN_STATUSES = (
    "detected", "triage", "diagnosing", "planning", "policy_check",
    "awaiting_approval", "suppressed", "queued", "acting", "verifying",
)
#: States in which the incident is closed.
CLOSED_STATUSES = ("resolved", "failed", "needs_human")
#: States that may not be left without an explicit recurrence/reopen.
TERMINAL_STATES = frozenset(CLOSED_STATUSES)

#: Strict adjacency table (the spec's graph). ``transition()`` enforces it.
TRANSITIONS: dict[str, frozenset[str]] = {
    "detected": frozenset({"triage", "suppressed", "needs_human", "failed"}),
    "triage": frozenset({"diagnosing", "suppressed", "needs_human", "failed"}),
    "diagnosing": frozenset({"planning", "needs_human", "failed"}),
    "planning": frozenset({"policy_check", "needs_human", "failed"}),
    "policy_check": frozenset({"queued", "awaiting_approval", "suppressed",
                               "needs_human", "failed"}),
    "awaiting_approval": frozenset({"queued", "suppressed", "needs_human",
                                     "failed"}),
    "suppressed": frozenset(),
    "queued": frozenset({"executing", "failed", "needs_human"}),
    "executing": frozenset({"verifying", "failed", "needs_human"}),
    "verifying": frozenset({"resolved", "failed", "needs_human"}),
    "resolved": frozenset(),
    # A failed repair that ran out of retries escalates to a human.
    "failed": frozenset({"needs_human"}),
    "needs_human": frozenset(),
}

#: How many consecutive healthy reconciliations close an open incident.
HEALTHY_THRESHOLD = max(1, int(
    os.environ.get("AGENT_OPS_HEALTHY_THRESHOLD", "3")))


class IncidentError(RuntimeError):
    """The incident store is unavailable (disk full, bad path …)."""


class TransitionError(IncidentError):
    """An illegal state transition was attempted."""


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


def cluster_id() -> str:
    """Identity of the cluster this incident belongs to."""
    return os.environ.get("AGENT_CLUSTER_ID", "").strip() or "default"


def resource_key(kind: str, name: str) -> str:
    """``Pod/api-1`` -> ``pod/api-1`` (stable, matches audit/kubectl style)."""
    return f"{(kind or '').strip().lower()}/{(name or '').strip()}"


def fingerprint(kind: str, namespace: str, name: str, cause: str,
                cluster: str = "") -> str:
    """Stable hash of *which problem on which resource*.

    Two scanners that see the same failure compute the same fingerprint, so
    the problem collapses into one incident. The cause is part of the hash: an
    OOMKill and a CrashLoop on the same pod are different problems.
    """
    raw = "|".join([
        (cluster or cluster_id()).strip(),
        (kind or "").strip().lower(),
        (namespace or "").strip(),
        (name or "").strip(),
        (cause or "").strip().lower(),
    ])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def normalize_state(status: str) -> str:
    """Map any accepted spelling onto a canonical state."""
    value = (status or "").strip().lower().replace("-", "_")
    if value in LEGACY_TO_STATE:
        return LEGACY_TO_STATE[value]
    return value


def status_for(state: str) -> str:
    """The lowercase status the API/UI have always exposed."""
    return LEGACY_STATUS.get(state, state)


def path_to(current: str, target: str) -> list[str]:
    """The shortest legal path of states from ``current`` to ``target``.

    Returns an empty list when the target is unreachable from the current
    state (for example out of a terminal state), so the caller can decide
    whether an explicit reopen is allowed.
    """
    start = normalize_state(current)
    goal = normalize_state(target)
    if not start or not goal:
        return []
    if start == goal:
        return []
    if goal not in STATES or start not in STATES:
        return []
    queue: list[tuple[str, list[str]]] = [(start, [])]
    seen = {start}
    while queue:
        state, path = queue.pop(0)
        for nxt in sorted(TRANSITIONS.get(state, ())):
            if nxt in seen:
                continue
            if nxt == goal:
                return path + [nxt]
            seen.add(nxt)
            queue.append((nxt, path + [nxt]))
    return []


def state_label(state: str) -> str:
    """The spec's uppercase name for a state."""
    value = normalize_state(state)
    return STATE_LABELS.get(value, value.upper())


class IncidentStore:
    def __init__(self, directory: str | None = None, max_records: int = 500,
                 index=None):
        self.directory = pathlib.Path(directory or default_dir())
        self.max_records = max(1, int(max_records))
        # Optional live index (a queue backend). The JSON files stay the
        # source of truth; the index is a rebuildable accelerator.
        self.index = index
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
        self._index_write(record)
        return record

    # -- live index (optional, rebuildable) -------------------------------- #
    def _index_key(self, *parts: str) -> str:
        return ":".join(["auton", "inc", *[str(p) for p in parts]])

    def _index_write(self, record: dict) -> None:
        """Mirror the record into the index; failures never block a write."""
        if self.index is None:
            return
        try:
            incident_id = record.get("id")
            fingerprint_value = record.get("fingerprint")
            state = normalize_state(record.get("status", ""))
            score = float(record.get("created_at") or 0)
            if fingerprint_value:
                self.index.set(self._index_key("fingerprint", fingerprint_value),
                               incident_id)
            self.index.zadd(self._index_key("all"), score, incident_id)
            if state:
                self.index.zadd(self._index_key("status", state), score,
                                incident_id)
        except Exception:
            # The index is an accelerator, never the source of truth.
            pass

    def _index_drop(self, incident_id: str) -> None:
        if self.index is None:
            return
        try:
            for key in self.index.scan(self._index_key("status", "*")):
                self.index.zrem(key, incident_id)
            self.index.zrem(self._index_key("all"), incident_id)
        except Exception:
            pass

    def rebuild_index(self) -> int:
        """Regenerate the index from the JSON files; returns the count."""
        if self.index is None:
            return 0
        count = 0
        for record in self.list(limit=500):
            self._index_write(record)
            count += 1
        return count

    # -- reads ------------------------------------------------------------- #
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
        self._index_drop(incident_id)

    # -- queries ----------------------------------------------------------- #
    def list(self, limit: int = 50, status: str = "", severity: str = "",
             cause: str = "", namespace: str = "", resource: str = "",
             fingerprint: str = "") -> list[dict]:
        """Filtered, newest-first incident list.

        The filters are the ones the dashboard needs (spec §4: incidents must
        be queryable and filterable, not just stored as text).
        """
        try:
            limit = max(1, min(int(limit), 500))
        except (TypeError, ValueError):
            limit = 50
        wanted = {s.strip() for s in (status or "").split(",") if s.strip()}
        wanted_severity = {s.strip() for s in (severity or "").split(",")
                           if s.strip()}
        wanted_cause = {s.strip() for s in (cause or "").split(",") if s.strip()}
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
            if wanted_severity and record.get("severity") not in wanted_severity:
                continue
            if wanted_cause and record.get("cause") not in wanted_cause:
                continue
            if namespace and record.get("namespace") != namespace:
                continue
            if resource and record.get("resource") != resource:
                continue
            if fingerprint and record.get("fingerprint") != fingerprint:
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

    def find_by_fingerprint(self, fingerprint_value: str,
                            include_closed: bool = True) -> dict | None:
        """The incident that owns this fingerprint (newest first)."""
        if not fingerprint_value:
            return None
        for record in self.list(limit=500, fingerprint=fingerprint_value):
            if include_closed or record.get("status") in OPEN_STATUSES:
                return record
        return None

    def open_incidents(self, limit: int = 500) -> list[dict]:
        return self.list(limit=limit, status=",".join(OPEN_STATUSES))

    # -- lifecycle --------------------------------------------------------- #
    def create(self, *, namespace: str, kind: str, name: str, cause: str,
               observation: str = "", severity: str = "warning",
               evidence: str = "", incident_type: str = "",
               resource_uid: str = "", fingerprint_value: str = "",
               cluster: str = "", previous: dict | None = None,
               **extra) -> dict:
        """Create one incident.

        ``previous`` links a recurrence to the incident it came back from: the
        new record points at the old one, continues its occurrence count and
        the old one records the recurrence.
        """
        now = time.time()
        fingerprint_value = fingerprint_value or fingerprint(
            kind, namespace, name, cause, cluster)
        incident_id = f"inc-{uuid.uuid4().hex[:10]}"
        occurrence = 1
        previous_id = ""
        if previous:
            previous_id = previous.get("id", "")
            try:
                occurrence = int(previous.get("occurrence_count") or 1) + 1
            except (TypeError, ValueError):
                occurrence = 2
        record = {
            "id": incident_id,
            "incident_id": incident_id,
            "fingerprint": fingerprint_value,
            "cluster_id": cluster or cluster_id(),
            "type": incident_type or cause,
            "created_at": round(now, 3),
            "created_iso": _iso(now),
            "updated_at": round(now, 3),
            "updated_iso": _iso(now),
            "first_seen": round(now, 3),
            "first_seen_iso": _iso(now),
            "last_seen": round(now, 3),
            "last_seen_iso": _iso(now),
            "occurrence_count": occurrence,
            "namespace": namespace,
            "kind": kind,
            "name": name,
            "resource_kind": kind,
            "resource_name": name,
            "resource_uid": resource_uid,
            "resource": resource_key(kind, name),
            "cause": cause,
            "severity": severity,
            "status": "detected",
            "state": STATE_LABELS["detected"],
            "observation": observation,
            "evidence": evidence,
            "evidence_structured": None,
            "diagnosis": None,
            "repair_plan": None,
            "resolution": None,
            "actions": [],
            "verification": "",
            "needs_human": False,
            "report": "",
            "notified": False,
            "attempts": 0,
            "timeline": [],
            "healthy_checks": 0,
            "previous_incident_id": previous_id,
            "recurrence_ids": [],
            "severity_changes": [],
        }
        record.update(extra)
        self._sync_aliases(record)
        self._append_timeline(record, from_state="", to_state="detected",
                              by=extra.get("source", "") or "system",
                              note=observation or "incident opened")
        return self._write(record)

    def update(self, incident_id: str, **fields) -> dict | None:
        record = self.get(incident_id)
        if record is None:
            return None
        record.update(fields)
        # Keep the v2 aliases consistent with whatever the caller changed.
        self._sync_aliases(record)
        self._guard_transition(record)
        return self._write(record)

    def save(self, record: dict) -> dict:
        """Persist a record, enforcing the terminal-state guard."""
        if not record.get("id"):
            raise IncidentError("incident has no id")
        self._sync_aliases(record)
        self._guard_transition(record)
        return self._write(record)

    def _guard_transition(self, record: dict) -> None:
        """Refuse to re-open a closed incident by accident.

        This is the check that lives at the single choke point (the file
        write), so legacy code paths which assign ``record["status"]``
        directly cannot bypass it. Re-opening is only allowed through an
        explicit recurrence, which sets ``reopen``.
        """
        previous = self.get(record["id"])
        if previous is None:
            return
        old_state = normalize_state(previous.get("status", ""))
        new_state = normalize_state(record.get("status", ""))
        if old_state == new_state:
            return
        if old_state in TERMINAL_STATES and not record.get("reopen"):
            allowed = TRANSITIONS.get(old_state, frozenset())
            if new_state not in allowed:
                raise TransitionError(
                    f"illegal transition {old_state} -> {new_state} for "
                    f"incident {record['id']}: a closed incident may only be "
                    f"re-opened through a recurrence")
        self._append_timeline(record, from_state=old_state,
                              to_state=new_state,
                              by=record.get("updated_by", "") or "system",
                              note=record.get("transition_note", ""))

    def _sync_aliases(self, record: dict) -> None:
        """Keep the v1 and v2 names of the same fact in agreement."""
        state = normalize_state(record.get("status", "detected")) or "detected"
        record["status"] = status_for(state)
        record["state"] = STATE_LABELS.get(state, state.upper())
        incident_id = record.get("id")
        if incident_id:
            record["incident_id"] = incident_id
        if record.get("kind"):
            record["resource_kind"] = record["kind"]
        if record.get("name"):
            record["resource_name"] = record["name"]
        if record.get("kind") or record.get("name"):
            record["resource"] = resource_key(record.get("kind", ""),
                                              record.get("name", ""))
        if "fingerprint" not in record and record.get("kind"):
            record["fingerprint"] = fingerprint(
                record.get("kind", ""), record.get("namespace", ""),
                record.get("name", ""), record.get("cause", ""),
                record.get("cluster_id", ""))
        record.setdefault("timeline", [])
        record.setdefault("occurrence_count", 1)
        record.setdefault("healthy_checks", 0)
        record.setdefault("recurrence_ids", [])
        record.setdefault("severity_changes", [])
        record.setdefault("cluster_id", cluster_id())

    # -- state machine ----------------------------------------------------- #
    def _append_timeline(self, record: dict, *, from_state: str,
                         to_state: str, by: str = "", note: str = "") -> None:
        now = time.time()
        record.setdefault("timeline", []).append({
            "ts": round(now, 3),
            "iso": _iso(now),
            "from": state_label(from_state) if from_state else "",
            "to": state_label(to_state),
            "by": by or "system",
            "note": note or "",
        })

    def transition(self, incident_id: str, to_state: str, *, by: str = "",
                   note: str = "", **fields) -> dict | None:
        """Move an incident to ``to_state``, enforcing the adjacency table.

        An illegal move raises ``TransitionError``; it is never a silent
        write. The timeline records who moved it and why.
        """
        record = self.get(incident_id)
        if record is None:
            return None
        target = normalize_state(to_state)
        if target not in STATES:
            raise TransitionError(
                f"unknown incident state {to_state!r} (expected one of "
                f"{', '.join(STATES)})")
        current = normalize_state(record.get("status", "detected"))
        if current == target:
            return self.update(incident_id, **fields) if fields else record
        allowed = TRANSITIONS.get(current, frozenset())
        if target not in allowed:
            allowed_text = ", ".join(sorted(allowed)) or "none"
            raise TransitionError(
                f"illegal transition {current} -> {target} for incident "
                f"{incident_id} (allowed: {allowed_text})")
        record["status"] = status_for(target)
        record["state"] = STATE_LABELS[target]
        record.update(fields)
        self._append_timeline(record, from_state=current, to_state=target,
                              by=by, note=note)
        return self._write(record)

    def reopen(self, incident_id: str, to_state: str = "queued", *,
               by: str = "worker", note: str = "") -> dict | None:
        """Re-open a closed incident for a retry.

        A repair retry reuses the same incident (the spec requires it), so a
        failed incident has to be able to run again. This is the *only* way
        out of a terminal state, and it is recorded in the timeline.
        """
        record = self.get(incident_id)
        if record is None:
            return None
        target = normalize_state(to_state)
        if target not in STATES:
            raise TransitionError(
                f"unknown incident state {to_state!r} (expected one of "
                f"{', '.join(STATES)})")
        current = normalize_state(record.get("status", "detected"))
        record["status"] = status_for(target)
        record["state"] = STATE_LABELS[target]
        self._append_timeline(record, from_state=current, to_state=target,
                              by=by,
                              note=f"reopened: {note}" if note
                              else f"reopened for a retry from {current}")
        # `_write` (not `save`) is deliberate here: this method is the one
        # explicit way out of a terminal state, and the timeline records it.
        return self._write(record)

    def note_seen(self, incident_id: str, *, by: str = "scanner",
                  note: str = "") -> dict | None:
        """Record another sighting of an open incident.

        The problem is not a new incident: the occurrence count and
        ``last_seen`` move, the timeline notes it.
        """
        record = self.get(incident_id)
        if record is None:
            return None
        now = time.time()
        record["occurrence_count"] = int(record.get("occurrence_count") or 1) + 1
        record["last_seen"] = round(now, 3)
        record["last_seen_iso"] = _iso(now)
        self._append_timeline(record,
                              from_state=normalize_state(record.get("status", "")),
                              to_state=normalize_state(record.get("status", "")),
                              by=by, note=note or "problem seen again")
        return self._write(record)

    def note_healthy(self, incident_id: str, *, threshold: int = 0,
                     by: str = "reconciler", note: str = "") -> dict | None:
        """Count a healthy reconciliation; close the incident when it holds.

        A problem that is gone for a few consecutive passes is *resolved* —
        a single green pass is not enough, because a pod that is merely
        restarting looks green for a moment.
        """
        record = self.get(incident_id)
        if record is None:
            return None
        limit = max(1, int(threshold or HEALTHY_THRESHOLD))
        current = normalize_state(record.get("status", "detected"))
        record["healthy_checks"] = int(record.get("healthy_checks") or 0) + 1
        if record["healthy_checks"] >= limit:
            record["status"] = "resolved"
            record["state"] = STATE_LABELS["resolved"]
            record["resolution"] = {
                "reason": "healthy",
                "healthy_checks": record["healthy_checks"],
                "at": round(time.time(), 3),
                "note": note or "problem no longer observed",
            }
            self._append_timeline(record, from_state=current,
                                  to_state="resolved", by=by,
                                  note=note or "problem cleared")
            return self._write(record)
        return self._write(record)

    def link_recurrence(self, previous_id: str, new_id: str) -> dict | None:
        """Record that ``new_id`` is the same problem coming back."""
        previous = self.get(previous_id)
        if previous is None:
            return None
        ids = previous.setdefault("recurrence_ids", [])
        if new_id and new_id not in ids:
            ids.append(new_id)
        return self._write(previous)

    def set_severity(self, incident_id: str, severity: str, *,
                     by: str = "system", note: str = "") -> dict | None:
        """Change the severity and keep the change history."""
        record = self.get(incident_id)
        if record is None:
            return None
        old = record.get("severity")
        if old == severity:
            return record
        record["severity"] = severity
        record.setdefault("severity_changes", []).append({
            "ts": round(time.time(), 3),
            "iso": _iso(),
            "from": old,
            "to": severity,
            "by": by,
            "note": note,
        })
        self._append_timeline(record,
                              from_state=normalize_state(record.get("status", "")),
                              to_state=normalize_state(record.get("status", "")),
                              by=by, note=note or f"severity {old} -> {severity}")
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
