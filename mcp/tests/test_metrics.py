"""Tests for the Prometheus exposition (stdlib only).

The exposition is hand-written, so the tests are strict about the format: a
parser that mimics Prometheus' own grammar checks every line, every family
and every histogram. A metric that Prometheus cannot parse is worse than no
metric at all.
"""
from __future__ import annotations

import json
import math
import re
import threading
import urllib.error
import urllib.request

import pytest

import metrics


# --------------------------------------------------------------------------- #
# A strict parser (the Prometheus text format)
# --------------------------------------------------------------------------- #

NAME = r"[a-zA-Z_:][a-zA-Z0-9_:]*"
LABEL_NAME = r"[a-zA-Z_][a-zA-Z0-9_]*"
SAMPLE_RE = re.compile(
    rf"^(?P<name>{NAME})(?:\{{(?P<labels>.*)\}})?\s+"
    r"(?P<value>[^ ]+)(?:\s+(?P<ts>[0-9]+))?$")
LABEL_RE = re.compile(rf'^(?P<label>{LABEL_NAME})="(?P<value>(?:[^"\\]|\\.)*)"$')


def parse_exposition(text: str) -> dict:
    """Parse the exposition; raise AssertionError on anything Prometheus
    would reject."""
    families: dict[str, dict] = {}
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip("\n")
        if not line:
            continue
        if line.startswith("# HELP "):
            parts = line[len("# HELP "):].split(" ", 1)
            assert len(parts) == 2, f"line {number}: bad HELP: {line!r}"
            name = parts[0]
            assert re.fullmatch(NAME, name), f"line {number}: bad name {name!r}"
            family = families.setdefault(name, {"type": None, "help": None,
                                                "samples": []})
            assert family["help"] is None, f"line {number}: duplicate HELP"
            family["help"] = parts[1]
            continue
        if line.startswith("# TYPE "):
            parts = line[len("# TYPE "):].split(" ", 1)
            assert len(parts) == 2, f"line {number}: bad TYPE: {line!r}"
            name, kind = parts
            assert re.fullmatch(NAME, name), f"line {number}: bad name {name!r}"
            assert kind in ("gauge", "counter", "histogram", "untyped"), \
                f"line {number}: unknown type {kind!r}"
            family = families.setdefault(name, {"type": None, "help": None,
                                                "samples": []})
            assert family["type"] is None, f"line {number}: duplicate TYPE"
            family["type"] = kind
            continue
        assert not line.startswith("#"), f"line {number}: unknown comment"
        match = SAMPLE_RE.match(line)
        assert match, f"line {number}: not a valid sample: {line!r}"
        name = match.group("name")
        value = match.group("value")
        assert value in ("NaN", "+Inf", "-Inf") or re.fullmatch(
            r"-?(?:[0-9]+(?:\.[0-9]+)?(?:[eE][-+]?[0-9]+)?)", value), \
            f"line {number}: bad value {value!r}"
        labels = {}
        raw_labels = match.group("labels")
        if raw_labels is not None:
            for part in raw_labels.split(","):
                part = part.strip()
                if not part:
                    continue
                label = LABEL_RE.match(part)
                assert label, f"line {number}: bad label {part!r}"
                labels[label.group("label")] = label.group("value")
        # A histogram's samples are named <base>_bucket/_sum/_count and belong
        # to the base family (this is how Prometheus' own parser reads them).
        base = name
        for suffix in ("_bucket", "_sum", "_count"):
            if name.endswith(suffix):
                candidate = name[:-len(suffix)]
                if families.get(candidate, {}).get("type") == "histogram":
                    base = candidate
                    break
        family = families.setdefault(base, {"type": None, "help": None,
                                            "samples": []})
        family["samples"].append((name, labels, value))
    return families


def values(families: dict, family_name: str, sample_name: str = "") -> list:
    """The (labels, value) pairs of a family (optionally of one sample name)."""
    family = families.get(family_name) or {}
    out = []
    for name, labels, value in family.get("samples", []):
        if sample_name and name != sample_name:
            continue
        out.append((labels, value))
    return out


def assert_valid(text: str) -> dict:
    families = parse_exposition(text)
    assert families, "the exposition must not be empty"
    for name, family in families.items():
        assert family["type"] is not None, f"{name}: a sample without a TYPE"
        assert family["help"] is not None, f"{name}: a sample without a HELP"
    # A histogram needs buckets, a sum and a count.
    for name, family in families.items():
        if family["type"] != "histogram":
            continue
        names = {sample for sample, _, _ in family["samples"]}
        assert f"{name}_bucket" in names
        assert f"{name}_sum" in names
        assert f"{name}_count" in names
        edges = [labels.get("le") for sample, labels, _ in family["samples"]
                 if sample == f"{name}_bucket"]
        assert "+Inf" in edges, f"{name}: the +Inf bucket is mandatory"
        finite = [float(edge) for edge in edges if edge != "+Inf"]
        assert finite == sorted(finite), f"{name}: the buckets are not sorted"
    return families


