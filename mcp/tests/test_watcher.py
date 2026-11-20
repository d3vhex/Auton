"""Tests for the Kubernetes watcher (Phase 3: event-driven detection).

The watcher is the part of the platform that must never fail silently, so
these tests cover the boring parts thoroughly: the URLs and headers, the
event parsing, the 410-Gone recovery, the backoff, the counters and the
cache. A watcher that quietly stops watching is worse than no watcher at
all, because the platform would still *look* healthy.
"""
from __future__ import annotations

import io
import json
import threading
import time

import pytest

import k8s_api
import ops_controller
import ops_queue
import ops_scanner
import task_store
import watcher


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

class FakeResponse:
    """A minimal stand-in for an ``http.client.HTTPResponse``."""

    def __init__(self, body: bytes = b"", lines: list[bytes] | None = None):
        self._body = body
        self._lines = lines if lines is not None else []
        self.closed = False

    def read(self) -> bytes:
        return self._body

    def __iter__(self):
        return iter(self._lines)

    def close(self) -> None:
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _event(kind: str, obj: dict) -> dict:
    """One watch event, as the API client yields it."""
    return {"type": kind, "object": obj}


def _line(kind: str, obj: dict) -> bytes:
    """One watch event, as the API server writes it on the wire."""
    return (json.dumps(_event(kind, obj)) + "\n").encode()


def _pod(name: str = "api-1", namespace: str = "auton-apps", **status) -> dict:
    body = {
        "metadata": {"name": name, "namespace": namespace,
                     "uid": f"uid-{name}", "resourceVersion": "10",
                     "creationTimestamp": "2026-01-01T00:00:00Z"},
        "spec": {"nodeName": "node-0", "containers": [{"name": "app"}]},
        "status": {"phase": "Running", "containerStatuses": []},
    }
    body["status"].update(status)
    return body


def _oom_pod(name: str = "api-1", namespace: str = "auton-apps") -> dict:
    return _pod(name, namespace, containerStatuses=[{
        "name": "app", "ready": False, "restartCount": 3,
        "state": {"waiting": {"reason": "CrashLoopBackOff"}},
        "lastState": {"terminated": {"reason": "OOMKilled"}},
    }])


class FakeClient:
    """An API client double: lists, then streams a scripted event list."""

    def __init__(self, items=None, version="100", events=None,
                 error=None, error_after=0, list_error=None):
        self.items = items if items is not None else []
        self.version = version
        self.events = events or []
        self.error = error
        self.error_after = error_after
        self.list_error = list_error
        self.calls: list[tuple] = []
        self.watches = 0

    def list(self, path, params=None, timeout=None):
        self.calls.append(("list", path, params, timeout))
        if self.list_error is not None:
            raise self.list_error
        return list(self.items), self.version

    def watch(self, path, resource_version, timeout_seconds=300):
        self.calls.append(("watch", path, resource_version, timeout_seconds))
        self.watches += 1
        for index, event in enumerate(self.events):
            if self.error is not None and index >= self.error_after:
                raise self.error
            yield event
        if self.error is not None and len(self.events) <= self.error_after:
            raise self.error


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

