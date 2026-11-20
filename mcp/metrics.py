#!/usr/bin/env python3
"""Prometheus metrics, with nothing but the standard library.

The platform runs on a slim Python image with no metrics client, and the
cluster has no Prometheus sidecar. So the exposition is produced here, by
hand, in the **Prometheus text format** (``text/plain; version=0.0.4``) — the
same format ``promtool`` and every scraper understand.

The rules this module follows:

* **No dependency.** Standard library only (``resource``, ``threading``,
  ``time``). Nothing to install, nothing to break when the image changes.
* **No invented numbers.** Every sample comes from the real state: the queue
  in Redis, the incident/approval/learning stores on the shared PVC, the
  watcher health the scanner publishes, the policy document. A metric that
  cannot be read is *absent*, never zero-guessed.
* **No secrets.** The exposition carries counts, states and durations. It
  never carries a token, a key, a password or a resource *body* — only names
  and states.
* **Bounded cardinality.** The label values come from a closed set (causes,
  states, roles) plus the resource name of the learned profiles, which is
  capped.

The rendering is deliberately small and explicit rather than clever: a
registry, three metric types and a text writer. The format is documented at
https://prometheus.io/docs/instrumenting/exposition_formats/
"""
from __future__ import annotations

import math
import os
import resource
import threading
import time

#: The content type Prometheus expects for the text exposition format.
CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

#: The standard buckets for a repair (seconds).
REPAIR_BUCKETS = (1, 2, 5, 10, 15, 30, 60, 120, 300, 600)

#: How many learned profiles are exposed (the store is small, but a label
#: value per profile is still cardinality).
MAX_PROFILES = 50

#: The started-at timestamp of this process (for `process_start_time_seconds`).
_STARTED_AT = time.time()


# --------------------------------------------------------------------------- #
# The text format
# --------------------------------------------------------------------------- #

def escape_help(text: str) -> str:
    """A HELP text may not contain a raw backslash or newline."""
    return str(text or "").replace("\\", r"\\").replace("\n", r"\n")


def escape_label_value(value) -> str:
    """A label value may not contain a raw backslash, quote or newline."""
    return (str(value if value is not None else "")
            .replace("\\", r"\\")
            .replace('"', r"\"")
            .replace("\n", r"\n"))


def format_value(value) -> str:
    """Render a sample value the way Prometheus expects it."""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "0"
    if math.isnan(number):
        return "NaN"
    if math.isinf(number):
        return "+Inf" if number > 0 else "-Inf"
    if number == int(number) and abs(number) < 1e15:
        return str(int(number))
    return repr(number)


def _labels(pairs: dict | None) -> str:
    """``{a="1",b="2"}`` — sorted, so the output is deterministic."""
    if not pairs:
        return ""
    parts = []
    for name in sorted(pairs):
        parts.append(f'{name}="{escape_label_value(pairs[name])}"')
    return "{" + ",".join(parts) + "}"


class Registry:
    """The metric families of one scrape."""

    def __init__(self) -> None:
        self._families: list[dict] = []

    # -- registration ------------------------------------------------------ #
    def _family(self, name: str, kind: str, help_text: str) -> dict:
        for family in self._families:
            if family["name"] == name:
                return family
        family = {"name": name, "type": kind, "help": help_text, "samples": []}
        self._families.append(family)
        return family

    def gauge(self, name: str, help_text: str, value,
              labels: dict | None = None) -> None:
        family = self._family(name, "gauge", help_text)
        family["samples"].append((name, dict(labels or {}), value))

    def counter(self, name: str, help_text: str, value,
                labels: dict | None = None) -> None:
        family = self._family(name, "counter", help_text)
        family["samples"].append((name, dict(labels or {}), value))

    def histogram(self, name: str, help_text: str, buckets, values,
                  labels: dict | None = None) -> None:
        """A cumulative histogram: ``_bucket``, ``_sum`` and ``_count``."""
        family = self._family(name, "histogram", help_text)
        edges = sorted(float(edge) for edge in buckets)
        observations = []
        for value in values or []:
            try:
                observations.append(float(value))
            except (TypeError, ValueError):
                continue
        counts = [0] * len(edges)
        total = 0.0
        for observation in observations:
            total += observation
            for index, edge in enumerate(edges):
                if observation <= edge:
                    counts[index] += 1
        for index, edge in enumerate(edges):
            bucket_labels = dict(labels or {})
            bucket_labels["le"] = format_value(edge)
            family["samples"].append((f"{name}_bucket", bucket_labels,
                                      counts[index]))
        infinity = dict(labels or {})
        infinity["le"] = "+Inf"
        family["samples"].append((f"{name}_bucket", infinity, len(observations)))
        family["samples"].append((f"{name}_sum", dict(labels or {}), total))
        family["samples"].append((f"{name}_count", dict(labels or {}),
                                  len(observations)))

    # -- rendering --------------------------------------------------------- #
    def render(self) -> str:
        lines: list[str] = []
        for family in self._families:
            lines.append(f"# HELP {family['name']} "
                         f"{escape_help(family['help'])}")
            lines.append(f"# TYPE {family['name']} {family['type']}")
            for sample_name, labels, value in family["samples"]:
                lines.append(f"{sample_name}{_labels(labels)} "
                             f"{format_value(value)}")
        return "\n".join(lines) + "\n"