# --------------------------------------------------------------------------- #
# The format
# --------------------------------------------------------------------------- #

def test_an_empty_registry_renders_nothing():
    assert metrics.Registry().render() == "\n"


def test_a_gauge_renders_help_type_and_the_sample():
    reg = metrics.Registry()
    reg.gauge("auton_test", "A test gauge.", 3)
    text = reg.render()
    assert "# HELP auton_test A test gauge." in text
    assert "# TYPE auton_test gauge" in text
    assert "auton_test 3" in text
    assert_valid(text)


def test_labels_are_sorted_and_escaped():
    reg = metrics.Registry()
    reg.gauge("auton_test", "help", 1, {"b": 'say "hi"', "a": "back\\slash",
                                        "c": "two\nlines"})
    text = reg.render()
    assert 'auton_test{a="back\\\\slash",b="say \\"hi\\"",c="two\\nlines"} 1' \
        in text
    assert_valid(text)


def test_the_help_text_escapes_backslashes_and_newlines():
    reg = metrics.Registry()
    reg.gauge("auton_test", "a\\b\nc", 1)
    text = reg.render()
    assert "# HELP auton_test a\\\\b\\nc" in text
    assert_valid(text)


def test_values_are_rendered_the_way_prometheus_expects():
    reg = metrics.Registry()
    reg.gauge("auton_int", "h", 7)
    reg.gauge("auton_float", "h", 1.5)
    reg.gauge("auton_bool", "h", True)
    reg.gauge("auton_nan", "h", float("nan"))
    reg.gauge("auton_inf", "h", float("inf"))
    reg.gauge("auton_ninf", "h", float("-inf"))
    text = reg.render()
    assert "auton_int 7" in text
    assert "auton_float 1.5" in text
    assert "auton_bool 1" in text
    assert "auton_nan NaN" in text
    assert "auton_inf +Inf" in text
    assert "auton_ninf -Inf" in text
    assert_valid(text)


def test_a_histogram_is_cumulative_and_complete():
    reg = metrics.Registry()
    reg.histogram("auton_seconds", "help", (1, 5, 10), [0.5, 2, 7],
                  {"cause": "oom"})
    text = reg.render()
    families = assert_valid(text)
    buckets = values(families, "auton_seconds", "auton_seconds_bucket")
    assert ({"cause": "oom", "le": "1"}, "1") in buckets
    assert ({"cause": "oom", "le": "5"}, "2") in buckets
    assert ({"cause": "oom", "le": "10"}, "3") in buckets
    assert ({"cause": "oom", "le": "+Inf"}, "3") in buckets
    assert ({"cause": "oom"}, "9.5") in \
        values(families, "auton_seconds", "auton_seconds_sum")
    assert ({"cause": "oom"}, "3") in \
        values(families, "auton_seconds", "auton_seconds_count")
    numbers = [int(value) for labels, value in buckets]
    assert numbers == sorted(numbers)


def test_a_histogram_ignores_values_it_cannot_read():
    reg = metrics.Registry()
    reg.histogram("auton_seconds", "help", (1, 5), [1, "nonsense", 2])
    text = reg.render()
    families = assert_valid(text)
    counts = [value for labels, value in
              values(families, "auton_seconds", "auton_seconds_bucket")
              if labels.get("le") == "+Inf"]
    assert counts == ["2"]


# --------------------------------------------------------------------------- #
# The collectors read the real state
# --------------------------------------------------------------------------- #

def _snapshot() -> dict:
    return {
        "version": "0.4.0",
        "uptime_seconds": 12.5,
        "jobs": {"running": 1, "known": 7},
        "http": {"total": 42, "classes": {"2xx": 40, "4xx": 2}},
        "queue": {
            "statuses": {"queued": 3, "resolved": 10},
            "queue_depth": {"repair_critical": 1, "repair_normal": 2,
                            "agent": 0, "delayed": 0, "dead": 0, "claimed": 1},
            "active": {"repair": 1, "agent": 0},
            "oldest_queued": 1000.0,
            "budget_used": 2,
            "workers": [{"role": "worker", "name": "worker-1", "alive": True,
                         "age": 3.2},
                        {"role": "scanner", "name": "scanner-1", "alive": False,
                         "age": 900.0}],
            "watchers": [{"resource": "pods", "available": True,
                          "connected": True, "resource_version": "4711",
                          "last_event_age_seconds": 4.0, "events": 12,
                          "errors": 0, "reconnects": 1},
                         {"resource": "summary", "available": True,
                          "transport": {"direct": 42, "direct_error": 0,
                                        "kubectl": 1, "unavailable": 0}}],
        },
        "incidents": [
            {"status": "resolved", "severity": "critical", "cause": "oom"},
            {"status": "resolved", "severity": "critical", "cause": "oom"},
            {"status": "needs_human", "severity": "warning", "cause": "image"},
        ],
        "approvals": [{"status": "pending"}, {"status": "executed"}],
        "learning": {
            "stats": {"limits": {"min_samples": 3, "max_step": 1.0}},
            "outcomes": [{"cause": "oom", "ok": True, "duration_seconds": 2.0},
                         {"cause": "oom", "ok": False, "needs_human": True,
                          "duration_seconds": 4.0}],
            "profiles": [{"resource": "deployment/api", "cause": "oom",
                          "memory_step": 0.5, "tier": 1,
                          "quarantined": False,
                          "adaptations": [{"name": "raise_oom_step"}]}],
        },
        "policy": {"mode": "safe-auto", "source": "configmap"},
    }