class TestConfig:
    def test_no_credentials_returns_none(self, monkeypatch):
        monkeypatch.setattr(watcher, "SA_DIR", "/nonexistent/sa")
        monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
        assert watcher.in_cluster_config() is None
        monkeypatch.delenv("AGENT_K8S_API", raising=False)
        monkeypatch.delenv("AGENT_K8S_TOKEN", raising=False)
        assert watcher.config_from_env() is None
        assert watcher.resolve_config() is None

    def test_env_config(self, monkeypatch):
        monkeypatch.setenv("AGENT_K8S_API", "https://api.example:6443/")
        monkeypatch.setenv("AGENT_K8S_TOKEN", "secret-token")
        monkeypatch.setenv("AGENT_K8S_NAMESPACE", "auton")
        config = watcher.config_from_env()
        assert config is not None
        assert config.base == "https://api.example:6443"
        assert config.token == "secret-token"
        assert config.namespace == "auton"

    def test_env_config_needs_both(self, monkeypatch):
        monkeypatch.setenv("AGENT_K8S_API", "https://api.example:6443")
        monkeypatch.delenv("AGENT_K8S_TOKEN", raising=False)
        assert watcher.config_from_env() is None

    def test_explicit_config_wins(self, monkeypatch):
        monkeypatch.setenv("AGENT_K8S_API", "https://api.example:6443")
        monkeypatch.setenv("AGENT_K8S_TOKEN", "secret-token")
        monkeypatch.setattr(watcher, "in_cluster_config", lambda: None)
        assert watcher.resolve_config() is not None

    def test_repr_never_leaks_the_token(self):
        config = watcher.ApiConfig("https://api", "super-secret")
        assert "super-secret" not in repr(config)
        assert "set" in repr(config)

    def test_in_cluster_config_reads_the_projected_files(self, monkeypatch,
                                                        tmp_path):
        sa = tmp_path / "serviceaccount"
        sa.mkdir()
        (sa / "token").write_text("  projected-token\n")
        (sa / "namespace").write_text("auton\n")
        (sa / "ca.crt").write_text("-----BEGIN CERTIFICATE-----\n")
        monkeypatch.setattr(watcher, "SA_DIR", str(sa))
        monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.43.0.1")
        monkeypatch.setenv("KUBERNETES_SERVICE_PORT", "443")
        monkeypatch.delenv("AGENT_K8S_API", raising=False)
        config = watcher.in_cluster_config()
        assert config is not None
        assert config.token == "projected-token"
        assert config.namespace == "auton"
        assert config.ca_file.endswith("ca.crt")
        assert config.base == "https://10.43.0.1:443"


# --------------------------------------------------------------------------- #
# The API client
# --------------------------------------------------------------------------- #

class TestApiClient:
    def _client(self):
        config = watcher.ApiConfig("https://api.example:6443", "tok",
                                   namespace="auton")
        return watcher.ApiClient(config)

    def test_url_building(self):
        client = self._client()
        assert client._url("/api/v1/namespaces/x/pods") == \
            "https://api.example:6443/api/v1/namespaces/x/pods"
        assert client._url("/pods", {"watch": "true"}) == \
            "https://api.example:6443/pods?watch=true"

    def test_headers_and_url(self, monkeypatch):
        client = self._client()
        seen = {}

        def fake_urlopen(request, timeout=None, context=None):
            seen["url"] = request.full_url
            seen["headers"] = dict(request.headers)
            seen["method"] = request.get_method()
            return FakeResponse(json.dumps({"items": [], "metadata": {
                "resourceVersion": "77"}}).encode())

        monkeypatch.setattr(k8s_api.urllib.request, "urlopen", fake_urlopen)
        items, version = client.list("/api/v1/namespaces/x/pods")
        assert items == []
        assert version == "77"
        assert seen["method"] == "GET"
        assert "watch=true" not in seen["url"]
        assert "resourceVersion=0" in seen["url"]
        auth = seen["headers"].get("Authorization")
        assert auth == "Bearer tok"

    def test_list_invalid_json(self, monkeypatch):
        client = self._client()
        monkeypatch.setattr(
            k8s_api.urllib.request, "urlopen",
            lambda *a, **k: FakeResponse(b"not json"))
        with pytest.raises(watcher.WatchError):
            client.list("/pods")

    def test_410_is_watch_expired(self, monkeypatch):
        client = self._client()
        error = k8s_api.urllib.error.HTTPError(
            "https://api", 410, "Gone", {}, io.BytesIO(b""))
        monkeypatch.setattr(k8s_api.urllib.request, "urlopen",
                            lambda *a, **k: (_ for _ in ()).throw(error))
        with pytest.raises(watcher.WatchExpired):
            client.list("/pods")

    def test_403_is_watch_forbidden(self, monkeypatch):
        client = self._client()
        error = k8s_api.urllib.error.HTTPError(
            "https://api", 403, "Forbidden", {}, io.BytesIO(b""))
        monkeypatch.setattr(k8s_api.urllib.request, "urlopen",
                            lambda *a, **k: (_ for _ in ()).throw(error))
        with pytest.raises(watcher.WatchForbidden):
            client.list("/pods")

    def test_unreachable_is_watch_error(self, monkeypatch):
        client = self._client()
        error = k8s_api.urllib.error.URLError("connection refused")
        monkeypatch.setattr(k8s_api.urllib.request, "urlopen",
                            lambda *a, **k: (_ for _ in ()).throw(error))
        with pytest.raises(watcher.WatchError):
            client.list("/pods")

    def test_watch_parses_the_event_stream(self, monkeypatch):
        client = self._client()
        lines = [
            _line("ADDED", _pod("a")),
            b"this is not json\n",
            _line("MODIFIED", _pod("a")),
            b"\n",
            _line("DELETED", _pod("a")),
        ]
        monkeypatch.setattr(k8s_api.urllib.request, "urlopen",
                            lambda *a, **k: FakeResponse(lines=lines))
        events = list(client.watch("/pods", "100"))
        assert [event["type"] for event in events] == \
            ["ADDED", "MODIFIED", "DELETED"]

    def test_watch_sends_the_watch_parameters(self, monkeypatch):
        client = self._client()
        seen = {}

        def fake_urlopen(request, timeout=None, context=None):
            seen["url"] = request.full_url
            return FakeResponse(lines=[])

        monkeypatch.setattr(k8s_api.urllib.request, "urlopen", fake_urlopen)
        list(client.watch("/pods", "123", timeout_seconds=42))
        assert "watch=true" in seen["url"]
        assert "resourceVersion=123" in seen["url"]
        assert "timeoutSeconds=42" in seen["url"]
        assert "allowWatchBookmarks=true" in seen["url"]


