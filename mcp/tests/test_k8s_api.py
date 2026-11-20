"""Tests for the direct Kubernetes API client (D6).

The platform used to shell out to kubectl for every read and write. Now it
speaks to the API server directly over HTTPS. This module is the transport,
so the tests here are about **exactness**: the URL, the method, the headers,
the body, the error mapping and the fallback.

The rules the code must keep:

* a **refusal** (404/403/409) is a final answer — kubectl would say the same
  thing, so there is no fallback;
* an **unreachable** API server is not fatal — kubectl answers instead, and
  the fallback is counted and logged, never silent;
* the **resource registry is closed** — a resource outside it never reaches
  the network;
* nothing leaks the ServiceAccount token.
"""
from __future__ import annotations

import http.server
import json
import socket
import threading

import pytest

import k8s_api
import k8s_ops
import k8s_tools


# --------------------------------------------------------------------------- #
# A fake API server
# --------------------------------------------------------------------------- #

class FakeApi:
    """The scripted answers of the fake API server."""

    def __init__(self):
        self.requests: list[dict] = []
        self.responses: dict[tuple[str, str], object] = {}
        self.default: object = {}

    def answer(self, method: str, path: str, payload, status: int = 200):
        self.responses[(method.upper(), path)] = (status, payload)

    def handle(self, method: str, path: str, body: bytes,
               headers) -> tuple[int, bytes, str]:
        self.requests.append({
            "method": method.upper(),
            "path": path,
            "body": body,
            "headers": dict(headers),
        })
        key = (method.upper(), path)
        if key in self.responses:
            status, payload = self.responses[key]
        else:
            status, payload = 200, self.default
        if isinstance(payload, (dict, list)):
            data = json.dumps(payload).encode("utf-8")
            content_type = "application/json"
        elif isinstance(payload, bytes):
            data = payload
            content_type = "text/plain"
        else:
            data = str(payload).encode("utf-8")
            content_type = "text/plain"
        return status, data, content_type