def test_the_exposition_covers_the_whole_platform():
    families = assert_valid(metrics.collect(_snapshot()))
    names = set(families)
    for expected in ("auton_up", "auton_build_info", "auton_queue_depth",
                     "auton_queue_tasks", "auton_queue_budget_used",
                     "auton_watch_available", "auton_incidents",
                     "auton_repairs_recorded", "auton_repair_duration_seconds",
                     "auton_learning_profiles", "auton_learning_adaptations_total",
                     "auton_approvals", "auton_policy_info", "auton_jobs_running",
                     "auton_http_responses_total", "process_resident_memory_bytes",
                     "process_cpu_seconds_total", "process_start_time_seconds"):
        assert expected in names, f"{expected} is missing from the exposition"


def test_the_queue_numbers_are_the_real_ones():
    families = assert_valid(metrics.collect(_snapshot()))
    samples = values(families, "auton_queue_depth")
    assert ({"queue": "repair_critical"}, "1") in samples
    assert ({"queue": "repair_normal"}, "2") in samples
    tasks = values(families, "auton_queue_tasks")
    assert ({"status": "queued"}, "3") in tasks
    assert ({"status": "resolved"}, "10") in tasks
    assert ({"role": "worker", "name": "worker-1"}, "1") in \
        values(families, "auton_worker_alive")
    assert ({"role": "scanner", "name": "scanner-1"}, "0") in \
        values(families, "auton_worker_alive")


def test_the_watcher_health_is_exposed():
    families = assert_valid(metrics.collect(_snapshot()))
    assert ({"resource": "pods"}, "1") in \
        values(families, "auton_watch_available")
    assert ({"resource": "pods"}, "12") in \
        values(families, "auton_watch_events_total")
    assert ({"resource": "pods"}, "1") in \
        values(families, "auton_watch_reconnects_total")


def test_the_transport_counters_are_exposed():
    # D6: the proof that the platform talks to the API server directly.
    families = assert_valid(metrics.collect(_snapshot()))
    samples = values(families, "auton_k8s_transport_total")
    assert ({"transport": "direct"}, "42") in samples
    assert ({"transport": "kubectl"}, "1") in samples


def test_a_missing_transport_is_absent_and_not_guessed():
    snapshot = _snapshot()
    snapshot["queue"]["watchers"] = [{"resource": "pods", "available": True}]
    families = assert_valid(metrics.collect(snapshot))
    assert "auton_k8s_transport_total" not in families


def test_the_incidents_are_counted_per_state():
    families = assert_valid(metrics.collect(_snapshot()))
    samples = values(families, "auton_incidents")
    assert ({"state": "resolved", "severity": "critical", "cause": "oom"},
            "2") in samples
    assert ({"state": "needs_human", "severity": "warning", "cause": "image"},
            "1") in samples


def test_the_repairs_are_counted_per_result():
    families = assert_valid(metrics.collect(_snapshot()))
    samples = values(families, "auton_repairs_recorded")
    assert ({"cause": "oom", "result": "ok"}, "1") in samples
    assert ({"cause": "oom", "result": "needs_human"}, "1") in samples


def test_the_learned_values_are_exposed():
    families = assert_valid(metrics.collect(_snapshot()))
    assert ({"resource": "deployment/api", "cause": "oom"}, "0.5") in \
        values(families, "auton_learning_memory_step")
    assert ({"resource": "deployment/api", "cause": "oom"}, "1") in \
        values(families, "auton_learning_tier")
    assert ({"name": "raise_oom_step"}, "1") in \
        values(families, "auton_learning_adaptations_total")


def test_a_missing_store_is_absent_and_not_guessed():
    text = metrics.collect({"version": "0.4.0"})
    families = assert_valid(text)
    # No queue, no incidents, no learning: the metrics are simply not there.
    assert "auton_queue_depth" not in families
    assert "auton_incidents" not in families
    assert "auton_repairs_recorded" not in families
    assert "auton_repairs_recorded_total" not in families
    # ... but the service itself is still reported.
    assert "auton_up" in families