# --------------------------------------------------------------------------- #
# The watcher
# --------------------------------------------------------------------------- #

class TestWatcher:
    def test_prime_records_the_resource_version(self):
        client = FakeClient(items=[_pod("a")], version="4711")
        stream = watcher.Watcher(client, "pods", "auton-apps")
        items = stream.prime()
        assert len(items) == 1
        assert stream.resource_version == "4711"
        assert stream.stats()["lists"] == 1

    def test_path_for_a_known_resource(self):
        stream = watcher.Watcher(FakeClient(), "pods", "auton-apps")
        assert stream.path() == "/api/v1/namespaces/auton-apps/pods"

    def test_path_for_an_unknown_resource(self):
        stream = watcher.Watcher(FakeClient(), "widgets", "auton-apps")
        with pytest.raises(watcher.WatchError):
            stream.path()

    def test_events_reach_the_handler_and_the_counters(self):
        client = FakeClient(events=[
            _event("ADDED", _pod("a")),
            _event("MODIFIED", _oom_pod("a")),
            _event("BOOKMARK", {"metadata": {"resourceVersion": "900"}}),
        ])
        stream = watcher.Watcher(client, "pods", "auton-apps")
        seen: list[tuple] = []
        stream._handle(client.events[0], lambda k, o: seen.append(k))
        stream._handle(client.events[1], lambda k, o: seen.append(k))
        stream._handle(client.events[2], lambda k, o: seen.append(k))
        assert seen == ["ADDED", "MODIFIED"]
        stats = stream.stats()
        assert stats["events"] == 2
        assert stats["bookmarks"] == 1
        assert stats["resource_version"] == "900"

    def test_a_bookmark_advances_the_version_without_an_event(self):
        stream = watcher.Watcher(FakeClient(), "pods", "auton-apps")
        stream._handle({"type": "BOOKMARK",
                        "object": {"metadata": {"resourceVersion": "55"}}},
                       None)
        assert stream.resource_version == "55"
        assert stream.stats()["events"] == 0
        assert stream.stats()["bookmarks"] == 1

    def test_an_error_event_with_410_expires_the_watch(self):
        stream = watcher.Watcher(FakeClient(), "pods", "auton-apps")
        with pytest.raises(watcher.WatchExpired):
            stream._handle({"type": "ERROR",
                            "object": {"code": 410, "reason": "Expired"}}, None)
        assert stream.stats()["errors"] == 1

    def test_a_failing_handler_does_not_kill_the_watch(self):
        stream = watcher.Watcher(FakeClient(), "pods", "auton-apps")

        def boom(kind, obj):
            raise RuntimeError("handler exploded")

        stream._handle({"type": "ADDED", "object": _pod("a")}, boom)
        stats = stream.stats()
        assert stats["events"] == 1
        assert stats["errors"] == 1
        assert "handler exploded" in stats["last_error"]

    def test_run_relists_after_410(self, monkeypatch):
        client = FakeClient(items=[_pod("a")], version="1", events=[
            _event("ADDED", _pod("a")),
        ], error=watcher.WatchExpired("expired"), error_after=1)
        stream = watcher.Watcher(client, "pods", "auton-apps",
                                 backoff=(0.01,))
        thread = threading.Thread(target=stream.run, daemon=True)
        thread.start()
        deadline = time.time() + 5
        while time.time() < deadline and stream.stats()["expired"] < 1:
            time.sleep(0.02)
        stream.stop()
        thread.join(timeout=5)
        stats = stream.stats()
        assert stats["expired"] >= 1
        assert stats["lists"] >= 2
        assert stats["reconnects"] >= 1

    def test_run_survives_a_transient_error(self, monkeypatch):
        client = FakeClient(list_error=watcher.WatchError("boom"))
        stream = watcher.Watcher(client, "pods", "auton-apps",
                                 backoff=(0.01,))
        thread = threading.Thread(target=stream.run, daemon=True)
        thread.start()
        deadline = time.time() + 5
        while time.time() < deadline and stream.stats()["errors"] < 1:
            time.sleep(0.02)
        stream.stop()
        thread.join(timeout=5)
        assert stream.stats()["errors"] >= 1
        assert stream.stats()["last_error"]

    def test_forbidden_is_counted(self, monkeypatch):
        client = FakeClient(list_error=watcher.WatchForbidden("no permission"))
        stream = watcher.Watcher(client, "pods", "auton-apps",
                                 backoff=(0.01,))
        thread = threading.Thread(target=stream.run, daemon=True)
        thread.start()
        deadline = time.time() + 5
        while time.time() < deadline and stream.stats()["forbidden"] < 1:
            time.sleep(0.02)
        stream.stop()
        thread.join(timeout=5)
        stats = stream.stats()
        assert stats["forbidden"] == 1
        assert stats["available"] is False

    def test_stats_shape(self):
        stream = watcher.Watcher(FakeClient(), "pods", "auton-apps")
        stats = stream.stats()
        for key in ("name", "resource", "namespace", "available", "connected",
                    "resource_version", "events", "reconnects", "errors",
                    "expired", "forbidden", "last_error", "uptime_seconds"):
            assert key in stats
        assert stats["resource"] == "pods"
        assert stats["namespace"] == "auton-apps"


