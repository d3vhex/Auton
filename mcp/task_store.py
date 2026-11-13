#!/usr/bin/env python3
"""Task model, states and the queue semantics used by every pod type.

One record describes one unit of work, whether it is a **repair** found by a
scanner or an **agent** job submitted from the UI/n8n. `/tasks` shows both; the
Redis keys carry the queue in their name.

Lifecycle

  repair:  queued → claimed → running → verifying → resolved
                                             ├→ failed → (retry) → queued
                                             └→ needs_human
  agent:   queued → running → done | error | cancelled

  `skipped` (resource changed / cooldown / budget) and `cancelled` are terminal.

Guarantees

* **At-least-once.** A worker crash leaves `status=claimed` with an expired
  lease; `recover()` puts it back on the queue (the playbook/agent step must be
  idempotent). Redis `XAUTOCLAIM` is used as well, but recovery never depends
  on the pending-entries list.
* **One writer per resource.** A repair task takes
  `auton:lock:{namespace}/{resource}` before it runs, so two workers can work on
  different Deployments in parallel but never on the same one.
* **Bounded retries.** `attempts` is incremented at claim time; after
  `AGENT_TASK_MAX_ATTEMPTS` the task becomes `needs_human` instead of looping.
* **Idempotent submit.** `idempotency_key` is claimed with `SET NX`, so two
  scanners that see the same symptom create one task, not two.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import time
import uuid

import ops_queue as queue_backend

# Repair states then agent states; the union is what `/tasks?status=` accepts.
REPAIR_STATUSES = ("queued", "claimed", "running", "verifying", "resolved",
                   "needs_human", "failed", "skipped", "cancelled")
AGENT_STATUSES = ("queued", "running", "done", "error", "cancelled")
STATUSES = tuple(dict.fromkeys(REPAIR_STATUSES + AGENT_STATUSES))
TERMINAL = frozenset({"resolved", "needs_human", "failed", "skipped",
                      "cancelled", "done", "error"})
ACTIVE = frozenset({"queued", "claimed", "running", "verifying"})
QUEUES = ("repair", "agent")
PRIORITIES = ("critical", "normal")

_ID = re.compile(r"^tsk-[0-9a-f]{8,32}$")


class TaskError(ValueError):
    """Invalid task field or state transition."""


def _now() -> float:
    return time.time()


def _iso(ts: float | None = None) -> str:
    moment = (_dt.datetime.fromtimestamp(ts, _dt.timezone.utc)
              if ts else _dt.datetime.now(_dt.timezone.utc))
    return moment.isoformat(timespec="seconds")


def new_id() -> str:
    return "tsk-" + uuid.uuid4().hex[:12]


def valid_id(task_id: str) -> bool:
    return bool(_ID.match(task_id or ""))


class Claim:
    """A stream entry this consumer owns for `lease` seconds."""

    __slots__ = ("task", "stream", "message_id")

    def __init__(self, task: dict, stream: str, message_id: str):
        self.task = task
        self.stream = stream
        self.message_id = message_id

    @property
    def id(self) -> str:
        return self.task["id"]

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Claim {self.task.get('id')} {self.task.get('status')}>"


class TaskStore:
    def __init__(self, backend=None, *, prefix: str = "",
                 lease: int | None = None, max_attempts: int | None = None,
                 backoff: list[int] | None = None,
                 max_queue: int | None = None) -> None:
        self.backend = backend if backend is not None else \
            queue_backend.create_backend()
        self.prefix = (prefix or os.environ.get("AGENT_QUEUE_PREFIX",
                                                "auton")).strip() or "auton"
        self.lease = int(lease if lease is not None
                         else os.environ.get("AGENT_TASK_LEASE", "300"))
        self.max_attempts = int(max_attempts if max_attempts is not None
                                else os.environ.get("AGENT_TASK_MAX_ATTEMPTS", "3"))
        raw = os.environ.get("AGENT_TASK_BACKOFF", "30,120,300")
        self.backoff = backoff or [int(part) for part in raw.split(",") if part.strip()]
        self.max_queue = int(max_queue if max_queue is not None
                             else os.environ.get("AGENT_QUEUE_MAX", "500"))
        self.consumer = os.environ.get("HOSTNAME", "local") or "local"

    # -- keys ------------------------------------------------------------- #
    def stream(self, queue: str, priority: str = "normal") -> str:
        return f"{self.prefix}:q:{queue}:{priority}"

    def task_key(self, task_id: str) -> str:
        return f"{self.prefix}:task:{task_id}"

    def index_key(self) -> str:
        return f"{self.prefix}:tasks"

    def delayed_key(self) -> str:
        return f"{self.prefix}:q:delayed"

    def dead_key(self) -> str:
        return f"{self.prefix}:q:dead"

    def claimed_key(self) -> str:
        return f"{self.prefix}:q:claimed"

    def active_key(self, queue: str) -> str:
        """Cheap in-flight counter (ZSET of task ids), one per queue."""
        return f"{self.prefix}:active:{queue}"

    def gpu_key(self) -> str:
        return f"{self.prefix}:gpu:slot"

    def idem_key(self, key: str) -> str:
        return f"{self.prefix}:idem:{key}"

    def lock_key(self, resource: str) -> str:
        return f"{self.prefix}:lock:{resource}"

    def cooldown_key(self, resource: str, cause: str) -> str:
        return f"{self.prefix}:cooldown:{resource}:{cause}"

    def budget_key(self, hour: str = "") -> str:
        return f"{self.prefix}:budget:{hour or time.strftime('%Y%m%d%H')}"

    def events_key(self, task_id: str) -> str:
        return f"{self.prefix}:task:{task_id}:events"

    # -- records ---------------------------------------------------------- #
    def _load(self, task_id: str) -> dict | None:
        raw = self.backend.hgetall(self.task_key(task_id)).get("data")
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return None

    def _save(self, record: dict) -> dict:
        record["updated_at"] = _iso()
        self.backend.hset(self.task_key(record["id"]),
                          {"data": json.dumps(record, sort_keys=True)})
        return record

    def _event(self, record: dict, old: str, new: str, *, by: str = "",
               note: str = "") -> None:
        timeline = record.setdefault("timeline", [])
        timeline.append({"at": _iso(), "from": old, "to": new,
                         "by": by or record.get("worker") or "",
                         "note": note})
        del timeline[:-40]

    # -- submit ----------------------------------------------------------- #
    def submit(self, queue: str, fields: dict, *, priority: str = "normal",
               delay: float = 0.0, idempotency_key: str = "",
               actor: str = "") -> dict:
        """Create a task and enqueue it (or return the duplicate)."""
        queue = (queue or "").strip().lower()
        if queue not in QUEUES:
            raise TaskError(f"unknown queue {queue!r}; use one of {QUEUES}")
        priority = (priority or "normal").strip().lower()
        if priority not in PRIORITIES:
            raise TaskError(f"priority must be one of {PRIORITIES}")
        if queue == "agent" and priority == "critical":
            priority = "normal"
        fields = dict(fields or {})
        severity = str(fields.get("severity") or "warning").lower()
        if severity not in ("critical", "warning", "info"):
            severity = "warning"
        fields["severity"] = severity

        task_id = new_id()
        idem = idempotency_key.strip() if idempotency_key else ""
        if idem:
            existing = self.backend.get(self.idem_key(idem))
            if existing:
                record = self._load(existing)
                if record:
                    record["duplicate"] = True
                    return record
            if not self.backend.set_nx(self.idem_key(idem), task_id,
                                       ttl_ms=7 * 24 * 3600 * 1000):
                existing = self.backend.get(self.idem_key(idem)) or ""
                record = self._load(existing) if existing else None
                if record:
                    record["duplicate"] = True
                    return record
                raise TaskError("duplicate task is still being created; retry")

        ts = _now()
        source = str(fields.pop("source", "") or actor or "api")
        record = {
            "id": task_id,
            "queue": queue,
            "priority": priority,
            "status": "queued",
            "created_ts": round(ts, 3),
            "created_at": _iso(ts),
            "updated_at": _iso(ts),
            "attempts": 0,
            "max_attempts": self.max_attempts,
            "worker": "",
            "lease_until": 0,
            "idempotency_key": idem,
            "source": source,
            "namespace": str(fields.pop("namespace", "") or ""),
            "kind": str(fields.pop("kind", "") or ""),
            "name": str(fields.pop("name", "") or ""),
            "cause": str(fields.pop("cause", "") or ""),
            "observation": str(fields.pop("observation", "") or ""),
            "evidence": str(fields.pop("evidence", "") or ""),
            "incident_id": str(fields.pop("incident_id", "") or ""),
            "task": str(fields.pop("task", "") or "")[:8000],
            "result": fields.pop("result", {}) or {},
            "timeline": [{"at": _iso(ts), "from": "", "to": "queued",
                          "by": source, "note": ""}],
            "audit_ids": [],
        }
        resource = str(fields.pop("resource", "") or "").strip()
        for key, value in fields.items():  # keep forward-compatible extras
            record.setdefault(key, value)
        record["resource"] = resource or (
            f"{record['kind'].lower()}/{record['name']}"
            if record["kind"] and record["name"] else "")
        record["severity"] = severity

        stream = self.stream(queue, priority)
        if self.backend.xlen(stream) >= self.max_queue:
            if idem:
                self.backend.delete(self.idem_key(idem))
            raise TaskError(f"queue {queue!r} is full ({self.max_queue} tasks); "
                            "retry later")

        self._save(record)
        self.backend.zadd(self.index_key(), ts, task_id)
        if delay and delay > 0:
            self.backend.zadd(self.delayed_key(), ts + delay, task_id)
        else:
            self.backend.xadd(stream, {"id": task_id})
        return record
    # -- claim ------------------------------------------------------------ #
    def recover(self) -> list[str]:
        """Requeue tasks whose lease expired (worker crash / node drain)."""
        now = _now()
        recovered: list[str] = []
        for task_id in self.backend.zrangebyscore(self.claimed_key(), 0, now,
                                                  limit=50):
            record = self._load(task_id)
            if not record:
                self.backend.zrem(self.claimed_key(), task_id)
                continue
            if record.get("status") not in ("claimed", "running", "verifying"):
                self.backend.zrem(self.claimed_key(), task_id)
                continue
            if float(record.get("lease_until") or 0) > now:
                continue
            if int(record.get("attempts") or 0) >= int(record.get("max_attempts")
                                                       or self.max_attempts):
                self._event(record, record["status"], "needs_human",
                            note="lease expired after the last attempt")
                record["status"] = "needs_human"
                record["worker"] = ""
                self._save(record)
                self._release(record)
                self.backend.zrem(self.claimed_key(), task_id)
                self.backend.zrem(self.active_key(record.get("queue", "")),
                                  task_id)
                continue
            self._event(record, record["status"], "queued",
                        note="lease expired; requeued")
            record.update({"status": "queued", "worker": "", "lease_until": 0})
            self._save(record)
            self._release(record)
            self.backend.zrem(self.claimed_key(), task_id)
            self.backend.zrem(self.active_key(record.get("queue", "")), task_id)
            self.backend.xadd(self.stream(record["queue"],
                                          record.get("priority", "normal")),
                              {"id": task_id})
            recovered.append(task_id)
        return recovered

    def promote_delayed(self) -> list[str]:
        """Move due retries from the delayed ZSET back onto their stream."""
        promoted: list[str] = []
        for task_id in self.backend.zrangebyscore(self.delayed_key(), 0, _now(),
                                                  limit=100):
            record = self._load(task_id)
            if not record or record.get("status") != "queued":
                self.backend.zrem(self.delayed_key(), task_id)
                continue
            self.backend.xadd(self.stream(record["queue"],
                                          record.get("priority", "normal")),
                              {"id": task_id})
            self.backend.zrem(self.delayed_key(), task_id)
            promoted.append(task_id)
        return promoted

    def claim(self, queue: str, consumer: str = "", *, limit: int = 1,
              block_ms: int = 0) -> list[Claim]:
        consumer = consumer or self.consumer
        self.recover()
        self.promote_delayed()
        group = f"{queue}-workers"
        streams = ([self.stream(queue, "critical"), self.stream(queue, "normal")]
                   if queue == "repair" else [self.stream(queue, "normal")])
        claims: list[Claim] = []

        # Redis: reclaim entries a dead consumer never acknowledged first.
        if getattr(self.backend, "name", "") == "redis":
            for stream in streams:
                for entry in self.backend.xautoclaim(
                        stream, group, consumer, self.lease * 1000, 10):
                    claims.extend(self._claim_entry(stream, entry, consumer))
                    if len(claims) >= limit:
                        return claims
        batches = self.backend.xreadgroup(group, consumer, streams,
                                          count=limit, block_ms=block_ms)
        for stream, entries in batches:
            for entry in entries:
                claims.extend(self._claim_entry(stream, entry, consumer))
                if len(claims) >= limit:
                    return claims
        return claims

    def _claim_entry(self, stream: str, entry, consumer: str) -> list[Claim]:
        message_id, fields = entry
        task_id = str((fields or {}).get("id") or "")
        record = self._load(task_id)
        if not record or record.get("status") != "queued":
            # Redelivery of something already taken (or a deleted task).
            self._ack(stream, message_id)
            return []

        resource = record.get("resource") or ""
        owner = ""
        if record["queue"] == "repair" and resource:
            owner = f"{consumer}:{task_id}"
            if not self.backend.set_nx(self.lock_key(resource), owner,
                                       ttl_ms=self.lease * 1000):
                # Another worker owns this resource; try again shortly instead
                # of blocking the single consumer.
                self.backend.zadd(self.delayed_key(), _now() + 15, task_id)
                self._ack(stream, message_id)
                return []

        now = _now()
        self._event(record, "queued", "claimed", by=consumer,
                    note=f"attempt {int(record.get('attempts') or 0) + 1}")
        record.update({
            "status": "claimed",
            "worker": consumer,
            "lock_owner": owner,
            "attempts": int(record.get("attempts") or 0) + 1,
            "lease_until": round(now + self.lease, 3),
            "claimed_at": _iso(now),
        })
        self._save(record)
        self.backend.zadd(self.claimed_key(), now + self.lease, task_id)
        self.backend.zadd(self.active_key(record["queue"]), now, task_id)
        self._ack(stream, message_id)
        return [Claim(record, stream, message_id)]

    def _group(self, stream: str) -> str:
        queue = stream.split(":q:")[1].split(":")[0]
        return f"{queue}-workers"

    def _ack(self, stream: str, message_id: str) -> None:
        """Acknowledge *and* delete: in Redis only XDEL shrinks XLEN."""
        group = self._group(stream)
        self.backend.xack(stream, group, message_id)
        self.backend.xdel(stream, message_id)

    def _release(self, record: dict) -> None:
        resource = record.get("resource") or ""
        if not resource or record.get("queue") != "repair":
            return
        key = self.lock_key(resource)
        current = self.backend.get(key)
        if not current:
            return
        # The lock value is "<consumer>:<task_id>"; the consumer/worker field is
        # cleared before finish(), so match on the task id.
        if str(current).endswith(str(record.get("id") or "")):
            self.backend.delete(key)

    # -- transitions ------------------------------------------------------ #
    def start(self, claim: Claim) -> dict:
        record = self._load(claim.id) or claim.task
        self._event(record, record.get("status", "claimed"), "running",
                    by=record.get("worker"))
        record["status"] = "running"
        record["started_at"] = record.get("started_at") or _iso()
        record["started_ts"] = record.get("started_ts") or round(_now(), 3)
        self._settle_lease(record)
        self._save(record)
        claim.task = record
        return record

    def verifying(self, claim: Claim) -> dict:
        record = self._load(claim.id) or claim.task
        self._event(record, "running", "verifying", by=record.get("worker"))
        record["status"] = "verifying"
        self._settle_lease(record)
        self._save(record)
        claim.task = record
        return record

    def _settle_lease(self, record: dict) -> None:
        """Keep the lease in step with the record at every transition."""
        now = _now()
        if record.get("status") in ACTIVE:
            record["lease_until"] = round(now + self.lease, 3)
            self.backend.zadd(self.claimed_key(), now + self.lease,
                              record["id"])
        else:
            record["lease_until"] = 0
            self.backend.zrem(self.claimed_key(), record["id"])
            self.backend.zrem(self.active_key(record.get("queue", "")),
                              record["id"])

    def update(self, task_id: str, **fields) -> dict:
        record = self._load(task_id)
        if record is None:
            raise TaskError(f"unknown task {task_id!r}")
        record.update(fields)
        return self._save(record)

    def finish(self, claim: Claim, *, status: str, result: dict | None = None,
               verification: str = "", note: str = "") -> dict:
        if status not in STATUSES:
            raise TaskError(f"unknown status {status!r}")
        record = self._load(claim.id) or claim.task
        old = record.get("status", "")
        if result is not None:
            record["result"] = result
        if verification:
            record["verification"] = verification
        self._event(record, old, status, by=record.get("worker"), note=note)
        record.update({
            "status": status,
            "worker": "",
            "finished_at": _iso(),
            "finished_ts": round(_now(), 3),
            "verified_at": _iso() if status == "resolved" else
                           record.get("verified_at", ""),
        })
        self._save(record)
        self.backend.zrem(self.claimed_key(), record["id"])
        self.backend.zrem(self.delayed_key(), record["id"])
        self.backend.zrem(self.active_key(record.get("queue", "")), record["id"])
        self._release(record)
        self._ack(claim.stream, claim.message_id)
        if status in ("needs_human", "failed", "error"):
            self.backend.xadd(self.dead_key(), {"id": record["id"],
                                                "status": status,
                                                "note": note[:200]})
        return record

    def defer(self, claim: Claim, *, delay: float = 5.0,
              note: str = "") -> str:
        """Put a claimed task back without counting the attempt.

        Used when a busy global resource (the GPU slot) prevented the task from
        starting: the work never ran, so burning an attempt would eventually
        turn a healthy job into `needs_human`.
        """
        record = self._load(claim.id) or claim.task
        record["attempts"] = max(0, int(record.get("attempts") or 0) - 1)
        self._event(record, record.get("status", ""), "queued",
                    note=note[:200])
        record.update({"status": "queued", "worker": "", "lease_until": 0})
        self._save(record)
        self.backend.zrem(self.claimed_key(), record["id"])
        self.backend.zrem(self.active_key(record.get("queue", "")), record["id"])
        self._release(record)
        self.backend.zadd(self.delayed_key(), _now() + max(0.0, delay),
                          record["id"])
        return "queued"

    def retry(self, claim: Claim, *, reason: str = "",
              result: dict | None = None) -> str:
        """Backoff and requeue, or give up after max attempts."""
        record = self._load(claim.id) or claim.task
        if result is not None:
            record["result"] = result
        attempts = int(record.get("attempts") or 0)
        self.backend.zrem(self.claimed_key(), record["id"])
        self.backend.zrem(self.active_key(record.get("queue", "")), record["id"])
        self._release(record)
        if attempts >= int(record.get("max_attempts") or self.max_attempts):
            self._event(record, record.get("status", ""), "needs_human",
                        note=f"gave up after {attempts} attempt(s): {reason}"[:300])
            record.update({"status": "needs_human", "worker": "",
                           "verification": reason[:500]})
            self._save(record)
            self.backend.xadd(self.dead_key(), {"id": record["id"],
                                                "status": "needs_human"})
            return "needs_human"
        delay = self.backoff[min(max(attempts - 1, 0), len(self.backoff) - 1)]
        self._event(record, record.get("status", ""), "queued",
                    note=f"retry in {delay}s: {reason}"[:300])
        record.update({"status": "queued", "worker": "", "lease_until": 0,
                       "last_error": reason[:500]})
        self._save(record)
        self.backend.zadd(self.delayed_key(), _now() + delay, record["id"])
        return "queued"

    def cancel(self, task_id: str, *, by: str = "api") -> dict:
        record = self._load(task_id)
        if record is None:
            raise TaskError(f"unknown task {task_id!r}")
        if record.get("status") in TERMINAL:
            return record
        old = record.get("status", "")
        if old in ("queued", "claimed"):
            self._event(record, old, "cancelled", by=by)
            record.update({"status": "cancelled", "worker": "",
                           "cancel_requested": True,
                           "finished_ts": round(_now(), 3)})
            self._save(record)
            self.backend.zrem(self.claimed_key(), task_id)
            self.backend.zrem(self.delayed_key(), task_id)
            self.backend.zrem(self.active_key(record.get("queue", "")), task_id)
            self._release(record)
        else:
            # Running: the executor checks this flag at the next step boundary.
            self._event(record, old, old, by=by, note="cancel requested")
            record["cancel_requested"] = True
            self._save(record)
        return record

    def cancel_requested(self, task_id: str) -> bool:
        record = self._load(task_id)
        return bool(record and record.get("cancel_requested"))

    # -- queries ---------------------------------------------------------- #
    def get(self, task_id: str) -> dict | None:
        return self._load(task_id)

    def active_for(self, resource: str, cause: str = "") -> dict | None:
        """The first queued/running task for a resource (scanner deduplication).

        The idempotency key only covers a short window; a repair that runs
        longer than that window must not be submitted a second time just
        because the scanner sees the same symptom again.
        """
        if not resource:
            return None
        for record in self.list(status=",".join(sorted(ACTIVE)), limit=300):
            if record.get("resource") != resource:
                continue
            if cause and record.get("cause") != cause:
                continue
            return record
        return None

    def list(self, *, queue: str = "", status: str = "", limit: int = 50,
             newest_first: bool = True) -> list[dict]:
        limit = max(1, min(int(limit), 500))
        statuses = {s.strip() for s in (status or "").split(",") if s.strip()}
        pool = self.backend.zrange(self.index_key(), -(limit * 5), -1)
        if newest_first:
            pool = list(reversed(pool))
        records: list[dict] = []
        for task_id in pool:
            record = self._load(task_id)
            if not record:
                continue
            if queue and record.get("queue") != queue:
                continue
            if statuses and record.get("status") not in statuses:
                continue
            records.append(record)
            if len(records) >= limit:
                break
        return records

    def stats(self) -> dict:
        counts: dict[str, int] = {}
        oldest_queued = None
        for record in self.list(limit=500):
            counts[record.get("status", "?")] = \
                counts.get(record.get("status", "?"), 0) + 1
            if record.get("status") == "queued" and oldest_queued is None:
                oldest_queued = record.get("created_ts")
        depth = {
            "repair_critical": self.backend.xlen(self.stream("repair", "critical")),
            "repair_normal": self.backend.xlen(self.stream("repair", "normal")),
            "agent": self.backend.xlen(self.stream("agent", "normal")),
            "delayed": self.backend.zcard(self.delayed_key()),
            "dead": self.backend.xlen(self.dead_key()),
            "claimed": self.backend.zcard(self.claimed_key()),
        }
        workers = self.workers()
        budget_used = int(self.backend.get(self.budget_key()) or 0)
        return {
            "statuses": counts,
            "queue_depth": depth,
            "active": {queue: self.active_count(queue) for queue in QUEUES},
            "oldest_queued": oldest_queued,
            "workers": workers,
            "budget_used": budget_used,
            "backend": getattr(self.backend, "name", "unknown"),
            "prefix": self.prefix,
        }

    def workers(self) -> list[dict]:
        out: list[dict] = []
        for key in self.backend.scan(f"{self.prefix}:hb:*"):
            parts = key.split(":hb:", 1)[1].split("/")
            if len(parts) != 2:
                continue
            value = self.backend.get(key) or ""
            try:
                seen = float(value)
            except ValueError:
                seen = 0.0
            out.append({"role": parts[0], "name": parts[1],
                        "heartbeat": seen,
                        "age": round(_now() - seen, 1) if seen else None,
                        "alive": bool(value) and (_now() - seen) < 120})
        return sorted(out, key=lambda w: (w["role"], w["name"]))

    # -- coordination ----------------------------------------------------- #
    def active_count(self, queue: str = "") -> int:
        """Cheap in-flight count (claimed + running + verifying)."""
        if queue:
            return int(self.backend.zcard(self.active_key(queue)))
        return sum(int(self.backend.zcard(self.active_key(q))) for q in QUEUES)

    def acquire_gpu(self, owner: str, ttl: int = 1200) -> bool:
        """Global single-GPU slot (SET NX with a TTL for a crashed owner)."""
        return self.backend.set_nx(self.gpu_key(), owner, ttl_ms=ttl * 1000)

    def release_gpu(self, owner: str = "") -> None:
        current = self.backend.get(self.gpu_key())
        if not current:
            return
        if not owner or current == owner:
            self.backend.delete(self.gpu_key())

    def heartbeat(self, role: str, name: str = "", ttl: int = 90) -> None:
        name = name or self.consumer
        self.backend.set(f"{self.prefix}:hb:{role}/{name}", str(_now()))
        self.backend.expire(f"{self.prefix}:hb:{role}/{name}", ttl * 1000)

    def in_cooldown(self, resource: str, cause: str) -> bool:
        return bool(self.backend.get(self.cooldown_key(resource, cause)))

    def set_cooldown(self, resource: str, cause: str,
                     seconds: float = 1800) -> None:
        key = self.cooldown_key(resource, cause)
        self.backend.set(key, _iso())
        self.backend.expire(key, int(seconds * 1000))

    def cooldown_remaining(self, resource: str, cause: str) -> int:
        ttl = self.backend.ttl(self.cooldown_key(resource, cause))
        return max(0, ttl // 1000) if ttl and ttl > 0 else 0

    def take_budget(self, limit: int, ttl: int = 3600) -> tuple[bool, int]:
        """Atomic hourly counter; (allowed, used_after_increment)."""
        if limit <= 0:
            return False, 0
        key = self.budget_key()
        used = self.backend.incr(key)
        if used == 1:
            self.backend.expire(key, ttl * 1000)
        return used <= limit, used

    def budget_used(self) -> int:
        return int(self.backend.get(self.budget_key()) or 0)

    # -- event stream (agent logs / SSE) ---------------------------------- #
    def append_log(self, task_id: str, line: str) -> None:
        self.backend.xadd(self.events_key(task_id), {"log": str(line)[:4000]},
                          maxlen=500)

    def logs(self, task_id: str, limit: int = 500) -> list[str]:
        return [fields.get("log", "")
                for _id, fields in self.backend.xrange(self.events_key(task_id),
                                                       limit)]