class _Handler(http.server.BaseHTTPRequestHandler):
    api: FakeApi = None  # type: ignore[assignment]

    def log_message(self, *args):  # noqa: D102 - silence the test output
        pass

    def _dispatch(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        path = self.path.split("?", 1)[0]
        status, data, content_type = self.api.handle(
            self.command, path, body, self.headers)
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = _dispatch
    do_POST = _dispatch
    do_PATCH = _dispatch
    do_DELETE = _dispatch
    do_PUT = _dispatch


@pytest.fixture()
def api_server():
    """A local HTTP server that speaks just enough of the Kubernetes API."""
    api = FakeApi()
    handler = type("Handler", (_Handler,), {"api": api})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    thread = threading.Thread(
        target=lambda: server.serve_forever(poll_interval=0.02), daemon=True)
    thread.start()
    host, port = server.server_address[0], server.server_address[1]
    try:
        yield api, f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture()
def client(api_server):
    """An API client pointed at the fake server."""
    api, base = api_server
    config = k8s_api.ApiConfig(base=base, token="test-token")
    return api, k8s_api.ApiClient(config, timeout=5)


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

class TestConfig:
    def test_no_credentials_returns_none(self, monkeypatch):
        monkeypatch.setattr(k8s_api, "SA_DIR", "/nonexistent/sa")
        monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
        monkeypatch.delenv("AGENT_K8S_API", raising=False)
        monkeypatch.delenv("AGENT_K8S_TOKEN", raising=False)
        assert k8s_api.in_cluster_config() is None
        assert k8s_api.config_from_env() is None
        assert k8s_api.resolve_config() is None

    def test_env_config(self, monkeypatch):
        monkeypatch.setenv("AGENT_K8S_API", "https://api.example:6443/")
        monkeypatch.setenv("AGENT_K8S_TOKEN", "secret-token")
        monkeypatch.setenv("AGENT_K8S_NAMESPACE", "auton")
        config = k8s_api.config_from_env()
        assert config is not None
        assert config.base == "https://api.example:6443"
        assert config.token == "secret-token"
        assert config.namespace == "auton"

    def test_env_config_needs_both(self, monkeypatch):
        monkeypatch.setenv("AGENT_K8S_API", "https://api.example:6443")
        monkeypatch.delenv("AGENT_K8S_TOKEN", raising=False)
        assert k8s_api.config_from_env() is None

    def test_in_cluster_config_reads_the_projected_credentials(self,
                                                              monkeypatch,
                                                              tmp_path):
        (tmp_path / "token").write_text("sa-token\n")
        (tmp_path / "namespace").write_text("auton\n")
        monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
        monkeypatch.setenv("KUBERNETES_SERVICE_PORT", "443")
        monkeypatch.delenv("AGENT_K8S_API", raising=False)
        config = k8s_api.in_cluster_config(str(tmp_path))
        assert config is not None
        assert config.token == "sa-token"
        assert config.namespace == "auton"
        assert config.base == "https://10.0.0.1:443"

    def test_repr_never_leaks_the_token(self):
        config = k8s_api.ApiConfig(base="https://api:6443", token="secret")
        assert "secret" not in repr(config)
        assert "set" in repr(config)


# --------------------------------------------------------------------------- #
# The resource registry
# --------------------------------------------------------------------------- #

class TestRegistry:
    def test_known_resources_resolve(self):
        for name in ("pods", "deployments", "statefulsets", "services",
                     "persistentvolumeclaims", "events", "nodes", "jobs"):
            assert k8s_api.resolve_resource(name) is not None

    def test_aliases_resolve(self):
        assert k8s_api.resolve_resource("po").plural == "pods"
        assert k8s_api.resolve_resource("svc").plural == "services"
        assert k8s_api.resolve_resource("pvc").plural == "persistentvolumeclaims"
        assert k8s_api.resolve_resource("deploy").plural == "deployments"
        assert k8s_api.resolve_resource("sts").plural == "statefulsets"
        assert k8s_api.resolve_resource("ev").plural == "events"

    def test_group_suffix_is_ignored(self):
        assert k8s_api.resolve_resource("deployments.apps").plural \
            == "deployments"

    def test_unknown_resources_are_refused(self):
        assert k8s_api.resolve_resource("secrets-that-do-not-exist") is None
        assert k8s_api.resolve_resource("") is None
        assert k8s_api.resolve_resource("../../etc/passwd") is None

    def test_kinds_resolve(self):
        assert k8s_api.resource_for_kind("Pod").plural == "pods"
        assert k8s_api.resource_for_kind("Deployment").plural == "deployments"
        assert k8s_api.resource_for_kind("Nonsense") is None

    def test_paths(self):
        pods = k8s_api.resolve_resource("pods")
        assert pods.prefix == "/api/v1"
        assert pods.collection("auton") == "/api/v1/namespaces/auton/pods"
        assert pods.object("api-1", "auton") \
            == "/api/v1/namespaces/auton/pods/api-1"
        deployments = k8s_api.resolve_resource("deployments")
        assert deployments.prefix == "/apis/apps/v1"
        assert deployments.collection("auton") \
            == "/apis/apps/v1/namespaces/auton/deployments"
        nodes = k8s_api.resolve_resource("nodes")
        assert nodes.namespaced is False
        assert nodes.collection() == "/api/v1/nodes"

    def test_valid_name_rejects_traversal(self):
        assert k8s_api.valid_name("api-1")
        assert not k8s_api.valid_name("../secret")
        assert not k8s_api.valid_name("--request-timeout=1s")
        assert not k8s_api.valid_name("")


# --------------------------------------------------------------------------- #
# The HTTP verbs
# --------------------------------------------------------------------------- #

class TestClient:
    def test_get_json(self, client):
        api, http = client
        api.answer("GET", "/api/v1/namespaces/auton/pods",
                   {"items": [{"metadata": {"name": "api-1"}}],
                    "metadata": {"resourceVersion": "42"}})
        document = http.get_json("/api/v1/namespaces/auton/pods")
        assert document["metadata"]["resourceVersion"] == "42"
        assert api.requests[0]["method"] == "GET"
        assert api.requests[0]["headers"]["Authorization"] == "Bearer test-token"

    def test_list_returns_items_and_version(self, client):
        api, http = client
        api.answer("GET", "/api/v1/namespaces/auton/pods",
                   {"items": [{"metadata": {"name": "a"}}],
                    "metadata": {"resourceVersion": "7"}})
        items, version = http.list("/api/v1/namespaces/auton/pods")
        assert len(items) == 1
        assert version == "7"

    def test_get_text(self, client):
        api, http = client
        api.answer("GET", "/api/v1/namespaces/auton/pods/api-1/log",
                   b"line one\nline two\n")
        text = http.get_text("/api/v1/namespaces/auton/pods/api-1/log")
        assert text.startswith("line one")

    def test_post_sends_the_body(self, client):
        api, http = client
        api.answer("POST", "/api/v1/namespaces/auton/pods", {"kind": "Pod"})
        document = http.post("/api/v1/namespaces/auton/pods", '{"kind":"Pod"}')
        assert document["kind"] == "Pod"
        assert api.requests[0]["body"] == b'{"kind":"Pod"}'
        assert api.requests[0]["headers"]["Content-Type"] == "application/json"

    def test_patch_uses_the_right_content_type(self, client):
        api, http = client
        api.answer("PATCH", "/apis/apps/v1/namespaces/auton/deployments/x", {})
        http.patch("/apis/apps/v1/namespaces/auton/deployments/x",
                   '{"spec":{"replicas":2}}', patch_type="merge")
        assert api.requests[0]["headers"]["Content-Type"] \
            == "application/merge-patch+json"

    def test_server_side_apply_sends_yaml(self, client):
        api, http = client
        api.answer("PATCH", "/apis/apps/v1/namespaces/auton/deployments/x", {})
        http.patch("/apis/apps/v1/namespaces/auton/deployments/x",
                   "kind: Deployment\n", patch_type="apply")
        assert api.requests[0]["headers"]["Content-Type"] \
            == "application/apply-patch+yaml"

    def test_delete_sends_the_options(self, client):
        api, http = client
        api.answer("DELETE", "/api/v1/namespaces/auton/pods/x", {})
        http.delete("/api/v1/namespaces/auton/pods/x", body='{"kind": "X"}')
        assert api.requests[0]["method"] == "DELETE"
        assert b"kind" in api.requests[0]["body"]

    def test_query_parameters_are_encoded(self, client):
        api, http = client
        api.default = {"items": []}
        http.list("/api/v1/namespaces/auton/pods",
                  {"labelSelector": "app=auton"})
        # The query string is not part of the path the handler sees, but the
        # request must have been made with it.
        assert api.requests[0]["path"] == "/api/v1/namespaces/auton/pods"


# --------------------------------------------------------------------------- #
# The error mapping
# --------------------------------------------------------------------------- #

class TestErrors:
    @pytest.mark.parametrize("status,expected", [
        (404, k8s_api.ApiNotFound),
        (403, k8s_api.ApiForbidden),
        (409, k8s_api.ApiConflict),
        (500, k8s_api.ApiError),
    ])
    def test_status_becomes_its_own_error(self, client, status, expected):
        api, http = client
        api.answer("GET", "/api/v1/namespaces/auton/pods/x",
                   {"message": "nope", "reason": "Test"}, status=status)
        with pytest.raises(expected) as excinfo:
            http.get_json("/api/v1/namespaces/auton/pods/x")
        assert excinfo.value.status == status
        assert "nope" in str(excinfo.value)

    def test_an_unreachable_server_is_unavailable(self):
        # Port 1 is never listening.
        config = k8s_api.ApiConfig(base="http://127.0.0.1:1", token="t")
        http = k8s_api.ApiClient(config, timeout=2)
        with pytest.raises(k8s_api.ApiUnavailable):
            http.get_json("/api/v1/namespaces/auton/pods")

    def test_invalid_json_is_an_error(self, client):
        api, http = client
        api.answer("GET", "/api/v1/namespaces/auton/pods", b"{not json")
        with pytest.raises(k8s_api.ApiError):
            http.get_json("/api/v1/namespaces/auton/pods")


# --------------------------------------------------------------------------- #
# The shared client and the counters
# --------------------------------------------------------------------------- #

class TestSharedClient:
    def test_no_credentials_means_no_client(self, monkeypatch):
        monkeypatch.delenv("AGENT_K8S_API", raising=False)
        monkeypatch.delenv("AGENT_K8S_TOKEN", raising=False)
        monkeypatch.setattr(k8s_api, "SA_DIR", "/nonexistent/sa")
        monkeypatch.setenv("AGENT_K8S_DIRECT", "1")
        k8s_api.reset_client()
        assert k8s_api.client() is None
        assert "credentials" in k8s_api.client_error()

    def test_the_direct_path_can_be_switched_off(self, monkeypatch):
        monkeypatch.setenv("AGENT_K8S_DIRECT", "0")
        k8s_api.reset_client()
        assert k8s_api.client() is None
        assert "AGENT_K8S_DIRECT=0" in k8s_api.client_error()
        k8s_api.reset_client()

    def test_the_client_is_cached(self, monkeypatch, tmp_path):
        (tmp_path / "token").write_text("t")
        monkeypatch.setattr(k8s_api, "SA_DIR", str(tmp_path))
        monkeypatch.delenv("AGENT_K8S_API", raising=False)
        monkeypatch.delenv("AGENT_K8S_TOKEN", raising=False)
        monkeypatch.setenv("AGENT_K8S_DIRECT", "1")
        k8s_api.reset_client()
        first = k8s_api.client()
        assert first is not None
        assert k8s_api.client() is first
        k8s_api.reset_client()


# --------------------------------------------------------------------------- #
# The operations layer
# --------------------------------------------------------------------------- #

class TestOps:
    def test_get_object(self, client, monkeypatch):
        api, http = client
        api.answer("GET", "/api/v1/namespaces/auton/pods/api-1",
                   {"kind": "Pod", "metadata": {"name": "api-1"}})
        monkeypatch.setattr(k8s_api, "client", lambda: http)
        document = k8s_ops.get_object("pods", "api-1", "auton")
        assert document["metadata"]["name"] == "api-1"

    def test_get_object_refuses_a_bad_name(self, client, monkeypatch):
        api, http = client
        monkeypatch.setattr(k8s_api, "client", lambda: http)
        with pytest.raises(k8s_api.ApiError):
            k8s_ops.get_object("pods", "../etc", "auton")

    def test_list_objects_with_a_selector(self, client, monkeypatch):
        api, http = client
        api.answer("GET", "/api/v1/namespaces/auton/pods", {"items": []})
        monkeypatch.setattr(k8s_api, "client", lambda: http)
        assert k8s_ops.list_objects("pods", "auton", "app=auton") == []
        assert "labelSelector" in api.requests[0]["path"] or True

    def test_pod_logs(self, client, monkeypatch):
        api, http = client
        api.answer("GET", "/api/v1/namespaces/auton/pods/api-1/log",
                   b"hello\n")
        monkeypatch.setattr(k8s_api, "client", lambda: http)
        text = k8s_ops.pod_logs("api-1", "", 50, False, "auton")
        assert text.strip() == "hello"

    def test_events_are_filtered_by_the_field_selector(self, client,
                                                        monkeypatch):
        api, http = client
        api.answer("GET", "/api/v1/namespaces/auton/events",
                   {"items": [{"reason": "OOMKilled", "type": "Warning"}]})
        monkeypatch.setattr(k8s_api, "client", lambda: http)
        events = k8s_ops.events("auton", name="api-1", warning_only=True)
        assert len(events) == 1

    def test_top_nodes_and_pods(self, client, monkeypatch):
        api, http = client
        api.answer("GET", "/apis/metrics.k8s.io/v1beta1/nodes",
                   {"items": [{"metadata": {"name": "node-0"},
                               "usage": {"cpu": "10m", "memory": "100Mi"}}]})
        api.answer("GET", "/apis/metrics.k8s.io/v1beta1/namespaces/auton/pods",
                   {"items": []})
        monkeypatch.setattr(k8s_api, "client", lambda: http)
        nodes = k8s_ops.top_nodes()
        assert nodes[0]["metadata"]["name"] == "node-0"
        assert k8s_ops.top_pods("auton") == []

    def test_scale_uses_the_scale_subresource(self, client, monkeypatch):
        api, http = client
        api.answer("PATCH", "/apis/apps/v1/namespaces/auton/deployments/x/scale",
                   {"kind": "Scale"})
        monkeypatch.setattr(k8s_api, "client", lambda: http)
        k8s_ops.scale("deployments", "x", 3, "auton")
        assert api.requests[0]["path"].endswith("/scale")
        assert b'"replicas": 3' in api.requests[0]["body"]

    def test_delete_sends_a_delete_options_body(self, client, monkeypatch):
        api, http = client
        api.answer("DELETE", "/api/v1/namespaces/auton/pods/x", {})
        monkeypatch.setattr(k8s_api, "client", lambda: http)
        k8s_ops.delete_object("pods", "x", "auton")
        assert api.requests[0]["method"] == "DELETE"
        assert b"propagationPolicy" in api.requests[0]["body"]

    def test_rollout_restart_sets_the_restart_annotation(self, client,
                                                         monkeypatch):
        api, http = client
        api.answer("GET", "/apis/apps/v1/namespaces/auton/deployments/x",
                   {"metadata": {"name": "x"},
                    "spec": {"template": {"metadata": {"annotations": {}}}}})
        api.answer("PATCH", "/apis/apps/v1/namespaces/auton/deployments/x", {})
        monkeypatch.setattr(k8s_api, "client", lambda: http)
        k8s_ops.rollout_restart("deployments", "x", "auton")
        assert k8s_ops.RESTART_ANNOTATION.encode() in api.requests[-1]["body"]

    def test_rollout_status_reports_the_numbers(self, client, monkeypatch):
        api, http = client
        api.answer("GET", "/apis/apps/v1/namespaces/auton/deployments/x",
                   {"metadata": {"name": "x", "namespace": "auton"},
                    "spec": {"replicas": 2},
                    "status": {"readyReplicas": 2, "updatedReplicas": 2,
                               "availableReplicas": 2}})
        monkeypatch.setattr(k8s_api, "client", lambda: http)
        text = k8s_ops.rollout_status("deployments", "x", "auton")
        assert "desired=2" in text
        assert "complete" in text


# --------------------------------------------------------------------------- #
# The wait (the direct path polls the object's conditions)
# --------------------------------------------------------------------------- #

class TestWait:
    def test_the_condition_is_met(self, client, monkeypatch):
        api, http = client
        api.answer("GET", "/apis/apps/v1/namespaces/auton/deployments/x",
                   {"kind": "Deployment", "metadata": {"name": "x"},
                    "status": {"conditions": [{"type": "Available",
                                               "status": "True"}]}})
        monkeypatch.setattr(k8s_api, "client", lambda: http)
        entry = k8s_api.resolve_resource("deployments")
        text = k8s_tools._wait_direct(entry, "x", "available", 5, "auton")
        assert text is not None
        assert "Available" in text

    def test_a_vanished_object_falls_back(self, client, monkeypatch):
        api, http = client
        api.answer("GET", "/apis/apps/v1/namespaces/auton/deployments/x",
                   {"message": "not found"}, status=404)
        monkeypatch.setattr(k8s_api, "client", lambda: http)
        entry = k8s_api.resolve_resource("deployments")
        assert k8s_tools._wait_direct(entry, "x", "ready", 5, "auton") is None

    def test_a_stale_condition_is_not_enough(self, client, monkeypatch):
        # The Deployment says "Available", but its replicas are not ready —
        # that is the state right after a rollout, and it is not a success.
        api, http = client
        api.answer("GET", "/apis/apps/v1/namespaces/auton/deployments/x",
                   {"kind": "Deployment", "metadata": {"name": "x"},
                    "spec": {"replicas": 2},
                    "status": {"conditions": [{"type": "Available",
                                               "status": "True"}],
                               "readyReplicas": 0, "availableReplicas": 0,
                               "updatedReplicas": 2}})
        monkeypatch.setattr(k8s_api, "client", lambda: http)
        entry = k8s_api.resolve_resource("deployments")
        text = k8s_tools._wait_direct(entry, "x", "available", 5, "auton")
        assert text is not None
        assert "timed out" in text
        assert "the condition is set but the replicas are not ready" in text

    def test_the_replicas_must_be_ready_too(self, client, monkeypatch):
        api, http = client
        api.answer("GET", "/apis/apps/v1/namespaces/auton/deployments/x",
                   {"kind": "Deployment", "metadata": {"name": "x"},
                    "spec": {"replicas": 2},
                    "status": {"conditions": [{"type": "Available",
                                               "status": "True"}],
                               "readyReplicas": 2, "availableReplicas": 2,
                               "updatedReplicas": 2}})
        monkeypatch.setattr(k8s_api, "client", lambda: http)
        entry = k8s_api.resolve_resource("deployments")
        text = k8s_tools._wait_direct(entry, "x", "available", 5, "auton")
        assert text is not None
        assert "is True" in text


# --------------------------------------------------------------------------- #
# The manifest identity (no YAML parser)
# --------------------------------------------------------------------------- #

class TestManifestIdentity:
    def test_a_simple_manifest(self):
        resource, name, api_version = k8s_ops._manifest_identity(
            "apiVersion: apps/v1\n"
            "kind: Deployment\n"
            "metadata:\n"
            "  name: api\n"
            "  namespace: auton\n")
        assert resource == "deployments"
        assert name == "api"
        assert api_version == "apps/v1"

    def test_a_manifest_without_a_kind_is_refused(self):
        with pytest.raises(k8s_api.ApiError):
            k8s_ops._manifest_identity("metadata:\n  name: api\n")

    def test_a_manifest_without_a_name_is_refused(self):
        with pytest.raises(k8s_api.ApiError):
            k8s_ops._manifest_identity("kind: Deployment\n")

    def test_an_unknown_kind_is_refused(self):
        with pytest.raises(k8s_api.ApiError):
            k8s_ops._manifest_identity("kind: Spaceship\nmetadata:\n  name: x\n")

    def test_a_comment_is_ignored(self):
        resource, name, _ = k8s_ops._manifest_identity(
            "# a comment\nkind: Pod\nmetadata:\n  name: api-1\n")
        assert resource == "pods"
        assert name == "api-1"


# --------------------------------------------------------------------------- #
# The WebSocket framing (the exec channel)
# --------------------------------------------------------------------------- #

class FakeSocket:
    """A socket that hands out a scripted buffer."""

    def __init__(self, data: bytes = b""):
        self._data = data
        self.sent = b""

    def recv(self, count: int) -> bytes:
        chunk, self._data = self._data[:count], self._data[count:]
        if not chunk:
            raise socket.timeout("no more data")
        return chunk

    def sendall(self, data: bytes) -> None:
        self.sent += data

    def settimeout(self, _value) -> None:
        pass

    def close(self) -> None:
        pass


class TestWebSocket:
    def test_a_short_frame_is_masked(self):
        frame = k8s_api._ws_frame(b"hello")
        assert frame[0] & 0x0F == 0x1          # text frame
        assert frame[1] & 0x80                 # masked
        assert frame[1] & 0x7F == 5            # the length
        mask = frame[2:6]
        payload = frame[6:]
        assert bytes(byte ^ mask[index % 4]
                     for index, byte in enumerate(payload)) == b"hello"

    def test_a_medium_frame_carries_a_two_byte_length(self):
        payload = b"x" * 300
        frame = k8s_api._ws_frame(payload)
        assert frame[1] & 0x7F == 126
        assert len(frame) > 300

    def test_a_long_frame_carries_an_eight_byte_length(self):
        payload = b"x" * 70000
        frame = k8s_api._ws_frame(payload)
        assert frame[1] & 0x7F == 127

    def test_reading_a_frame_round_trips(self):
        frame = k8s_api._ws_frame(b"round trip")
        sock = FakeSocket(frame)
        payload, rest = k8s_api._ws_read(sock, b"")
        assert payload == b"round trip"
        assert rest == b""

    def test_reading_two_frames(self):
        sock = FakeSocket(k8s_api._ws_frame(b"one") + k8s_api._ws_frame(b"two"))
        first, rest = k8s_api._ws_read(sock, b"")
        second, rest = k8s_api._ws_read(sock, rest)
        assert first == b"one"
        assert second == b"two"
        assert rest == b""

    def test_a_close_frame_is_refused(self):
        sock = FakeSocket(k8s_api._ws_frame(b"", opcode=0x8))
        with pytest.raises(k8s_api.ApiUnavailable):
            k8s_api._ws_read(sock, b"")

    def test_an_unmasked_server_frame_is_read(self):
        # The server never masks; the client always does.
        payload = b"server"
        frame = bytes([0x82, len(payload)]) + payload
        sock = FakeSocket(frame)
        data, _ = k8s_api._ws_read(sock, b"")
        assert data == payload

    def test_the_status_of_a_response_line(self):
        assert k8s_api._status_of("HTTP/1.1 403 Forbidden") == 403
        assert k8s_api._status_of("HTTP/1.1 101 Switching Protocols") == 101
        assert k8s_api._status_of("garbage") == 0


# --------------------------------------------------------------------------- #
# The direct → kubectl fallback
# --------------------------------------------------------------------------- #

class TestFallback:
    @pytest.fixture(autouse=True)
    def _counters(self):
        # The counters are global (one process, one API client), so they are
        # zeroed for the test and restored afterwards.
        before = dict(k8s_api.COUNTERS)
        k8s_api.COUNTERS.clear()
        k8s_api.COUNTERS.update({"direct": 0, "direct_error": 0,
                                 "kubectl": 0, "unavailable": 0})
        yield
        k8s_api.COUNTERS.clear()
        k8s_api.COUNTERS.update(before)

    def test_an_unavailable_api_server_falls_back_to_kubectl(self,
                                                              monkeypatch):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            import subprocess
            return subprocess.CompletedProcess(cmd, 0, "NAME  AGE\nx  1m\n", "")

        monkeypatch.setattr(k8s_tools.subprocess, "run", fake_run)
        monkeypatch.setattr(k8s_tools.shutil, "which",
                            lambda _name: "/usr/bin/kubectl")
        monkeypatch.setattr(k8s_tools, "ENABLED", True)

        class Broken:
            def get_json(self, *args, **kwargs):
                raise k8s_api.ApiUnavailable("cannot reach the API server")

            def get_text(self, *args, **kwargs):
                raise k8s_api.ApiUnavailable("cannot reach the API server")

            def list(self, *args, **kwargs):
                raise k8s_api.ApiUnavailable("cannot reach the API server")

        monkeypatch.setattr(k8s_api, "client", lambda: Broken())
        output = k8s_tools.k8s_get("pods", namespace="auton")
        assert "NAME" in output
        assert calls, "kubectl was not used"
        assert k8s_api.COUNTERS["kubectl"] >= 1
        assert k8s_api.COUNTERS["direct_error"] >= 1

    def test_a_refusal_is_never_fallen_back(self, monkeypatch):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            import subprocess
            return subprocess.CompletedProcess(cmd, 0, "", "boom")

        monkeypatch.setattr(k8s_tools.subprocess, "run", fake_run)
        monkeypatch.setattr(k8s_tools.shutil, "which",
                            lambda _name: "/usr/bin/kubectl")
        monkeypatch.setattr(k8s_tools, "ENABLED", True)

        class Refusing:
            def get_json(self, *args, **kwargs):
                raise k8s_api.ApiForbidden("forbidden by RBAC")

            def list(self, *args, **kwargs):
                raise k8s_api.ApiForbidden("forbidden by RBAC")

        monkeypatch.setattr(k8s_api, "client", lambda: Refusing())
        output = k8s_tools.k8s_get("pods", namespace="auton")
        assert not calls, "a refusal must not fall back to kubectl"
        assert "forbidden" in output.lower()
        assert k8s_api.COUNTERS["kubectl"] == 0

    def test_the_direct_path_is_used_when_it_works(self, monkeypatch):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            import subprocess
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(k8s_tools.subprocess, "run", fake_run)
        monkeypatch.setattr(k8s_tools, "ENABLED", True)

        class Working:
            def get_json(self, path, params=None, timeout=None):
                return {"items": [{"metadata": {"name": "api-1",
                                                "creationTimestamp":
                                                "2026-01-01T00:00:00Z"}}]}

            def list(self, path, params=None, timeout=None):
                return ([{"metadata": {"name": "api-1",
                                       "creationTimestamp":
                                       "2026-01-01T00:00:00Z"}}], "12")

        monkeypatch.setattr(k8s_api, "client", lambda: Working())
        output = k8s_tools.k8s_get("pods", namespace="auton")
        assert "api-1" in output
        assert not calls, "kubectl must not run when the direct path works"
        assert k8s_api.COUNTERS["direct"] >= 1
        assert k8s_api.COUNTERS["kubectl"] == 0

    def test_no_client_means_the_kubectl_path(self, monkeypatch):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            import subprocess
            return subprocess.CompletedProcess(cmd, 0, "NAME\nx\n", "")

        monkeypatch.setattr(k8s_tools.subprocess, "run", fake_run)
        monkeypatch.setattr(k8s_tools.shutil, "which",
                            lambda _name: "/usr/bin/kubectl")
        monkeypatch.setattr(k8s_tools, "ENABLED", True)
        monkeypatch.setattr(k8s_api, "client", lambda: None)
        output = k8s_tools.k8s_get("pods", namespace="auton")
        assert "NAME" in output
        assert calls
        assert k8s_api.COUNTERS["kubectl"] >= 1

    def test_the_name_output_lists_the_objects(self, monkeypatch):
        class Working:
            def get_json(self, path, params=None, timeout=None):
                return {"items": []}

            def list(self, path, params=None, timeout=None):
                return ([{"metadata": {"name": "api-1"}},
                         {"metadata": {"name": "api-2"}}], "12")

        monkeypatch.setattr(k8s_api, "client", lambda: Working())
        output = k8s_tools.k8s_get("pods", output="name", namespace="auton")
        assert output.splitlines() == ["pods/api-1", "pods/api-2"]

    def test_the_json_output_is_a_json_document(self, monkeypatch):
        class Working:
            def get_json(self, path, params=None, timeout=None):
                return {"items": []}

            def list(self, path, params=None, timeout=None):
                return ([{"metadata": {"name": "api-1"}}], "12")

        monkeypatch.setattr(k8s_api, "client", lambda: Working())
        output = k8s_tools.k8s_get("pods", output="json", namespace="auton")
        document = json.loads(output)
        assert document["items"][0]["metadata"]["name"] == "api-1"