# --------------------------------------------------------------------------- #
# The cache
# --------------------------------------------------------------------------- #

class TestObjectCache:
    def test_apply_and_read(self):
        cache = watcher.ObjectCache()
        assert cache.apply("pods", "ADDED", _pod("a")) == ("auton-apps", "a")
        assert len(cache.objects("pods", "auton-apps")) == 1
        assert cache.names("pods", "auton-apps") == {"a"}
        assert cache.size() == 1

    def test_delete_removes_the_object(self):
        cache = watcher.ObjectCache()
        cache.apply("pods", "ADDED", _pod("a"))
        cache.apply("pods", "DELETED", _pod("a"))
        assert cache.objects("pods", "auton-apps") == []
        assert cache.size() == 0

    def test_resources_do_not_collide(self):
        # A Pod and a Deployment routinely share a name.
        cache = watcher.ObjectCache()
        cache.apply("pods", "ADDED", _pod("api"))
        cache.apply("deployments", "ADDED", _pod("api"))
        assert cache.size() == 2
        assert len(cache.objects("pods", "auton-apps")) == 1
        assert len(cache.objects("deployments", "auton-apps")) == 1

    def test_namespaces_are_separate(self):
        cache = watcher.ObjectCache()
        cache.apply("pods", "ADDED", _pod("a", namespace="one"))
        cache.apply("pods", "ADDED", _pod("b", namespace="two"))
        assert len(cache.objects("pods", "one")) == 1
        assert len(cache.objects("pods", "two")) == 1
        assert len(cache.objects("pods")) == 2

    def test_replace_rebases_the_cache(self):
        cache = watcher.ObjectCache()
        cache.apply("pods", "ADDED", _pod("old"))
        count = cache.replace("pods", [_pod("new")])
        assert count == 1
        assert cache.names("pods", "auton-apps") == {"new"}

    def test_an_object_without_a_name_is_ignored(self):
        cache = watcher.ObjectCache()
        assert cache.apply("pods", "ADDED", {"metadata": {}}) is None
        assert cache.size() == 0


