#!/usr/bin/env python3
"""Autonomous ops **scanner**: read-only detection that enqueues repair tasks.

This is the read-only half of the old ``ops_controller.py`` loop. It never
mutates a workload, so it runs under the read-only ``auton-scanner``
ServiceAccount. Any number of scanners can run at once: duplicate submissions
collapse on the idempotency key, and an active task, cooldown or recent
incident suppresses a finding before it is queued.

Phase 3 changed *how* it looks, not *what* it decides.

Before, every scanner asked the API server the same question every 60
seconds — "what does this namespace look like right now?" — and parsed a full
snapshot of pods, deployments, statefulsets and PVCs to answer it. A pod that
was OOMKilled at second 3 was noticed at second 60, and the API server paid
for a full listing per replica per minute.

Now the scanner **watches**:

    ┌──────────┐   list()                    ┌──────────────────┐
    │ watcher  │ ──────────────────────────▶ │ api-server       │
    │  thread  │   watch(rv=12345)            │                  │
    │          │ ◀────────────────────────── │ ADDED/MODIFIED/  │
    └──────────┘   event stream              │ DELETED/BOOKMARK │
                 ▲                           └──────────────────┘
                 │ reconnect with backoff
                 └── 410 Gone ──▶ re-list ──▶ new resourceVersion

Two tiers run side by side:

**fast tier** (every ``AGENT_WATCH_FAST_INTERVAL``, default 60 s, and
immediately when an event says something is wrong) reads the watch cache and
applies the pod-level rules. No API call is made at all.

**full tier** (every ``AGENT_WATCH_FULL_INTERVAL``, default 300 s) takes a
real snapshot and applies every rule — workload-level ones included.

The watcher is an *optimisation*, never a dependency. If the API server is
unreachable, if the ServiceAccount may not watch, or if the watch is switched
off, the scanner falls back to polling exactly as it did before. Detection
must never stop because a watch stream did.

The health of every watch stream is published to the queue backend and is
visible at ``GET /tasks/stats`` under ``watchers`` — "is the platform still
watching?" has to be answerable from the outside.

``--once`` runs a single scan (chaos tests / CronJob-style setups).
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time

import ops_controller as ops
import ops_queue
import watcher as watch_api


def _flag(name: str, default: bool = True) -> bool:
    value = os.environ.get(name, "1" if default else "0").strip().lower()
    return value in ("1", "true", "yes", "on")


#: The watch streams the scanner opens per namespace.
#:
#: The first two are what the detection rules need; the other three are what
#: the **full tier** snapshot needs. Watching them means the snapshot can be
#: served from the cache instead of five fresh listings — and the snapshot
#: itself then goes over direct HTTPS (see :mod:`k8s_ops`).
WATCH_RESOURCES = ("pods", "deployments", "statefulsets",
                   "persistentvolumeclaims", "events")

#: The resources whose events only trigger the *full* tier. A pod event can
#: trigger the fast tier; a PVC or an event cannot.
FULL_ONLY_RESOURCES = ("statefulsets", "persistentvolumeclaims", "events")

#: Master switch. ``AGENT_WATCH_ENABLED=0`` keeps the old polling behaviour.
WATCH_ENABLED = _flag("AGENT_WATCH_ENABLED", True)

#: How often the fast tier reads the cache.
WATCH_FAST_INTERVAL = max(5, int(os.environ.get(
    "AGENT_WATCH_FAST_INTERVAL", "60")))

#: How often the full tier takes a real snapshot.
WATCH_FULL_INTERVAL = max(30, int(os.environ.get(
    "AGENT_WATCH_FULL_INTERVAL", "300")))

#: How long the API server keeps a watch stream open before closing it.
WATCH_TIMEOUT = max(30, int(os.environ.get("AGENT_WATCH_TIMEOUT", "300")))

#: The smallest gap between two event-triggered scans (debounce).
WATCH_MIN_GAP = max(0, int(os.environ.get("AGENT_WATCH_MIN_GAP", "5")))

#: How long a watched Event stays in the cache. The API server garbage
#: collects events after about an hour; the cache keeps them a little longer
#: than the scan interval, so a scan always sees the recent ones.
WATCH_EVENT_TTL = max(60, int(os.environ.get("AGENT_WATCH_EVENT_TTL", "900")))


def _stop(signum, frame):  # noqa: ARG001 - signal handler signature
    ops.log(f"[scanner] received signal {signum}; stopping after this scan")
    ops.STOP.set()


class WatchPlane:
    """The watch threads, the cache and the trigger.

    One thread per (resource, namespace). They share one cache, because the
    fast path needs the whole namespace view, and they feed one wake-up
    event: the scanner wakes up either for a fast (pod-level) scan or for a
    full (snapshot) scan.
    """

    def __init__(self, namespaces: list[str]):
        self.namespaces = list(namespaces)
        # Events are high churn: they get a short TTL so the cache cannot
        # grow without bound. Everything else is kept until it is deleted.
        self.cache = watch_api.ObjectCache({"events": WATCH_EVENT_TTL})
        self.watchers: list[watch_api.Watcher] = []
        self.threads: list[threading.Thread] = []
        self.wake = threading.Event()
        self.started = False
        self.reason = ""
        self._last_fast = 0.0
        self._last_full = 0.0
        self._pending_fast = False
        self._pending_full = False
        self._lock = threading.Lock()
        self.counters = {"fast": 0, "full": 0, "events": 0,
                         "triggered_fast": 0, "triggered_full": 0}

    # -- lifecycle -------------------------------------------------------- #
    def start(self) -> bool:
        """Open the watch streams. Returns ``False`` when unavailable."""
        config = watch_api.resolve_config()
        if config is None:
            self.reason = ("no API server credentials (not running in the "
                           "cluster and AGENT_K8S_API/AGENT_K8S_TOKEN unset)")
            return False
        client = watch_api.ApiClient(config, timeout=WATCH_TIMEOUT)
        for namespace in self.namespaces:
            for resource in WATCH_RESOURCES:
                stream = watch_api.Watcher(
                    client, resource, namespace,
                    name=f"{resource}/{namespace}",
                    timeout_seconds=WATCH_TIMEOUT,
                    cache=self.cache)
                self.watchers.append(stream)
                thread = threading.Thread(
                    target=stream.run,
                    kwargs={"on_event": self._handler(resource, namespace)},
                    name=f"watch-{resource}-{namespace}", daemon=True)
                thread.start()
                self.threads.append(thread)
        self.started = True
        self.reason = "watching"
        return True

    def stop(self) -> None:
        for stream in self.watchers:
            try:
                stream.stop()
            except Exception:  # pragma: no cover - best effort
                pass

    # -- events ----------------------------------------------------------- #
    def _handler(self, resource: str, namespace: str):
        def handler(kind: str, obj: dict) -> None:
            self._on_event(resource, namespace, kind, obj)
        return handler

    def _on_event(self, resource: str, namespace: str, kind: str,
                  obj: dict) -> None:
        """Decide whether an event deserves a scan right now."""
        with self._lock:
            self.counters["events"] += 1
        if resource == "pods":
            try:
                summary = ops.k8s_tools.pod_summary(obj)
            except Exception as exc:  # a malformed pod must not stop the watch
                ops.log(f"[scanner] cannot summarise a pod event: {exc}")
                return
            reasons = summary.get("reasons") or []
            if not any(reason in ops.REASONS for reason in reasons):
                return
            detail = f"{summary.get('name')}: {','.join(reasons)}"
            self._fire_fast(f"pod event {kind} {detail}")
            return
        # A workload changed: the workload-level rules may apply, so the full
        # reconciliation runs instead of the pod-only fast path.
        metadata = obj.get("metadata") or {}
        name = str(metadata.get("name") or "")
        self._fire_full(f"{resource} event {kind} {name}")

    def _fire_fast(self, detail: str) -> None:
        now = time.monotonic()
        if WATCH_MIN_GAP and now - self._last_fast < WATCH_MIN_GAP:
            return
        self._last_fast = now
        with self._lock:
            self._pending_fast = True
            self.counters["triggered_fast"] += 1
        ops.log(f"[scanner] watch trigger (fast): {detail}")
        self.wake.set()

    def _fire_full(self, detail: str) -> None:
        now = time.monotonic()
        if WATCH_MIN_GAP and now - self._last_full < WATCH_MIN_GAP:
            return
        self._last_full = now
        with self._lock:
            self._pending_full = True
            self.counters["triggered_full"] += 1
        ops.log(f"[scanner] watch trigger (full): {detail}")
        self.wake.set()

    def take_pending(self) -> tuple[bool, bool]:
        """Consume the pending triggers."""
        with self._lock:
            fast, full = self._pending_fast, self._pending_full
            self._pending_fast = False
            self._pending_full = False
        return fast, full

    # -- reads ------------------------------------------------------------ #
    def pod_summaries(self, namespace: str) -> list[dict]:
        """The cached pods of one namespace, flattened like a snapshot."""
        pods = self.cache.objects("pods", namespace)
        out = []
        for pod in pods:
            try:
                out.append(ops.k8s_tools.pod_summary(pod))
            except Exception as exc:  # pragma: no cover - defensive
                ops.log(f"[scanner] cannot summarise a cached pod: {exc}")
        return out

    def healthy(self) -> bool:
        """True when at least one stream is alive and listing succeeded."""
        for stream in self.watchers:
            stats = stream.stats()
            if stats.get("available") and stats.get("lists", 0) > 0:
                return True
        return False

    def stats(self) -> dict:
        streams = [stream.stats() for stream in self.watchers]
        with self._lock:
            counters = dict(self.counters)
        return {
            "started": self.started,
            "reason": self.reason,
            "resources": list(WATCH_RESOURCES),
            "namespaces": list(self.namespaces),
            "cache_size": self.cache.size(),
            "streams": streams,
            "counters": counters,
        }


def _fast_pass(store, plane: WatchPlane) -> dict:
    """The fast tier: pod-level detection straight from the watch cache."""
    totals = {"namespaces": [], "findings": 0, "queued": 0, "duplicates": 0,
              "suppressed": 0, "errors": 0, "tasks": [], "source": "watch"}
    for namespace in plane.namespaces:
        if ops.STOP.is_set():
            break
        pods = plane.pod_summaries(namespace)
        if not pods:
            continue
        summary = ops.scan_pods(store, pods, namespace)
        totals["namespaces"].append(namespace)
        for key in ("findings", "queued", "duplicates", "suppressed", "errors"):
            totals[key] += summary.get(key, 0)
        totals["tasks"].extend(summary.get("tasks") or [])
    return totals


def _publish(store, plane: WatchPlane) -> None:
    """Publish the health of every stream so ``/tasks/stats`` can show it."""
    try:
        for stream in plane.watchers:
            store.set_watcher_stats(stream.name, stream.stats())
        store.set_watcher_stats("scanner", {
            "name": "scanner",
            "resource": "summary",
            "available": plane.healthy(),
            "started": plane.started,
            "reason": plane.reason,
            "cache_size": plane.cache.size(),
            "counters": dict(plane.counters),
            # Which transport answered: the direct HTTPS client or kubectl.
            # A fallback is never silent, and this is where it is visible.
            "transport": dict(ops.k8s_api.COUNTERS),
        })
    except Exception as exc:  # publishing must never break the scan
        ops.log(f"[scanner] cannot publish the watcher stats: {exc}")


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

    plane = WatchPlane(ops.NAMESPACES)
    watching = False
    if WATCH_ENABLED:
        watching = plane.start()
        if watching:
            ops.log(f"[scanner] watch started: "
                    f"{len(plane.watchers)} streams over "
                    f"{','.join(ops.NAMESPACES)}")
        else:
            ops.log(f"[scanner] watch unavailable ({plane.reason}); "
                    f"falling back to polling every {ops.SCAN_INTERVAL}s")
    else:
        ops.log("[scanner] watch disabled (AGENT_WATCH_ENABLED=0); "
                f"polling every {ops.SCAN_INTERVAL}s")

    fast_interval = WATCH_FAST_INTERVAL if watching else ops.SCAN_INTERVAL
    full_interval = WATCH_FULL_INTERVAL if watching else ops.SCAN_INTERVAL

    ops.log(f"[scanner] starting: namespaces={','.join(ops.NAMESPACES)} "
            f"interval={ops.SCAN_INTERVAL}s backend={store.backend.name} "
            f"dedupe={ops.DEDUPE_WINDOW}s dry_run={ops.DRY_RUN} "
            f"watch={'on' if watching else 'off'} "
            f"fast={fast_interval}s full={full_interval}s")

    if args.once:
        try:
            summary = ops.scan_once(store)
            ops.log(f"[scanner] scan complete: {json.dumps(summary)}")
        except ops_queue.QueueError as exc:
            ops.log(f"[scanner] scan failed (queue): {exc}")
        except Exception as exc:
            ops.log(f"[scanner] scan failed: {type(exc).__name__}: {exc}")
        _publish(store, plane)
        plane.stop()
        return 0

    next_fast = time.monotonic() + fast_interval
    next_full = time.monotonic() + full_interval

    try:
        while not ops.STOP.is_set():
            now = time.monotonic()
            timeout = max(0.5, min(next_fast - now, next_full - now))
            # Wake up early when an event says something is wrong.
            plane.wake.wait(timeout)
            plane.wake.clear()
            if ops.STOP.is_set():
                break

            pending_fast, pending_full = plane.take_pending()
            now = time.monotonic()
            run_fast = pending_fast or now >= next_fast
            run_full = pending_full or now >= next_full
            if run_fast:
                next_fast = time.monotonic() + fast_interval
            if run_full:
                next_full = time.monotonic() + full_interval

            # The cache holds the watched objects; the high-churn ones
            # (events) are dropped once they are stale.
            pruned = plane.cache.prune()
            if pruned:
                ops.log(f"[scanner] pruned {pruned} stale cache entries")

            try:
                if run_fast and watching:
                    summary = _fast_pass(store, plane)
                    plane.counters["fast"] += 1
                    if summary["findings"] or summary["queued"]:
                        ops.log(f"[scanner] fast scan complete: "
                                f"{json.dumps(summary)}")
                if run_full:
                    summary = ops.scan_once(store)
                    plane.counters["full"] += 1
                    ops.log(f"[scanner] scan complete: {json.dumps(summary)}")
            except ops_queue.QueueError as exc:
                # The queue is the safety boundary: do not fall back to memory.
                ops.log(f"[scanner] scan failed (queue): {exc}")
            except Exception as exc:  # a loop must never die on one bad pass
                ops.log(f"[scanner] scan failed: {type(exc).__name__}: {exc}")

            _publish(store, plane)
    finally:
        plane.stop()

    return 0


if __name__ == "__main__":
    sys.exit(main())
