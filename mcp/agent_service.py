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
  GET  /provider                -> active model provider (key masked)
  POST /provider/test           -> one tiny completion against the provider

`/` and `/ui` serve the bundled page; `/health` stays open (probes);
everything else requires `Authorization: Bearer $AGENT_API_TOKEN`.
The service refuses to start without a token unless AGENT_AUTH_DISABLED=1.

One worker thread owns the GPU: jobs run FIFO, so two tasks never fight over
VRAM. No extra deps — stdlib http.server only. Listen addr/port via AGENT_BIND
(default 0.0.0.0) / AGENT_PORT (default 8080).

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
import audit
import incidents
import llm

UI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui")

BIND = os.environ.get("AGENT_BIND", "0.0.0.0")
PORT = int(os.environ.get("AGENT_PORT", "8080"))
MAX_QUEUE = max(1, int(os.environ.get("AGENT_MAX_QUEUE", "8")))
JOB_TIMEOUT = max(10, int(os.environ.get("AGENT_JOB_TIMEOUT", "900")))
MAX_JOBS = max(10, int(os.environ.get("AGENT_MAX_JOBS", "100")))
MAX_BODY = 2 * 1024 * 1024  # 2 MiB is plenty for a task string
LOG_TAIL = 300

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


def submit(task: str) -> Job:
    """Queue a task. Raises QueueFull instead of piling up unbounded work."""
    job = Job(task)
    with JOBS_LOCK:
        if QUEUE.qsize() >= MAX_QUEUE:
            raise QueueFull(f"queue is full ({MAX_QUEUE} jobs)")
        JOBS[job.id] = job
        _prune_locked()
        QUEUE.put(job.id)
    return job


def cancel_job(job_id: str) -> Job | None:
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
    """Start the single GPU worker (idempotent)."""
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
    def _send(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
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
                         "hint": "send 'Authorization: Bearer <token>'"})
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
                records = store.list(limit=limit, status=status)
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

    # -- GET -------------------------------------------------------------- #
    def do_GET(self):  # noqa: N802
        path, _ = self._route()
        if path in ("/", "/ui"):
            self._serve_ui()
            return
        if path == "/health":
            with JOBS_LOCK:
                running = sum(1 for j in JOBS.values() if j.status == "running")
            info = llm.provider_info()
            self._send(200, {
                "status": "ok",
                "provider": info.get("provider", "ollama"),
                "model": info.get("model", MODEL),
                "local_model_enabled": info.get("local_model_enabled", True),
                "ollama": OLLAMA_HOST,
                "queue_depth": QUEUE.qsize(),
                "jobs_running": running,
                "jobs_known": len(JOBS),
            })
            return

        if not self._check_auth():
            return

        if path == "/provider":
            self._send(200, llm.provider_info())
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

        if path == "/jobs":
            with JOBS_LOCK:
                jobs = sorted(JOBS.values(), key=lambda j: j.created, reverse=True)
            self._send(200, {"jobs": [j.summary() for j in jobs[:50]]})
            return

        if path.startswith("/jobs/"):
            segments = path.strip("/").split("/")
            job_id = segments[1]
            with JOBS_LOCK:
                job = JOBS.get(job_id)
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

            # Synchronous mode: wait for the worker (n8n expects the result).
            if not job.done.wait(JOB_TIMEOUT + 60):
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
    start_worker()
    srv = ThreadingHTTPServer((BIND, PORT), Handler)
    print(f"[agent-service] listening on {BIND}:{PORT}  model={MODEL}  "
          f"ollama={OLLAMA_HOST}  queue={MAX_QUEUE}  timeout={JOB_TIMEOUT}s  "
          f"auth={'off' if AUTH_DISABLED else 'bearer'}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
