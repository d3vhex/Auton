#!/usr/bin/env python3
"""HTTP front-end for the coding agent, meant to run as a pod.

Endpoints:
  GET  /                        -> single-page UI (open; it holds no data)
  GET  /ui                      -> same UI
  GET  /health                  -> {"status":"ok", ...} plus queue depth (open)
  POST /run  {"task": ""}       -> synchronous result (kept for n8n back-compat)
  POST /run?async=1             -> 202 {"job_id": ...} and poll /jobs/{id}
  GET  /jobs                    -> recent jobs
  GET  /jobs/{id}               -> status, log tail, result
  GET  /jobs/{id}/events        -> SSE stream of progress
  POST /jobs/{id}/cancel        -> cooperative cancellation
  GET  /audit?limit=50          -> recent mutation audit records (JSONL-backed)
  GET  /incidents?limit=&status=-> incidents recorded by the ops controller
  GET  /incidents/{id}          -> one incident (state, actions, verification)
  POST /incidents/ingest        -> the ops controller records an incident
  GET  /tasks?queue=&status=    -> every queued/running/finished task
  GET  /tasks/{id}              -> one task with its log tail
  GET  /tasks/stats             -> queue depth, active counts, workers, budget
  POST /tasks                   -> enqueue a manual repair (or an agent job)
  POST /tasks/{id}/cancel       -> cancel a queued task
  GET  /provider                -> active model provider (key masked)
  POST /provider/test           -> one tiny completion against the provider

`/` and `/ui` serve the bundled page; `/health` stays open (probes);
everything else requires `Authorization: Bearer $AGENT_API_TOKEN`.
The service refuses to start without a token unless AGENT_AUTH_DISABLED=1.

With `AGENT_QUEUE=redis` this process is only a producer: `/run` enqueues an
`agent` task and `deployment/auton-ops` runs it. `AGENT_QUEUE=memory` (default
for local runs) keeps the in-process worker thread.

One worker thread owns the GPU (memory mode) / one executor pod holds the GPU
slot (redis mode), so two tasks never fight over VRAM. No extra deps — stdlib
http.server only. Listen addr/port via AGENT_BIND (default 0.0.0.0) /
AGENT_PORT (default 8080).

  python agent_service.py
"""
from __future__ import annotations

import asyncio
import hmac
import json
import os
import queue
import sys
import threading
import time
import traceback
import uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from agent_core import MODEL, OLLAMA_HOST, run_agent
import approvals
import audit
import incidents
import llm
import ops_queue
import policy
import provider_admin
import task_store

UI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui")

BIND = os.environ.get("AGENT_BIND", "0.0.0.0")
PORT = int(os.environ.get("AGENT_PORT", "8080"))
MAX_QUEUE = max(1, int(os.environ.get("AGENT_MAX_QUEUE", "8")))
JOB_TIMEOUT = max(10, int(os.environ.get("AGENT_JOB_TIMEOUT", "900")))
MAX_JOBS = max(10, int(os.environ.get("AGENT_MAX_JOBS", "100")))
MAX_BODY = 2 * 1024 * 1024  # 2 MiB is plenty for a task string
LOG_TAIL = 300

# Where the LLM work runs. `memory` keeps the old single-process queue (local
# development); `redis` makes the API a *producer* and hands execution to the
# `auton-ops` pod. There is no silent fallback: a redis deployment with an
# unreachable Redis refuses to start.
QUEUE_KIND = (os.environ.get("AGENT_QUEUE", "memory").strip().lower()
              or "memory")
AGENT_STORE: "task_store.TaskStore | None" = None
AGENT_STORE_ERROR = ""

# Bearer auth. /health stays open (probes); everything else needs the token.
# An empty token is a configuration error, so the handler fails closed and
# main() refuses to start — never "no token means open".
API_TOKEN = os.environ.get("AGENT_API_TOKEN", "").strip()
AUTH_DISABLED = os.environ.get("AGENT_AUTH_DISABLED", "").strip().lower() in (
    "1", "true", "yes", "on"
)

# Incidents live next to the audit log on the workspace PVC. A broken volume
# must not stop the API from serving /run: the incident endpoints answer 503
# instead (same fail-closed-in-one-place rule as /audit).
try:
    INCIDENT_STORE: incidents.IncidentStore | None = incidents.IncidentStore()
    INCIDENT_STORE_ERROR = ""
except incidents.IncidentError as _exc:
    INCIDENT_STORE = None
    INCIDENT_STORE_ERROR = str(_exc)

# The approval requests live in the same directory tree, so the worker that
# parks a plan and the API that decides it see the same files.
try:
    APPROVAL_STORE: approvals.ApprovalStore | None = approvals.ApprovalStore()
    APPROVAL_STORE_ERROR = ""
except approvals.ApprovalError as _exc:
    APPROVAL_STORE = None
    APPROVAL_STORE_ERROR = str(_exc)