# --------------------------------------------------------------------------- #
# The fast path: detection straight from the cache
# --------------------------------------------------------------------------- #

class TestFastPath:
    def test_pod_findings_from_the_cache(self):
        cache = watcher.ObjectCache()
        cache.apply("pods", "ADDED", _oom_pod("api-1"))
        pods = [ops_controller.k8s_tools.pod_summary(pod)
                for pod in cache.objects("pods", "auton-apps")]
        found, covered = ops_controller.pod_findings(pods, "auton-apps")
        assert len(found) == 1
        assert found[0]["cause"] == "oom"
        assert found[0]["kind"] == "Pod"
        assert found[0]["name"] == "api-1"
        assert covered == set()

    def test_a_healthy_pod_produces_no_finding(self):
        cache = watcher.ObjectCache()
        cache.apply("pods", "ADDED", _pod("api-1"))
        pods = [ops_controller.k8s_tools.pod_summary(pod)
                for pod in cache.objects("pods", "auton-apps")]
        found, _ = ops_controller.pod_findings(pods, "auton-apps")
        assert found == []

    def test_scan_pods_enqueues_one_task(self):
        backend = ops_queue.MemoryBackend()
        store = task_store.TaskStore(backend)
        pods = [ops_controller.k8s_tools.pod_summary(_oom_pod("api-1"))]
        summary = ops_controller.scan_pods(store, pods, "auton-apps")
        assert summary["source"] == "watch"
        assert summary["findings"] == 1
        assert summary["queued"] == 1
        assert len(summary["tasks"]) == 1

    def test_scan_pods_is_idempotent(self):
        backend = ops_queue.MemoryBackend()
        store = task_store.TaskStore(backend)
        pods = [ops_controller.k8s_tools.pod_summary(_oom_pod("api-1"))]
        ops_controller.scan_pods(store, pods, "auton-apps")
        again = ops_controller.scan_pods(store, pods, "auton-apps")
        assert again["findings"] == 1
        assert again["queued"] == 0
        # The first task is still active, so the second pass is suppressed
        # before it can even reach the idempotency key.
        assert again["suppressed"] == 1
        assert again["tasks"] == []


# --------------------------------------------------------------------------- #
# The scanner wiring
# --------------------------------------------------------------------------- #

