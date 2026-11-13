#!/usr/bin/env python3
"""Autonomous ops **scanner**: read-only detection that enqueues repair tasks.

This is the read-only half of the old `ops_controller.py` loop. It:

1. takes a read-only snapshot of each namespace in ``AGENT_OPS_NAMESPACES``
   with the same code the agent uses (``k8s_tools._collect``),
2. turns deterministic symptoms into findings (``ops_controller.findings``),
3. submits one task per finding to the ``repair`` queue (``scan_once``).

It never mutates a workload, so it runs under the read-only ``auton-scanner``
ServiceAccount. Any number of scanners can run at once: duplicate submissions
collapse on the idempotency key, and an active task, cooldown or recent
incident suppresses a finding before it is queued.

``--once`` runs a single scan (chaos tests / CronJob-style setups).
"""
from __future__ import annotations

import argparse
import json
import signal
import sys

import ops_controller as ops
import ops_queue


def _stop(signum, frame):  # noqa: ARG001 - signal handler signature
    ops.log(f"[scanner] received signal {signum}; stopping after this scan")
    ops.STOP.set()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true",
                        help="scan once, print the summary and exit")
    args = parser.parse_args(argv)

    if not ops.k8s_tools.ENABLED:
        ops.log("[scanner] error: Kubernetes tools are disabled "
                "(AGENT_K8S_ENABLED=0)")
        return 2
    if ops.k8s_tools.WRITE:
        ops.log("[scanner] warning: AGENT_K8S_WRITE=1 but the scanner never "
                "writes; its RBAC identity should be read-only")

    try:
        store = ops.task_store.TaskStore(ops_queue.create_backend())
    except ops_queue.QueueError as exc:
        # Fail closed: a scanner with a memory queue would enqueue work into a
        # process that dies, and nobody would ever see it.
        ops.log(f"[scanner] error: queue backend unavailable: {exc}")
        return 2

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    ops.log(f"[scanner] starting: namespaces={','.join(ops.NAMESPACES)} "
            f"interval={ops.SCAN_INTERVAL}s backend={store.backend.name} "
            f"dedupe={ops.DEDUPE_WINDOW}s dry_run={ops.DRY_RUN}")

    while not ops.STOP.is_set():
        try:
            summary = ops.scan_once(store)
            ops.log(f"[scanner] scan complete: {json.dumps(summary)}")
        except ops_queue.QueueError as exc:
            # The queue is the safety boundary: do not fall back to memory.
            ops.log(f"[scanner] scan failed (queue): {exc}")
        except Exception as exc:  # a loop must never die on one bad pass
            ops.log(f"[scanner] scan failed: {type(exc).__name__}: {exc}")
        if args.once:
            break
        ops.STOP.wait(ops.SCAN_INTERVAL)
    return 0


if __name__ == "__main__":
    sys.exit(main())