class QueueFull(Exception):
    pass


class Job:
    def __init__(self, task: str):
        self.id = uuid.uuid4().hex[:12]
        self.task = task
        self.status = "queued"          # queued|running|done|error|cancelled
        self.logs: deque[str] = deque(maxlen=LOG_TAIL)
        self.created = time.time()
        self.started: float | None = None
        self.finished: float | None = None
        self.result: dict | None = None
        self.error: str | None = None
        self.cancelled = threading.Event()
        self.done = threading.Event()

    # -- helpers ---------------------------------------------------------- #
    def log(self, *parts) -> None:
        line = " ".join(str(p) for p in parts)
        self.logs.append(line)
        print(f"[job {self.id}] {line}", flush=True)

    def should_stop(self) -> bool:
        if self.cancelled.is_set():
            return True
        return self.started is not None and (time.time() - self.started) > JOB_TIMEOUT

    def summary(self) -> dict:
        return {
            "job_id": self.id,
            "status": self.status,
            "task": self.task,
            "created": round(self.created, 3),
            "started": round(self.started, 3) if self.started else None,
            "finished": round(self.finished, 3) if self.finished else None,
            "elapsed": (round((self.finished or time.time()) - self.started, 1)
                        if self.started else 0),
            "stop_reason": (self.result or {}).get("stop_reason"),
            "error": self.error,
            # The UI polls this; without it a finished job looked "running"
            # forever (the events stream knew, the JSON payload did not).
            "done": self.done.is_set(),
        }

    def payload(self) -> dict:
        data = self.summary()
        data["result"] = self.result
        data["logs"] = list(self.logs)[-60:]
        return data


JOBS: dict[str, Job] = {}
JOBS_LOCK = threading.Lock()
QUEUE: "queue.Queue[str]" = queue.Queue()
_WORKER: threading.Thread | None = None
_WORKER_LOCK = threading.Lock()


# --------------------------------------------------------------------------- #
# Redis-backed agent jobs (API producer, `auton-ops` executor)
# --------------------------------------------------------------------------- #

def redis_mode() -> bool:
    return QUEUE_KIND == "redis"


def _agent_store() -> "task_store.TaskStore":
    """Create (once) the Redis-backed task store; raises QueueError."""
    global AGENT_STORE, AGENT_STORE_ERROR
    if AGENT_STORE is not None:
        return AGENT_STORE
    try:
        backend = ops_queue.create_backend(QUEUE_KIND)
        AGENT_STORE = task_store.TaskStore(backend, max_queue=MAX_QUEUE)
    except ops_queue.QueueError as exc:
        AGENT_STORE_ERROR = str(exc)
        raise
    AGENT_STORE_ERROR = ""
    return AGENT_STORE


def _agent_store_quiet() -> "task_store.TaskStore | None":
    try:
        return _agent_store()
    except ops_queue.QueueError:
        return None


class RemoteJob:
    """Read-through view of one `agent` task in the shared task store.

    The HTTP layer was written against the in-process `Job`; this class gives a
    Redis task the same surface (`summary`, `payload`, `logs`, `done`, `wait`)
    so `/jobs` keeps working when the executor is a different pod.
    """

    def __init__(self, store, task_id: str, record: dict | None = None):
        self.store = store
        self.id = task_id
        self.done = threading.Event()
        self._record: dict = {}
        self._refresh(record)

    def _refresh(self, record: dict | None = None) -> dict:
        record = record or self.store.get(self.id) or {}
        self._record = record
        self.task = str(record.get("task") or "")
        self.status = str(record.get("status") or "queued")
        self.result = record.get("result") or None
        self.error = (self.result or {}).get("error") if self.status == "error" \
            else None
        self.created = float(record.get("created_ts") or 0)
        self.started = float(record.get("started_ts") or 0) or None
        self.finished = float(record.get("finished_ts") or 0) or None
        if self.status in task_store.TERMINAL:
            self.done.set()
        else:
            self.done.clear()
        return record

    @property
    def logs(self) -> list[str]:
        try:
            return self.store.logs(self.id)
        except ops_queue.QueueError:
            return []

    def summary(self) -> dict:
        self._refresh()
        if self.started:
            end = self.finished or time.time()
            elapsed = round(end - self.started, 1)
        else:
            elapsed = 0
        return {
            "job_id": self.id,
            "status": self.status,
            "task": self.task,
            "created": round(self.created, 3),
            "started": self.started,
            "finished": self.finished,
            "elapsed": elapsed,
            "stop_reason": (self.result or {}).get("stop_reason"),
            "error": self.error,
            "done": self.done.is_set(),
        }

    def payload(self) -> dict:
        data = self.summary()
        data["result"] = self.result
        data["logs"] = self.logs[-60:]
        return data

    def cancel(self) -> "RemoteJob":
        self.store.cancel(self.id, by="api")
        self._refresh()
        return self

    def wait(self, timeout: float) -> bool:
        deadline = time.time() + max(0.0, timeout)
        while time.time() < deadline:
            self._refresh()
            if self.done.is_set():
                return True
            time.sleep(0.5)
        return self.done.is_set()


