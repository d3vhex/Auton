#!/usr/bin/env python3
"""Autonomous ops **worker**: claims repair tasks and runs the playbooks.

The write half of the old controller. It:

1. claims one repair task from the queue (blocking-free; ``AGENT_WORKER_POLL``
   controls the idle wait),
2. runs ``ops_controller.execute_task``: cooldown → report-only → dry run →
   hourly budget → incident → playbook (audit-gated) → verify → notify,
3. finishes the task as ``resolved`` / ``needs_human`` / ``failed``, or
   ``retry``s it with backoff when the playbook raised.

Concurrency rules live in ``task_store``: different resources run in parallel in
different worker pods, the *same* resource is serialised by a Redis lock, a
crashed worker's lease expires and its task is requeued, and the hourly budget
is a single atomic counter shared by all workers.

``--once`` claims and runs at most one task, then exits (chaos tests).
"""
from __future__ import annotations

import argparse
import json
import signal
import sys
import time

import incidents
import ops_controller as ops
import ops_queue


def _stop(signum, frame):  # noqa: ARG001 - signal handler signature
    ops.log(f"[worker] received signal {signum}; finishing the current task")
    ops.STOP.set()


def _prune_due(last: float) -> bool:
    return (time.time() - last) >= ops.PRUNE_EVERY


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true",
                        help="run at most one task and exit")
    args = parser.parse_args(argv)

    if not ops.k8s_tools.ENABLED:
        ops.log("[worker] error: Kubernetes tools are disabled "
                "(AGENT_K8S_ENABLED=0)")
        return 2
    if not ops.k8s_tools.WRITE and not ops.DRY_RUN:
        ops.log("[worker] warning: AGENT_K8S_WRITE=0; playbooks will refuse to "
                "mutate (use AGENT_OPS_DRY_RUN=1 for a report-only worker)")
    if not ops.API_TOKEN:
        ops.log("[worker] warning: AGENT_API_TOKEN is empty; "
                "/incidents/ingest will answer 401")

    try:
        store = ops.task_store.TaskStore(ops_queue.create_backend())
    except ops_queue.QueueError as exc:
        ops.log(f"[worker] error: queue backend unavailable: {exc}")
        return 2

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    ops.log(f"[worker] starting: namespaces={','.join(ops.NAMESPACES)} "
            f"poll={ops.WORKER_POLL}s backend={store.backend.name} "
            f"lease={store.lease}s max_attempts={store.max_attempts} "
            f"dry_run={ops.DRY_RUN} api={ops.API_URL} "
            f"n8n={'on' if ops.N8N_WEBHOOK else 'off'}")

    last_prune = 0.0
    while not ops.STOP.is_set():
        ops.touch_heartbeat()
        try:
            store.heartbeat("worker")
        except ops_queue.QueueError as exc:
            ops.log(f"[worker] heartbeat failed: {exc}")
        try:
            claims = store.claim("repair", limit=1)
        except ops_queue.QueueError as exc:
            # Backend down: do not guess, wait and let the lease/health checks
            # page a human. Never fall back to memory.
            ops.log(f"[worker] claim failed (queue): {exc}")
            ops.STOP.wait(ops.WORKER_POLL)
            continue
        except Exception as exc:
            ops.log(f"[worker] claim failed: {type(exc).__name__}: {exc}")
            ops.STOP.wait(ops.WORKER_POLL)
            continue

        if not claims:
            try:
                ops.flush_pending()
            except Exception as exc:
                ops.log(f"[worker] report flush failed: "
                        f"{type(exc).__name__}: {exc}")
            if _prune_due(last_prune):
                try:
                    removed = ops.STORE.prune()
                    if removed:
                        ops.log(f"[worker] pruned {removed} incident(s)")
                except incidents.IncidentError as exc:
                    ops.log(f"[worker] prune failed: {exc}")
                last_prune = time.time()
            if args.once:
                break
            ops.STOP.wait(ops.WORKER_POLL)
            continue

        claim = claims[0]
        try:
            result = ops.execute_task(store, claim)
            ops.log(f"[worker] task result: {json.dumps(result)}")
        except ops_queue.QueueError as exc:
            # The lease is still in the backend; recovery requeues the task
            # once it expires. Retrying here would need the same broken Redis.
            ops.log(f"[worker] task {claim.id}: queue error: {exc}")
        except Exception as exc:
            ops.log(f"[worker] task {claim.id} failed: "
                    f"{type(exc).__name__}: {exc}")
            try:
                store.retry(claim, reason=f"{type(exc).__name__}: {exc}")
            except Exception as retry_exc:
                ops.log(f"[worker] task {claim.id}: retry failed too: "
                        f"{retry_exc}")
        try:
            ops.flush_pending()
        except Exception as exc:
            ops.log(f"[worker] report flush failed: {type(exc).__name__}: {exc}")
        if args.once:
            break
    return 0


if __name__ == "__main__":
    sys.exit(main())