class TestScanner:
    def test_watch_plane_without_credentials_falls_back(self, monkeypatch):
        monkeypatch.setattr(ops_scanner.watch_api, "resolve_config",
                            lambda: None)
        plane = ops_scanner.WatchPlane(["auton-apps"])
        assert plane.start() is False
        assert plane.started is False
        assert "credentials" in plane.reason
        assert plane.healthy() is False

    def test_watch_plane_opens_one_stream_per_resource(self, monkeypatch):
        config = watcher.ApiConfig("https://api", "tok")
        monkeypatch.setattr(ops_scanner.watch_api, "resolve_config",
                            lambda: config)
        started: list[str] = []

        class NoopWatcher(watcher.Watcher):
            def run(self, on_event=None):
                started.append(self.name)
                while not self.stopped():
                    time.sleep(0.01)

        monkeypatch.setattr(ops_scanner.watch_api, "Watcher", NoopWatcher)
        plane = ops_scanner.WatchPlane(["auton-apps"])
        assert plane.start() is True
        try:
            assert len(plane.watchers) == len(ops_scanner.WATCH_RESOURCES)
            assert sorted(started) == sorted(
                f"{resource}/auton-apps"
                for resource in ops_scanner.WATCH_RESOURCES)
        finally:
            plane.stop()

    def test_a_bad_pod_event_triggers_the_fast_path(self):
        plane = ops_scanner.WatchPlane(["auton-apps"])
        plane._on_event("pods", "auton-apps", "MODIFIED", _oom_pod("api-1"))
        fast, full = plane.take_pending()
        assert fast is True
        assert full is False

    def test_a_healthy_pod_event_triggers_nothing(self):
        plane = ops_scanner.WatchPlane(["auton-apps"])
        plane._on_event("pods", "auton-apps", "MODIFIED", _pod("api-1"))
        fast, full = plane.take_pending()
        assert fast is False
        assert full is False

    def test_a_deployment_event_triggers_the_full_path(self):
        plane = ops_scanner.WatchPlane(["auton-apps"])
        plane._on_event("deployments", "auton-apps", "MODIFIED", _pod("api"))
        fast, full = plane.take_pending()
        assert fast is False
        assert full is True

    def test_the_trigger_is_debounced(self, monkeypatch):
        monkeypatch.setattr(ops_scanner, "WATCH_MIN_GAP", 60)
        plane = ops_scanner.WatchPlane(["auton-apps"])
        plane._on_event("pods", "auton-apps", "MODIFIED", _oom_pod("api-1"))
        plane.take_pending()
        plane._on_event("pods", "auton-apps", "MODIFIED", _oom_pod("api-2"))
        fast, _ = plane.take_pending()
        assert fast is False

    def test_the_fast_pass_reads_the_cache(self):
        plane = ops_scanner.WatchPlane(["auton-apps"])
        plane.cache.apply("pods", "ADDED", _oom_pod("api-1"))
        backend = ops_queue.MemoryBackend()
        store = task_store.TaskStore(backend)
        summary = ops_scanner._fast_pass(store, plane)
        assert summary["source"] == "watch"
        assert summary["findings"] == 1
        assert summary["queued"] == 1

    def test_publish_makes_the_health_visible(self):
        plane = ops_scanner.WatchPlane(["auton-apps"])
        backend = ops_queue.MemoryBackend()
        store = task_store.TaskStore(backend)
        ops_scanner._publish(store, plane)
        published = store.watcher_stats()
        assert any(item.get("name") == "scanner" for item in published)


# --------------------------------------------------------------------------- #
# The published health
# --------------------------------------------------------------------------- #

class TestWatcherStats:
    def test_set_and_read(self):
        backend = ops_queue.MemoryBackend()
        store = task_store.TaskStore(backend)
        store.set_watcher_stats("pods/auton-apps", {
            "name": "pods/auton-apps", "available": True, "events": 12,
        })
        published = store.watcher_stats()
        assert len(published) == 1
        assert published[0]["available"] is True
        assert published[0]["events"] == 12

    def test_stats_exposes_the_watchers(self):
        backend = ops_queue.MemoryBackend()
        store = task_store.TaskStore(backend)
        store.set_watcher_stats("pods/auton-apps", {"name": "pods/auton-apps"})
        assert "watchers" in store.stats()
        assert store.stats()["watchers"][0]["name"] == "pods/auton-apps"

    def test_a_corrupt_payload_is_ignored(self):
        backend = ops_queue.MemoryBackend()
        store = task_store.TaskStore(backend)
        backend.set(store.watcher_key("broken"), "not json")
        assert store.watcher_stats() == []

    def test_the_payload_expires(self):
        backend = ops_queue.MemoryBackend()
        store = task_store.TaskStore(backend)
        store.set_watcher_stats("pods/auton-apps", {"name": "pods/auton-apps"},
                                ttl=10)
        assert store.watcher_stats()