# --------------------------------------------------------------------------- #
# Job lifecycle
# --------------------------------------------------------------------------- #

def _prune_locked() -> None:
    """Drop the oldest finished jobs so memory stays bounded."""
    if len(JOBS) <= MAX_JOBS:
        return
    finished = sorted(
        (j for j in JOBS.values() if j.done.is_set()),
        key=lambda j: j.created,
    )
    for job in finished:
        if len(JOBS) <= MAX_JOBS:
            break
        JOBS.pop(job.id, None)


def submit(task: str):
    """Queue a task. Raises QueueFull instead of piling up unbounded work."""
    if redis_mode():
        store = _agent_store_quiet()
        if store is None:
            raise QueueFull(f"queue unavailable: {AGENT_STORE_ERROR}")
        try:
            record = store.submit("agent", {"task": task, "source": "api"},
                                  actor="api")
        except task_store.TaskError as exc:
            raise QueueFull(str(exc)) from exc
        return RemoteJob(store, record["id"], record)

    job = Job(task)
    with JOBS_LOCK:
        if QUEUE.qsize() >= MAX_QUEUE:
            raise QueueFull(f"queue is full ({MAX_QUEUE} jobs)")
        JOBS[job.id] = job
        _prune_locked()
        QUEUE.put(job.id)
    return job


def get_job(job_id: str):
    if redis_mode():
        store = _agent_store_quiet()
        if store is None or not task_store.valid_id(job_id):
            return None
        record = store.get(job_id)
        if not record or record.get("queue") != "agent":
            return None
        return RemoteJob(store, job_id, record)
    with JOBS_LOCK:
        return JOBS.get(job_id)


def list_jobs(limit: int = 50) -> list[dict]:
    if redis_mode():
        store = _agent_store_quiet()
        if store is None:
            return []
        return [RemoteJob(store, record["id"], record).summary()
                for record in store.list(queue="agent", limit=limit)]
    with JOBS_LOCK:
        jobs = sorted(JOBS.values(), key=lambda j: j.created, reverse=True)
    return [job.summary() for job in jobs[:limit]]


def cancel_job(job_id: str):
    if redis_mode():
        store = _agent_store_quiet()
        if store is None or not task_store.valid_id(job_id):
            return None
        try:
            record = store.cancel(job_id, by="api")
        except task_store.TaskError:
            return None
        return RemoteJob(store, job_id, record)

    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        return None
    job.cancelled.set()
    if job.status == "queued":
        job.status = "cancelled"
        job.finished = time.time()
        job.done.set()
    return job


def queue_depth() -> int:
    if redis_mode():
        store = _agent_store_quiet()
        if store is None:
            return 0
        try:
            return int(store.backend.xlen(store.stream("agent", "normal")))
        except Exception:
            return 0
    return QUEUE.qsize()


def jobs_running() -> int:
    if redis_mode():
        store = _agent_store_quiet()
        if store is None:
            return 0
        try:
            return store.active_count("agent")
        except Exception:
            return 0
    with JOBS_LOCK:
        return sum(1 for j in JOBS.values() if j.status == "running")


def jobs_known() -> int:
    if redis_mode():
        store = _agent_store_quiet()
        if store is None:
            return 0
        try:
            return int(store.backend.zcard(store.index_key()))
        except Exception:
            return 0
    return len(JOBS)


# --------------------------------------------------------------------------- #
# Tasks (both queues: scanner repairs and agent jobs)
#
# `/jobs` is the n8n-facing view of agent runs; `/tasks` is the operator view of
# *everything* the queue holds. It reads the same `TaskStore` the scanner,
# workers and executor use, so the UI cannot show a status the workers do not
# see. Submitting a repair from here is a queue write, not a mutation: the
# worker validates, locks, budgets and audit-gates it exactly like a finding.
# --------------------------------------------------------------------------- #

def tasks_store() -> task_store.TaskStore:
    """The store behind `/tasks` (same backend as the agent queue)."""
    return _agent_store()


def _manual_causes() -> dict[str, str]:
    """cause -> severity for the UI's manual repair form."""
    import ops_controller as ops  # lazy: the API does not own the repair loop

    causes = {cause: severity for cause, severity in ops.REASONS.values()}
    causes.update({cause: "warning" for cause in ops.REPORT_ONLY})
    return causes