def test_an_empty_but_readable_store_is_zero_and_not_guessed():
    snapshot = _snapshot()
    snapshot["learning"]["outcomes"] = []
    snapshot["incidents"] = []
    snapshot["approvals"] = []
    families = assert_valid(metrics.collect(snapshot))
    # The stores were read, so the totals are known zeros...
    assert (None, "0") in [(labels or None, value) for labels, value in
                           values(families, "auton_repairs_recorded_total")]
    assert (None, "0") in [(labels or None, value) for labels, value in
                           values(families, "auton_incidents_total")]
    assert (None, "0") in [(labels or None, value) for labels, value in
                           values(families, "auton_approvals_total")]
    # ... and there is no per-cause family to report.
    assert "auton_repairs_recorded" not in families
    assert "auton_incidents" not in families
    assert "auton_approvals" not in families


def test_the_policy_mode_is_a_label_not_a_value():
    families = assert_valid(metrics.collect(_snapshot()))
    assert ({"mode": "safe-auto", "source": "configmap"}, "1") in \
        values(families, "auton_policy_info")


def test_the_exposition_never_carries_a_secret():
    text = metrics.collect(_snapshot())
    for forbidden in ("token", "password", "api_key", "apikey", "secret",
                      "Authorization", "Bearer"):
        assert forbidden.lower() not in text.lower(), \
            f"the exposition must not mention {forbidden}"


def test_the_cardinality_of_the_profiles_is_bounded():
    snapshot = _snapshot()
    snapshot["learning"]["profiles"] = [
        {"resource": f"deployment/app-{index}", "cause": "oom",
         "memory_step": 0.25, "tier": 0, "quarantined": False,
         "adaptations": []}
        for index in range(200)]
    families = assert_valid(metrics.collect(snapshot))
    assert len(values(families, "auton_learning_memory_step")) == \
        metrics.MAX_PROFILES


# --------------------------------------------------------------------------- #
# The HTTP counter
# --------------------------------------------------------------------------- #

def test_the_http_counter_counts_by_status_class():
    counter = metrics.HttpCounter()
    counter.record(200)
    counter.record(201)
    counter.record(404)
    counter.record(500)
    snapshot = counter.snapshot()
    assert snapshot["total"] == 4
    assert snapshot["classes"] == {"2xx": 2, "4xx": 1, "5xx": 1}


def test_the_http_counter_is_thread_safe():
    counter = metrics.HttpCounter()

    def hammer():
        for _ in range(200):
            counter.record(200)

    threads = [threading.Thread(target=hammer) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert counter.snapshot()["total"] == 800


# --------------------------------------------------------------------------- #
# The endpoint
# --------------------------------------------------------------------------- #

def _request(url: str, headers: dict | None = None, timeout: float = 15):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, response.headers.get("Content-Type", ""), \
                response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Content-Type", ""), \
            exc.read().decode("utf-8")


@pytest.fixture()
def server(monkeypatch, tmp_path):
    """The HTTP service with isolated stores."""
    import agent_service
    import approvals
    import incidents

    monkeypatch.setattr(agent_service, "INCIDENT_STORE",
                        incidents.IncidentStore(str(tmp_path / "incidents")))
    monkeypatch.setattr(agent_service, "APPROVAL_STORE",
                        approvals.ApprovalStore(str(tmp_path / "approvals")))
    httpd = agent_service.ThreadingHTTPServer(("127.0.0.1", 0),
                                               agent_service.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def test_the_metrics_endpoint_serves_the_exposition(server):
    status, content_type, body = _request(f"{server}/metrics")
    assert status == 200
    assert "text/plain" in content_type
    assert "version=0.0.4" in content_type
    families = assert_valid(body)
    assert "auton_up" in families
    assert "auton_build_info" in families


def test_the_metrics_endpoint_needs_the_token(server, monkeypatch):
    import agent_service

    monkeypatch.setattr(agent_service, "AUTH_DISABLED", False)
    monkeypatch.setattr(agent_service, "API_TOKEN", "s3cret")
    status, _, _ = _request(f"{server}/metrics")
    assert status == 401
    status, _, _ = _request(f"{server}/metrics",
                            {"Authorization": "Bearer s3cret"})
    assert status == 200


def test_the_metrics_endpoint_survives_a_broken_store(server, monkeypatch):
    import agent_service

    class Broken:
        def list(self, *args, **kwargs):
            raise OSError("the volume is gone")

        def stats(self):
            raise OSError("the volume is gone")

    monkeypatch.setattr(agent_service, "INCIDENT_STORE", Broken())
    status, _, body = _request(f"{server}/metrics")
    assert status == 200
    assert_valid(body)
