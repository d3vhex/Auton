#!/usr/bin/env python3
"""Auton **ops executor**: the pod that owns the GPU and runs agent tasks.

The API (`agent_service.py`) is only a producer when `AGENT_QUEUE=redis`; this
process is the single consumer of the `agent` queue. It:

1. takes the global GPU slot (`SET NX` in Redis, TTL for a crashed owner),
2. claims one `agent` task and marks it running,
3. runs `agent_core.run_agent`, forwarding every log line to the task stream
   the UI reads, and checks `cancel_requested` at each step boundary,
4. finishes the task as `done` / `cancelled` / `error`.

There is **no automatic retry** for agent jobs: a failed run is a result, not a
transient queue error. A crashed executor is different — its lease expires and
the task is requeued by `TaskStore.recover()` (at-least-once).

Replicas: 1. The GPU slot makes a second replica safe (it would just defer),
but more executors do not make one GPU faster.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
import threading
import time
import traceback

from agent_core import run_agent
import ops_queue
import task_store

INTERVAL = max(1, int(os.environ.get("AGENT_AGENT_POLL", "5")))
JOB_TIMEOUT = max(10, int(os.environ.get("AGENT_JOB_TIMEOUT", "900")))
HEARTBEAT = os.environ.get("AGENT_OPS_HEARTBEAT",
                           "/tmp/agent-heartbeat").strip()
STOP = threading.Event()


def log(message: str) -> None:
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} "
          f"[agent-worker] {message}", flush=True)


def touch_heartbeat() -> None:
    if not HEARTBEAT:
        return
    try:
        with open(HEARTBEAT, "w", encoding="utf-8") as fh:
            fh.write(str(int(time.time())))
    except OSError:
        pass


def _stop(signum, frame):  # noqa: ARG001 - signal handler signature
    log(f"received signal {signum}; finishing the current step")
    STOP.set()


def run_one(store, claim) -> str:
    """Execute one claimed agent task. Returns the final status."""
    task_id = claim.id
    task = claim.task
    owner = f"{store.consumer}:{task_id}"
    if not store.acquire_gpu(owner, ttl=JOB_TIMEOUT + 300):
        store.defer(claim, delay=INTERVAL, note="GPU slot is busy")
        log(f"task {task_id} deferred: GPU slot busy")
        return "deferred"

    record = store.start(claim)
    deadline = time.time() + JOB_TIMEOUT

    def job_log(*parts) -> None:
        line = " ".join(str(p) for p in parts)
        print(f"[job {task_id}] {line}", flush=True)
        try:
            store.append_log(task_id, line)
        except ops_queue.QueueError:
            pass  # logs are best effort; the result is the contract

    def should_stop() -> bool:
        if STOP.is_set() or time.time() > deadline:
            return True
        try:
            return store.cancel_requested(task_id)
        except ops_queue.QueueError:
            # A Redis blip must not kill a healthy long-running job.
            return False

    job_log(f"[agent] start task={str(record.get('task') or '')[:120]!r}")
    try:
        result = asyncio.run(run_agent(str(record.get("task") or ""),
                                      log=job_log, should_stop=should_stop))
        status = ("cancelled" if result.get("stop_reason") == "cancelled"
                  else "done")
    except Exception as exc:
        traceback.print_exc()
        result = {"final": "", "steps": [],
                  "stop_reason": "error", "elapsed": 0,
                  "error": f"{type(exc).__name__}: {exc}"}
        status = "error"
        job_log(f"[agent] {status}: {result['error']}")
    finally:
        store.release_gpu(owner)

    elapsed = result.get("elapsed")
    job_log(f"[agent] {status} in {elapsed}s")
    store.finish(claim, status=status, result=result)
    return status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true",
                        help="run at most one agent task and exit")
    args = parser.parse_args(argv)

    try:
        store = task_store.TaskStore(ops_queue.create_backend())
    except ops_queue.QueueError as exc:
        log(f"error: queue backend unavailable: {exc}")
        return 2

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    log(f"starting: backend={store.backend.name} "
        f"lease={store.lease}s poll={INTERVAL}s timeout={JOB_TIMEOUT}s")

    while not STOP.is_set():
        touch_heartbeat()
        try:
            store.heartbeat("ops")
        except ops_queue.QueueError as exc:
            log(f"heartbeat failed: {exc}")
        try:
            claims = store.claim("agent", limit=1)
        except ops_queue.QueueError as exc:
            log(f"claim failed (queue): {exc}")
            STOP.wait(INTERVAL)
            continue
        except Exception as exc:
            log(f"claim failed: {type(exc).__name__}: {exc}")
            STOP.wait(INTERVAL)
            continue

        if not claims:
            if args.once:
                break
            STOP.wait(INTERVAL)
            continue

        claim = claims[0]
        try:
            status = run_one(store, claim)
            log(f"task {claim.id} -> {status}")
            log(f"result: {json.dumps({'task_id': claim.id, 'status': status})}")
        except ops_queue.QueueError as exc:
            # The lease stays in Redis; recover() requeues the task when it
            # expires. Falling back to a local run would bypass the audit trail.
            log(f"task {claim.id}: queue error: {exc}")
        except Exception as exc:
            log(f"task {claim.id} failed: {type(exc).__name__}: {exc}")
            try:
                store.finish(claim, status="error", result={
                    "final": "", "steps": [], "stop_reason": "error",
                    "error": f"{type(exc).__name__}: {exc}"})
            except Exception as finish_exc:
                log(f"task {claim.id}: cannot record the error: {finish_exc}")
        if args.once:
            break
    return 0


if __name__ == "__main__":
    sys.exit(main())