def _worker_loop() -> None:
    while True:
        job_id = QUEUE.get()
        try:
            with JOBS_LOCK:
                job = JOBS.get(job_id)
            if job is None:
                continue
            if job.cancelled.is_set():
                job.status = "cancelled"
                job.finished = time.time()
                job.done.set()
                continue

            job.status = "running"
            job.started = time.time()
            job.log(f"[worker] start task={job.task[:120]!r}")
            try:
                result = asyncio.run(
                    run_agent(job.task, log=job.log, should_stop=job.should_stop)
                )
                job.result = result
                job.status = ("cancelled"
                              if result.get("stop_reason") == "cancelled" else "done")
            except Exception as exc:  # keep the worker alive for the next job
                traceback.print_exc()
                job.error = f"{type(exc).__name__}: {exc}"
                job.status = "error"
            finally:
                job.finished = time.time()
                job.done.set()
                job.log(f"[worker] {job.status} in {job.summary()['elapsed']}s")
                with JOBS_LOCK:
                    _prune_locked()
        finally:
            QUEUE.task_done()


def start_worker() -> None:
    """Start the single GPU worker (idempotent).

    In Redis mode the API is a producer only: the `auton-ops` pod owns the GPU
    and the agent loop, so this process must never start a second worker.
    """
    if redis_mode():
        return
    global _WORKER
    with _WORKER_LOCK:
        if _WORKER is not None and _WORKER.is_alive():
            return
        _WORKER = threading.Thread(target=_worker_loop, name="agent-worker", daemon=True)
        _WORKER.start()


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