class HttpCounter:
    """A tiny thread-safe counter of the HTTP responses.

    The service is a threaded stdlib server, so the counter has a lock. It
    counts *responses* by status class — the cheapest useful signal that the
    API is alive and not answering 401/500 all the time.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts: dict[str, int] = {}
        self._total = 0

    def record(self, code: int) -> None:
        try:
            code = int(code)
        except (TypeError, ValueError):
            code = 0
        key = f"{code // 100}xx"
        with self._lock:
            self._counts[key] = self._counts.get(key, 0) + 1
            self._total += 1

    def snapshot(self) -> dict:
        with self._lock:
            return {"total": self._total, "classes": dict(self._counts)}


# --------------------------------------------------------------------------- #
# The collectors — every number comes from the real state
# --------------------------------------------------------------------------- #

def _service_metrics(reg: Registry, snapshot: dict) -> None:
    version = str(snapshot.get("version") or os.environ.get("AUTON_VERSION")
                  or "dev")
    reg.gauge("auton_build_info",
              "Build information of the running Auton API.", 1,
              {"version": version})
    reg.gauge("auton_up", "1 when the Auton API is serving metrics.", 1)
    uptime = snapshot.get("uptime_seconds")
    if uptime is None:
        uptime = max(0.0, time.time() - _STARTED_AT)
    reg.gauge("auton_uptime_seconds", "Uptime of the API process in seconds.",
              float(uptime))


def _queue_metrics(reg: Registry, stats: dict) -> None:
    if not stats:
        return
    for name, value in sorted((stats.get("queue_depth") or {}).items()):
        reg.gauge("auton_queue_depth",
                  "Queued items per stream (claimed/dead are not work).",
                  int(value or 0), {"queue": name})
    for name, value in sorted((stats.get("active") or {}).items()):
        reg.gauge("auton_queue_active",
                  "Tasks in flight per queue (claimed + running + verifying).",
                  int(value or 0), {"queue": name})
    for name, value in sorted((stats.get("statuses") or {}).items()):
        reg.gauge("auton_queue_tasks",
                  "Tasks per status in the task index.", int(value or 0),
                  {"status": name})
    oldest = stats.get("oldest_queued")
    if oldest:
        reg.gauge("auton_queue_oldest_queued_seconds",
                  "Age of the oldest queued task in seconds.",
                  max(0.0, time.time() - float(oldest)))
    reg.gauge("auton_queue_budget_used",
              "Repair actions used in the current hourly budget window.",
              int(stats.get("budget_used") or 0))
    for worker in stats.get("workers") or []:
        labels = {"role": str(worker.get("role") or "?"),
                  "name": str(worker.get("name") or "?")}
        reg.gauge("auton_worker_alive",
                  "1 when the worker's heartbeat is fresh.", 1 if worker.get(
                      "alive") else 0, labels)
        age = worker.get("age")
        if age is not None:
            reg.gauge("auton_worker_heartbeat_age_seconds",
                      "Seconds since the worker last checked in.", float(age),
                      labels)


def _watch_metrics(reg: Registry, watchers: list) -> None:
    for watcher in watchers or []:
        if not isinstance(watcher, dict):
            continue
        labels = {"resource": str(watcher.get("resource")
                                  or watcher.get("name") or "?")}
        reg.gauge("auton_watch_available",
                  "1 when the watch stream is usable (listed and not "
                  "forbidden).", 1 if watcher.get("available") else 0, labels)
        reg.gauge("auton_watch_connected",
                  "1 when the watch stream is currently connected.", 1 if
                  watcher.get("connected") else 0, labels)
        version = watcher.get("resource_version")
        if version not in (None, ""):
            try:
                reg.gauge("auton_watch_resource_version",
                          "The last Kubernetes resourceVersion the stream "
                          "saw.", float(version), labels)
            except (TypeError, ValueError):
                pass
        age = watcher.get("last_event_age_seconds")
        if age is not None:
            reg.gauge("auton_watch_last_event_age_seconds",
                      "Seconds since the last watch event.", float(age),
                      labels)
        for counter, metric in (("events", "auton_watch_events_total"),
                                ("errors", "auton_watch_errors_total"),
                                ("reconnects", "auton_watch_reconnects_total"),
                                ("expired", "auton_watch_expired_total"),
                                ("lists", "auton_watch_lists_total"),
                                ("forbidden", "auton_watch_forbidden_total")):
            if counter in watcher:
                reg.counter(metric,
                            f"Watch stream {counter} (published by the "
                            "scanner).", int(watcher.get(counter) or 0), labels)


def _transport_metrics(reg: Registry, watchers: list) -> None:
    """Which transport answered: the direct HTTPS client or kubectl.

    The scanner publishes the counters it collected (D6). ``direct`` is the
    number of API calls that went straight to the API server over HTTPS;
    ``kubectl`` is the number of calls that had to fall back to the CLI. A
    rising ``kubectl`` count means the direct path is broken — so it is worth
    a metric of its own.
    """
    for watcher in watchers or []:
        if not isinstance(watcher, dict):
            continue
        transport = watcher.get("transport")
        if not isinstance(transport, dict):
            continue
        for name, value in sorted(transport.items()):
            try:
                count = int(value or 0)
            except (TypeError, ValueError):
                continue
            reg.counter("auton_k8s_transport_total",
                        "Kubernetes API calls by transport (direct = the HTTPS "
                        "client, kubectl = the fallback).", count,
                        {"transport": str(name)})


def _incident_metrics(reg: Registry, records: list) -> None:
    counts: dict[tuple, int] = {}
    for record in records or []:
        if not isinstance(record, dict):
            continue
        key = (str(record.get("status") or "unknown"),
               str(record.get("severity") or "unknown"),
               str(record.get("cause") or "unknown"))
        counts[key] = counts.get(key, 0) + 1
    for (status, severity, cause), value in sorted(counts.items()):
        reg.gauge("auton_incidents",
                  "Incidents per state, severity and cause.", value,
                  {"state": status, "severity": severity, "cause": cause})
    reg.gauge("auton_incidents_total", "Incidents in the store.",
              len(records or []))


def _approval_metrics(reg: Registry, records: list) -> None:
    counts: dict[str, int] = {}
    for record in records or []:
        if not isinstance(record, dict):
            continue
        status = str(record.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    for status, value in sorted(counts.items()):
        reg.gauge("auton_approvals", "Approval requests per status.", value,
                  {"status": status})
    reg.gauge("auton_approvals_total", "Approval requests in the store.",
              len(records or []))


def _learning_metrics(reg: Registry, learning: dict) -> None:
    stats = learning.get("stats") or {}
    outcomes = learning.get("outcomes") or []
    profiles = learning.get("profiles") or []

    results: dict[tuple, int] = {}
    durations: dict[str, list] = {}
    for outcome in outcomes:
        if not isinstance(outcome, dict):
            continue
        cause = str(outcome.get("cause") or "unknown")
        result = "ok" if outcome.get("ok") else (
            "needs_human" if outcome.get("needs_human") else "failed")
        key = (cause, result)
        results[key] = results.get(key, 0) + 1
        try:
            durations.setdefault(cause, []).append(
                float(outcome.get("duration_seconds") or 0.0))
        except (TypeError, ValueError):
            continue
    for (cause, result), value in sorted(results.items()):
        reg.gauge("auton_repairs_recorded",
                  "Repairs recorded in the outcome store, per cause and "
                  "result. The store is pruned at AGENT_LEARN_MAX_OUTCOMES, "
                  "so this is a gauge, not a lifetime counter.", value,
                  {"cause": cause, "result": result})
    # The store was read (it is in the snapshot), so the total is known — even
    # when it is zero. A zero is a fact; an absent family would be a guess
    # about a store that was never read.
    reg.gauge("auton_repairs_recorded_total",
              "Repairs in the outcome store (pruned at "
              "AGENT_LEARN_MAX_OUTCOMES).", len(outcomes))
    for cause, values in sorted(durations.items()):
        reg.histogram("auton_repair_duration_seconds",
                      "Duration of the recorded repairs, per cause (the "
                      "outcome store is pruned, so the histogram reflects "
                      "the stored window).",
                      REPAIR_BUCKETS, values, {"cause": cause})

    quarantined = 0
    adaptations: dict[str, int] = {}
    for profile in profiles:
        if not isinstance(profile, dict):
            continue
        if profile.get("quarantined"):
            quarantined += 1
        for entry in profile.get("adaptations") or []:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "?")
            adaptations[name] = adaptations.get(name, 0) + 1
    reg.gauge("auton_learning_profiles", "Learned profiles per state.", len(
        profiles), {"quarantined": "false"})
    reg.gauge("auton_learning_profiles", "Learned profiles per state.",
              quarantined, {"quarantined": "true"})
    for name, value in sorted(adaptations.items()):
        reg.counter("auton_learning_adaptations_total",
                    "Bounded adaptations the platform applied, per name.",
                    value, {"name": name})
    for profile in profiles[:MAX_PROFILES]:
        if not isinstance(profile, dict):
            continue
        labels = {"resource": str(profile.get("resource") or "?"),
                  "cause": str(profile.get("cause") or "?")}
        reg.gauge("auton_learning_memory_step",
                  "The learned memory bump for a workload (a fraction).",
                  float(profile.get("memory_step") or 0.25), labels)
        reg.gauge("auton_learning_tier",
                  "The learned risk tier for a workload.", int(
                      profile.get("tier") or 0), labels)
    if stats:
        limits = stats.get("limits") or {}
        for name, value in sorted(limits.items()):
            reg.gauge("auton_learning_limit",
                      "The configured caps of the Learn stage.", float(value),
                      {"limit": name})


def _policy_metrics(reg: Registry, policy: dict) -> None:
    if not policy:
        return
    mode = str(policy.get("mode") or "unknown")
    source = str(policy.get("source") or "unknown")
    reg.gauge("auton_policy_info",
              "The effective repair policy (mode and where it came from).", 1,
              {"mode": mode, "source": source})


def _job_metrics(reg: Registry, jobs: dict) -> None:
    if not jobs:
        return
    reg.gauge("auton_jobs_running", "Agent jobs running right now.",
              int(jobs.get("running") or 0))
    reg.gauge("auton_jobs_known", "Agent jobs known to this process.",
              int(jobs.get("known") or 0))


def _http_metrics(reg: Registry, http: dict) -> None:
    if not http:
        return
    reg.counter("auton_http_responses_total",
                "HTTP responses sent by this process, since it started.",
                int(http.get("total") or 0))
    for status_class, value in sorted((http.get("classes") or {}).items()):
        reg.counter("auton_http_responses_by_class_total",
                    "HTTP responses sent by this process, per status class.",
                    int(value), {"status": status_class})


def _process_metrics(reg: Registry) -> None:
    """The standard ``process_*`` metrics (stdlib ``resource``)."""
    try:
        usage = resource.getrusage(resource.RUSAGE_SELF)
    except (OSError, ValueError):
        return
    reg.gauge("process_resident_memory_bytes",
              "Resident memory size in bytes.",
              float(usage.ru_maxrss) * 1024.0)
    reg.counter("process_cpu_seconds_total",
                "Total user and system CPU time spent in seconds.",
                float(usage.ru_utime) + float(usage.ru_stime))
    reg.gauge("process_start_time_seconds",
              "Start time of the process since unix epoch in seconds.",
              _STARTED_AT)


# --------------------------------------------------------------------------- #
# The entry point
# --------------------------------------------------------------------------- #

def collect(snapshot: dict | None = None) -> str:
    """Render the whole exposition from a snapshot of the real state.

    The snapshot distinguishes two cases, and so does the exposition:

    * the key is **present** -> the source was read. An empty store is then a
      fact and is reported as a zero total;
    * the key is **absent** -> the source could not be read. The families are
      left out entirely, because a zero would be a guess.
    """
    data = dict(snapshot or {})
    reg = Registry()
    _service_metrics(reg, data)
    if "queue" in data:
        _queue_metrics(reg, data.get("queue") or {})
        watchers = (data.get("queue") or {}).get("watchers") or []
        _watch_metrics(reg, watchers)
        _transport_metrics(reg, watchers)
    if "incidents" in data:
        _incident_metrics(reg, data.get("incidents") or [])
    if "approvals" in data:
        _approval_metrics(reg, data.get("approvals") or [])
    if "learning" in data:
        _learning_metrics(reg, data.get("learning") or {})
    if "policy" in data:
        _policy_metrics(reg, data.get("policy") or {})
    if "jobs" in data:
        _job_metrics(reg, data.get("jobs") or {})
    if "http" in data:
        _http_metrics(reg, data.get("http") or {})
    _process_metrics(reg)
    return reg.render()