class Handler(BaseHTTPRequestHandler):
    server_version = "auton/1"

    # -- plumbing --------------------------------------------------------- #
    def _send(self, code: int, obj: dict,
              headers: dict | None = None) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_html(self, code: int, text: str) -> None:
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; style-src 'unsafe-inline'; "
                         "script-src 'unsafe-inline'; connect-src 'self'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _serve_ui(self) -> None:
        """Serve the bundled single-page UI.

        The page itself is open (it contains no data), while every fetch it
        makes uses the bearer token the user pastes in. If the file is missing
        (e.g. a slim custom image) answer with a one-line hint instead of a
        stack trace.
        """
        try:
            with open(os.path.join(UI_DIR, "index.html"), encoding="utf-8") as fh:
                self._send_html(200, fh.read())
        except OSError:
            self._send_html(200, "<h1>Auton</h1><p>UI bundle is missing; the "
                                 "JSON API is still available at "
                                 "<code>/health</code>.</p>")

    def _check_auth(self) -> bool:
        """True if the caller may proceed; otherwise send 401/503 and return False."""
        if AUTH_DISABLED:
            return True
        if not API_TOKEN:
            self._send(503, {"error": "agent has no AGENT_API_TOKEN configured",
                             "hint": "set the auton-api-token Secret or "
                                     "AGENT_AUTH_DISABLED=1 for local development"})
            return False
        scheme, _, token = self.headers.get("Authorization", "").partition(" ")
        if scheme.lower() == "bearer" and hmac.compare_digest(
            token.strip(), API_TOKEN
        ):
            return True
        self._send(401, {"error": "unauthorized",
                         "hint": "send 'Authorization: Bearer <token>'",
                         "token_url": "/auth/check"},
                   headers={"WWW-Authenticate": 'Bearer realm="auton"'})
        return False

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length > MAX_BODY:
            raise ValueError(f"request body too large (>{MAX_BODY} bytes)")
        raw = self.rfile.read(length) if length else b"{}"
        data = json.loads(raw or b"{}")
        if not isinstance(data, dict):
            raise ValueError("body must be a JSON object")
        return data

    def _route(self) -> tuple[str, dict[str, list[str]]]:
        parts = urlsplit(self.path)
        return parts.path.rstrip("/") or "/", parse_qs(parts.query)

    def log_message(self, fmt, *args):  # quieter default logging
        print("[http]", fmt % args, flush=True)

    # -- incidents -------------------------------------------------------- #
    def _incident_store(self) -> incidents.IncidentStore | None:
        store = globals().get("INCIDENT_STORE")
        if store is None:
            self._send(503, {
                "error": f"incident store unavailable: {INCIDENT_STORE_ERROR}",
                "hint": "check the workspace volume / AGENT_INCIDENTS_DIR",
            })
        return store

    def _get_incidents(self, path: str) -> None:
        store = self._incident_store()
        if store is None:
            return
        try:
            if path == "/incidents":
                _, query = self._route()
                try:
                    limit = int(query.get("limit", ["50"])[0])
                except (TypeError, ValueError):
                    limit = 50
                status = query.get("status", [""])[0]
                severity = query.get("severity", [""])[0]
                cause = query.get("cause", [""])[0]
                namespace = query.get("namespace", [""])[0]
                resource = query.get("resource", [""])[0]
                fingerprint = query.get("fingerprint", [""])[0]
                records = store.list(limit=limit, status=status,
                                     severity=severity, cause=cause,
                                     namespace=namespace, resource=resource,
                                     fingerprint=fingerprint)
                self._send(200, {"count": len(records), "incidents": records})
                return
            segments = path.strip("/").split("/")
            if len(segments) != 2:
                self._send(404, {"error": "not found", "path": path})
                return
            record = store.get(segments[1])
        except incidents.IncidentError as exc:
            self._send(503, {"error": f"incident store unavailable: {exc}"})
            return
        if record is None:
            self._send(404, {"error": "unknown incident",
                             "incident_id": segments[1]})
            return
        self._send(200, record)

    # -- policy and approvals ---------------------------------------------- #
    def _approval_store(self):
        store = globals().get("APPROVAL_STORE")
        if store is None:
            self._send(503, {
                "error": f"approval store unavailable: {APPROVAL_STORE_ERROR}",
                "hint": "check the workspace volume / AGENT_APPROVALS_DIR",
            })
        return store

    def _get_policy(self) -> None:
        """The effective policy, and the modes/tiers it understands."""
        self._send(200, policy.describe())

    def _get_approvals(self, path: str) -> None:
        store = self._approval_store()
        if store is None:
            return
        try:
            if path == "/approvals":
                _, query = self._route()
                try:
                    limit = int(query.get("limit", ["50"])[0])
                except (TypeError, ValueError):
                    limit = 50
                records = store.list(
                    status=query.get("status", [""])[0].strip(),
                    incident_id=query.get("incident_id", [""])[0].strip(),
                    limit=limit)
                self._send(200, {"count": len(records),
                                 "approvals": records})
                return
            segments = path.strip("/").split("/")
            if len(segments) != 2:
                self._send(404, {"error": "not found", "path": path})
                return
            record = store.get(segments[1])
        except approvals.ApprovalError as exc:
            self._send(503, {"error": f"approval store unavailable: {exc}"})
            return
        if record is None:
            self._send(404, {"error": "unknown approval",
                             "approval_id": segments[1]})
            return
        self._send(200, record)

    def _post_approval(self, path: str) -> None:
        """Approve or reject a pending repair plan."""
        store = self._approval_store()
        if store is None:
            return
        segments = path.strip("/").split("/")
        if len(segments) != 3 or segments[2] not in ("approve", "reject"):
            self._send(404, {"error": "not found", "path": path})
            return
        approve = segments[2] == "approve"
        try:
            body = self._read_json()
        except ValueError as exc:
            self._send(400, {"error": str(exc)})
            return
        by = str(body.get("by") or "").strip() or "api"
        comment = str(body.get("comment") or "").strip()
        try:
            record = store.decide(segments[1], approve=approve, by=by,
                                  comment=comment)
        except approvals.ApprovalConflict as exc:
            self._send(409, {"error": str(exc)})
            return
        except approvals.ApprovalError as exc:
            self._send(404 if "unknown approval" in str(exc) else 503,
                       {"error": str(exc)})
            return

        task = None
        if approve:
            task = self._enqueue_approved(record)
        else:
            self._close_approved_incident(record, by=by, comment=comment)
        self._send(200, {"ok": True, "approval": record, "task": task})

    def _enqueue_approved(self, record: dict) -> dict | None:
        """Turn an approved plan back into a repair task."""
        # `ops_controller` is imported lazily: at module level it would build a
        # second IncidentStore (the API already has one).
        import ops_controller

        plan = record.get("plan") or {}
        fields = {
            "namespace": str(plan.get("namespace") or ""),
            "kind": str(plan.get("kind") or "Pod"),
            "name": str(plan.get("name") or ""),
            "cause": str(plan.get("cause") or ""),
            "severity": "critical",
            "observation": f"approved repair plan {plan.get('plan_id', '')}",
            "container": str((plan.get("params") or {}).get("container") or ""),
            "resource": str(plan.get("resource") or ""),
            "source": "approval",
            # The same incident continues; the approval is the authorisation.
            "incident_id": str(record.get("incident_id") or ""),
            "approval_id": str(record.get("id") or ""),
            # A human asked for this, so the cooldown does not block it.
            "force": True,
        }
        try:
            task = ops_controller.submit_manual(
                tasks_store(), fields, actor=str(record.get("decided_by")
                                                 or "api"),
                source="approval")
        except (ValueError, ops_queue.QueueError) as exc:
            print(f"[http] cannot enqueue approved plan: {exc}", flush=True)
            return None
        return task

    def _close_approved_incident(self, record: dict, *, by: str,
                                 comment: str) -> None:
        """A rejected plan sends its incident back to a human."""
        import ops_controller

        incident_id = str(record.get("incident_id") or "")
        store = globals().get("INCIDENT_STORE")
        if not incident_id or store is None:
            return
        try:
            incident = store.get(incident_id)
            if incident is None:
                return
            incident.update({"status": "needs_human", "needs_human": True,
                             "verification": "the repair plan was rejected",
                             "actions": (incident.get("actions") or []) + [
                                 {"action": "rejected", "ok": False,
                                  "detail": comment or
                                            f"rejected by {by}"}]})
            incident["state"] = incidents.STATE_LABELS["needs_human"]
            incident["report"] = ops_controller.build_report(incident)
            store.save(incident)
        except incidents.IncidentError as exc:
            print(f"[http] cannot close rejected incident: {exc}", flush=True)

    # -- tasks ------------------------------------------------------------ #
    def _store_unavailable(self, exc: Exception) -> None:
        self._send(503, {
            "error": f"task store unavailable: {exc}",
            "hint": "AGENT_QUEUE=redis needs Redis at AGENT_REDIS_URL; "
                    "there is no memory fallback",
        })

    def _get_tasks(self, path: str) -> None:
        try:
            store = tasks_store()
            if path == "/tasks/stats":
                self._send(200, store.stats())
                return
            if path == "/tasks":
                _, query = self._route()
                try:
                    limit = int(query.get("limit", ["50"])[0])
                except (TypeError, ValueError):
                    limit = 50
                records = store.list(
                    queue=query.get("queue", [""])[0].strip().lower(),
                    status=query.get("status", [""])[0].strip().lower(),
                    limit=limit)
                self._send(200, {
                    "count": len(records),
                    "tasks": records,
                    "stats": store.stats(),
                    "causes": _manual_causes(),
                })
                return
        except ops_queue.QueueError as exc:
            self._store_unavailable(exc)
            return

        segments = path.strip("/").split("/")
        if len(segments) != 2 or not task_store.valid_id(segments[1]):
            self._send(404, {"error": "unknown task", "path": path})
            return
        task_id = segments[1]
        try:
            record = store.get(task_id)
            logs = store.logs(task_id, limit=200) if record else []
        except ops_queue.QueueError as exc:
            self._store_unavailable(exc)
            return
        if record is None:
            self._send(404, {"error": "unknown task", "task_id": task_id})
            return
        payload = dict(record)
        payload["logs"] = logs
        self._send(200, payload)

    def _post_task(self) -> None:
        try:
            body = self._read_json()
        except ValueError as exc:
            self._send(400, {"error": str(exc)})
            return
        queue_name = (str(body.get("queue") or "repair").strip().lower()
                      or "repair")

        if queue_name == "agent":
            text = str(body.get("task") or "").strip()
            if not text:
                self._send(400, {"error": "missing 'task'",
                                 "hint": 'send {"queue": "agent", '
                                         '"task": "what to do"}'})
                return
            try:
                job = submit(text)
            except QueueFull as exc:
                self._send(429, {"error": str(exc), "hint": "retry later"})
                return
            self._send(202, {"task_id": job.id, "queue": "agent",
                             "status": job.status,
                             "poll": f"/tasks/{job.id}"})
            return

        if queue_name != "repair":
            self._send(400, {"error": f"unknown queue {queue_name!r}",
                             "hint": "queue is 'repair' or 'agent'"})
            return
        try:
            store = tasks_store()
        except ops_queue.QueueError as exc:
            self._store_unavailable(exc)
            return
        try:
            import ops_controller as ops  # lazy: the API does not own repairs

            record = ops.submit_manual(store, body, actor="api")
        except task_store.TaskError as exc:
            status = 429 if "full" in str(exc) else 400
            self._send(status, {"error": str(exc)})
            return
        except ValueError as exc:
            self._send(400, {"error": str(exc),
                             "causes": sorted(_manual_causes()),
                             "kinds": ["Pod", "Deployment", "StatefulSet",
                                       "PersistentVolumeClaim"]})
            return
        except ops_queue.QueueError as exc:
            self._store_unavailable(exc)
            return

        # An event, not a mutation: the playbook's kubectl call is what must be
        # write-ahead audited. A broken log must not swallow the request, so
        # this is best effort and the id is attached to the task when written.
        audit_id = ""
        try:
            audit_id = audit.record(
                "task_submit", actor="api",
                args={"task_id": record["id"],
                      "namespace": record.get("namespace", ""),
                      "kind": record.get("kind", ""),
                      "name": record.get("name", ""),
                      "cause": record.get("cause", ""),
                      "priority": record.get("priority", "")},
                result=f"queued {record['id']} for {record.get('resource', '')}")
        except audit.AuditError as exc:
            print(f"[http] task_submit audit event failed: {exc}", flush=True)
        if audit_id:
            try:
                record = store.update(record["id"], audit_ids=[audit_id])
            except ops_queue.QueueError:
                pass
        self._send(202, {"task_id": record["id"], "queue": "repair",
                         "status": record["status"],
                         "resource": record.get("resource", ""),
                         "audit_ids": record.get("audit_ids") or [],
                         "poll": f"/tasks/{record['id']}"})

    def _cancel_task(self, task_id: str) -> None:
        if not task_store.valid_id(task_id):
            self._send(404, {"error": "unknown task", "task_id": task_id})
            return
        try:
            store = tasks_store()
            record = store.cancel(task_id, by="api")
        except task_store.TaskError:
            record = None
        except ops_queue.QueueError as exc:
            self._store_unavailable(exc)
            return
        if record is None and not redis_mode():
            job = cancel_job(task_id)  # in-process agent job (local mode)
            if job is not None:
                self._send(200, {"task_id": job.id, "status": job.status,
                                 "cancelled": True})
                return
        if record is None:
            self._send(404, {"error": "unknown task", "task_id": task_id})
            return
        self._send(200, {"task_id": record["id"], "status": record["status"],
                         "cancelled": True})

    # -- GET -------------------------------------------------------------- #
    def do_GET(self):  # noqa: N802
        path, _ = self._route()
        if path in ("/", "/ui"):
            self._serve_ui()
            return
        if path == "/health":
            info = llm.provider_info()
            self._send(200, {
                "status": "ok",
                "provider": info.get("provider", "ollama"),
                "model": info.get("model", MODEL),
                "local_model_enabled": info.get("local_model_enabled", True),
                "ollama": OLLAMA_HOST,
                "queue": QUEUE_KIND,
                "queue_depth": queue_depth(),
                "jobs_running": jobs_running(),
                "jobs_known": jobs_known(),
                "auth": "off" if AUTH_DISABLED else "bearer",
            })
            return

        if not self._check_auth():
            return

        if path == "/auth/check":
            # Cheap token probe for the UI: 200 means "this token is accepted",
            # so the login modal can validate before saving it.
            self._send(200, {"ok": True,
                             "auth": "off" if AUTH_DISABLED else "bearer"})
            return

        if path == "/provider":
            self._send(200, provider_admin.describe())
            return

        if path == "/audit":
            _, query = self._route()
            try:
                limit = int(query.get("limit", ["50"])[0])
            except (TypeError, ValueError):
                limit = 50
            incident_id = query.get("incident_id", [""])[0].strip()
            try:
                rows = audit.recent(limit, incident_id=incident_id)
            except audit.AuditError as exc:
                self._send(503, {"error": f"audit log unavailable: {exc}",
                                 "hint": "check the workspace volume / "
                                         "AGENT_AUDIT_DIR"})
                return
            self._send(200, {"count": len(rows), "records": rows})
            return

        if path == "/incidents" or path.startswith("/incidents/"):
            self._get_incidents(path)
            return

        if path == "/policy":
            self._get_policy()
            return

        if path == "/approvals" or path.startswith("/approvals/"):
            self._get_approvals(path)
            return

        if path == "/tasks" or path.startswith("/tasks/"):
            self._get_tasks(path)
            return

        if path == "/jobs":
            self._send(200, {"jobs": list_jobs(50)})
            return

        if path.startswith("/jobs/"):
            segments = path.strip("/").split("/")
            job_id = segments[1]
            job = get_job(job_id)
            if job is None:
                self._send(404, {"error": "unknown job", "job_id": job_id})
                return
            if len(segments) == 2:
                self._send(200, job.payload())
                return
            if len(segments) == 3 and segments[2] == "events":
                self._stream(job)
                return

        self._send(404, {"error": "not found", "path": path})

    def _stream(self, job: Job) -> None:
        """Server-sent events: log lines plus a final status event."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        sent = 0
        try:
            while True:
                lines = list(job.logs)
                if len(lines) < sent:      # deque wrapped; resend from the start
                    sent = 0
                for line in lines[sent:]:
                    self.wfile.write(
                        f"data: {json.dumps({'log': line})}\n\n".encode("utf-8"))
                sent = len(lines)
                state = job.summary()
                if job.done.is_set():
                    state["result"] = job.result
                self.wfile.write(
                    f"event: status\ndata: {json.dumps(state)}\n\n".encode("utf-8"))
                self.wfile.flush()
                if job.done.is_set():
                    break
                time.sleep(0.5)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # -- POST ------------------------------------------------------------- #
    def do_POST(self):  # noqa: N802
        path, query = self._route()

        if not self._check_auth():
            return

        if path == "/provider/test":
            try:
                payload = asyncio.run(llm.test())
            except Exception as exc:
                self._send(502, {"ok": False,
                                 "error": f"{type(exc).__name__}: {exc}"})
                return
            self._send(200, payload)
            return

        if path == "/provider":
            try:
                body = self._read_json()
            except ValueError as exc:
                self._send(400, {"error": str(exc)})
                return
            try:
                payload = provider_admin.apply(body)
            except provider_admin.ProviderError as exc:
                self._send(400, {"ok": False, "error": str(exc)})
                return
            self._send(200, payload)
            return

        if path == "/incidents/ingest":
            store = self._incident_store()
            if store is None:
                return
            try:
                record = self._read_json()
            except ValueError as exc:
                self._send(400, {"error": str(exc)})
                return
            if not str(record.get("id") or "").strip():
                self._send(400, {"error": "missing 'id'",
                                 "hint": "send the record created by "
                                         "IncidentStore.create()"})
                return
            if not record.get("resource"):
                record["resource"] = incidents.resource_key(
                    str(record.get("kind") or ""), str(record.get("name") or ""))
            record["ingested_at"] = round(time.time(), 3)
            try:
                store.save(record)
            except incidents.IncidentError as exc:
                self._send(400, {"error": str(exc)})
                return
            self._send(200, {"ok": True, "id": record["id"],
                             "status": record.get("status")})
            return

        if path == "/tasks":
            self._post_task()
            return

        if path.startswith("/approvals/") and path.endswith(("/approve",
                                                             "/reject")):
            self._post_approval(path)
            return

        if path.startswith("/tasks/") and path.endswith("/cancel"):
            segments = path.strip("/").split("/")
            if len(segments) == 3:
                self._cancel_task(segments[1])
                return
            self._send(404, {"error": "not found", "path": path})
            return

        if path == "/run":
            try:
                body = self._read_json()
            except ValueError as exc:
                self._send(400, {"error": str(exc)})
                return
            task = str(body.get("task") or "").strip()
            if not task:
                self._send(400, {"error": "missing 'task'",
                                 "hint": 'send {"task": "what to do"}'})
                return
            async_mode = (
                query.get("async", ["0"])[0].lower() in ("1", "true", "yes", "on")
                or bool(body.get("async"))
            )
            try:
                job = submit(task)
            except QueueFull as exc:
                self._send(429, {"error": str(exc), "hint": "retry later"})
                return

            if async_mode:
                self._send(202, {"job_id": job.id, "status": job.status,
                                 "poll": f"/jobs/{job.id}"})
                return

            # Synchronous mode: wait for the executor (n8n expects the result).
            if hasattr(job, "wait"):
                finished = job.wait(JOB_TIMEOUT + 60)
            else:
                finished = job.done.wait(JOB_TIMEOUT + 60)
            if not finished:
                self._send(504, {"error": "job did not finish in time",
                                 "job_id": job.id, "hint": f"poll /jobs/{job.id}"})
                return
            if job.status == "error":
                self._send(500, {"error": job.error, "job_id": job.id})
                return
            payload = job.result or {"final": "(no result)", "steps": [],
                                     "stop_reason": job.status}
            payload["job_id"] = job.id
            self._send(200, payload)
            return

        if path.startswith("/jobs/") and path.endswith("/cancel"):
            job_id = path.strip("/").split("/")[1]
            job = cancel_job(job_id)
            if job is None:
                self._send(404, {"error": "unknown job", "job_id": job_id})
                return
            self._send(200, {"job_id": job.id, "status": job.status,
                             "cancelled": True})
            return

        self._send(404, {"error": "not found", "path": path})

    # -- DELETE ----------------------------------------------------------- #
    def do_DELETE(self):  # noqa: N802
        path, _ = self._route()
        if not self._check_auth():
            return
        if path == "/provider/key":
            try:
                payload = provider_admin.clear_key()
            except provider_admin.ProviderError as exc:
                self._send(400, {"ok": False, "error": str(exc)})
                return
            self._send(200, payload)
            return
        self._send(404, {"error": "not found", "path": path})


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def main() -> None:
    if not API_TOKEN and not AUTH_DISABLED:
        print("[agent-service] refusing to start: AGENT_API_TOKEN is not set. "
              "The API can control the cluster, so it must not run unauthenticated. "
              "Set AGENT_AUTH_DISABLED=1 only for local development.",
              file=sys.stderr, flush=True)
        raise SystemExit(1)
    if redis_mode():
        try:
            store = _agent_store()
        except ops_queue.QueueError as exc:
            print(f"[agent-service] refusing to start: AGENT_QUEUE=redis but the "
                  f"queue is unavailable: {exc}. There is no memory fallback: "
                  f"two API pods with separate queues would look healthy and "
                  f"lose work.", file=sys.stderr, flush=True)
            raise SystemExit(1)
        print(f"[agent-service] queue=redis/{store.backend.name} "
              f"depth={queue_depth()}", flush=True)
    start_worker()
    srv = ThreadingHTTPServer((BIND, PORT), Handler)
    print(f"[agent-service] listening on {BIND}:{PORT}  model={MODEL}  "
          f"ollama={OLLAMA_HOST}  queue={QUEUE_KIND}/{MAX_QUEUE}  "
          f"timeout={JOB_TIMEOUT}s  "
          f"auth={'off' if AUTH_DISABLED else 'bearer'}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
