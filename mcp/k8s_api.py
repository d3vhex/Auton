#!/usr/bin/env python3
"""The Kubernetes API, spoken directly over HTTPS — standard library only.

The platform used to shell out to ``kubectl`` for everything. That works, but
it is slow (a fork plus a client start-up per call), it depends on a 58 MB
binary in the image, and it hides the API's own semantics behind a CLI.

So the reads and the writes go straight to the API server over HTTPS, using
the ServiceAccount credentials the kubelet projects into every pod:

    /var/run/secrets/kubernetes.io/serviceaccount/{token,ca.crt,namespace}

There is **no client library** — ``urllib``, ``ssl`` and ``json`` are enough,
and a dependency is a dependency that can break the image.

The rules this module follows:

* **The RBAC is still the boundary.** This client can do whatever the
  ServiceAccount may do and nothing else; it grants no extra permission.
* **Every mutation is audited** by :mod:`k8s_tools` before it is sent. This
  module does not know about the audit log; it is a transport, not a gate.
* **kubectl stays as a fallback.** If the direct path fails (no credentials,
  an unreachable API server, an endpoint this client does not implement), the
  caller falls back to kubectl — and says so in the log and in the counters.
  The fallback is never silent.
* **Nothing is invented.** An error from the API server is reported with the
  server's own status and message, not with a guess.

The resource registry below is the closed set of resources this platform
knows. A name outside it is refused before a request is made.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import socket
import ssl
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator

#: Where the kubelet projects the pod's ServiceAccount credentials.
SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #

class ApiError(RuntimeError):
    """The API server refused a request (or could not be reached)."""

    def __init__(self, message: str, *, status: int = 0, reason: str = "",
                 body: str = ""):
        super().__init__(message)
        self.status = int(status or 0)
        self.reason = reason or ""
        self.body = body or ""

    def __str__(self) -> str:
        return self.args[0] if self.args else "api error"


class ApiNotFound(ApiError):
    """404 — the object does not exist."""


class ApiForbidden(ApiError):
    """403 — RBAC denied the verb."""


class ApiConflict(ApiError):
    """409 — the object already exists, or the change collided."""


class ApiUnavailable(ApiError):
    """The API server could not be reached at all."""


class WatchError(RuntimeError):
    """A watch stream failed in a way the caller has to know about."""


class WatchExpired(WatchError):
    """The resourceVersion was too old (HTTP 410 Gone): re-list and restart."""


class WatchForbidden(WatchError):
    """The ServiceAccount may not watch this resource."""


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

class ApiConfig:
    """Where the API server is and how to authenticate against it."""

    __slots__ = ("base", "token", "ca_file", "namespace", "verify")

    def __init__(self, base: str, token: str, ca_file: str = "",
                 namespace: str = "", verify: bool = True):
        self.base = base.rstrip("/")
        self.token = token
        self.ca_file = ca_file
        self.namespace = namespace
        self.verify = verify

    def __repr__(self) -> str:  # never leak the token
        return (f"ApiConfig(base={self.base!r}, namespace={self.namespace!r}, "
                f"token={'set' if self.token else 'unset'})")


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def in_cluster_config(sa_dir: str = "") -> ApiConfig | None:
    """The credentials the kubelet projects into every pod.

    Returns ``None`` when this process is not running inside the cluster, so
    the caller can fall back to kubectl instead of failing. ``sa_dir`` exists
    so a test (or a caller with its own mount) can point somewhere else.
    """
    directory = sa_dir or SA_DIR
    token = _read(os.path.join(directory, "token"))
    if not token:
        return None
    ca_file = os.path.join(directory, "ca.crt")
    if not os.path.isfile(ca_file):
        ca_file = ""
    namespace = _read(os.path.join(directory, "namespace"))
    host = os.environ.get("KUBERNETES_SERVICE_HOST", "").strip()
    port = os.environ.get("KUBERNETES_SERVICE_PORT", "443").strip() or "443"
    if not host:
        host = "kubernetes.default.svc"
    if host.startswith("http://"):
        host = host[len("http://"):]
    elif host.startswith("https://"):
        host = host[len("https://"):]
    base = os.environ.get("AGENT_K8S_API", "").strip() or f"https://{host}:{port}"
    return ApiConfig(base=base, token=token, ca_file=ca_file,
                     namespace=namespace)


def config_from_env() -> ApiConfig | None:
    """An explicit configuration, for a host run against a remote cluster."""
    base = os.environ.get("AGENT_K8S_API", "").strip()
    token = os.environ.get("AGENT_K8S_TOKEN", "").strip()
    if not base or not token:
        return None
    ca_file = os.environ.get("AGENT_K8S_CA_FILE", "").strip()
    if ca_file and not os.path.isfile(ca_file):
        ca_file = ""
    return ApiConfig(base=base, token=token, ca_file=ca_file,
                     namespace=os.environ.get("AGENT_K8S_NAMESPACE", "").strip())


def resolve_config() -> ApiConfig | None:
    """The best available configuration, or ``None``."""
    return config_from_env() or in_cluster_config()


# --------------------------------------------------------------------------- #
# The resource registry — a closed set
# --------------------------------------------------------------------------- #

class Resource:
    """One API resource: where it lives and what it is called."""

    __slots__ = ("group", "version", "plural", "kind", "namespaced")

    def __init__(self, group: str, version: str, plural: str, kind: str,
                 namespaced: bool = True):
        self.group = group
        self.version = version
        self.plural = plural
        self.kind = kind
        self.namespaced = namespaced

    @property
    def prefix(self) -> str:
        """``/api/v1`` or ``/apis/apps/v1``."""
        if not self.group:
            return f"/api/{self.version}"
        return f"/apis/{self.group}/{self.version}"

    def collection(self, namespace: str = "") -> str:
        """The collection path for this resource."""
        if self.namespaced and namespace:
            return f"{self.prefix}/namespaces/{namespace}/{self.plural}"
        return f"{self.prefix}/{self.plural}"

    def object(self, name: str, namespace: str = "") -> str:
        """The path of one object."""
        return f"{self.collection(namespace)}/{name}"

    def __repr__(self) -> str:
        return f"Resource({self.plural!r})"


def _r(group: str, version: str, plural: str, kind: str,
       namespaced: bool = True) -> Resource:
    return Resource(group, version, plural, kind, namespaced)


#: The resources this platform knows. Everything else is refused.
RESOURCES: dict[str, Resource] = {
    "pods": _r("", "v1", "pods", "Pod"),
    "services": _r("", "v1", "services", "Service"),
    "endpoints": _r("", "v1", "endpoints", "Endpoints"),
    "configmaps": _r("", "v1", "configmaps", "ConfigMap"),
    "secrets": _r("", "v1", "secrets", "Secret"),
    "persistentvolumeclaims": _r("", "v1", "persistentvolumeclaims",
                                 "PersistentVolumeClaim"),
    "events": _r("", "v1", "events", "Event"),
    "namespaces": _r("", "v1", "namespaces", "Namespace", namespaced=False),
    "nodes": _r("", "v1", "nodes", "Node", namespaced=False),
    "persistentvolumes": _r("", "v1", "persistentvolumes",
                            "PersistentVolume", namespaced=False),
    "resourcequotas": _r("", "v1", "resourcequotas", "ResourceQuota"),
    "limitranges": _r("", "v1", "limitranges", "LimitRange"),
    "deployments": _r("apps", "v1", "deployments", "Deployment"),
    "replicasets": _r("apps", "v1", "replicasets", "ReplicaSet"),
    "statefulsets": _r("apps", "v1", "statefulsets", "StatefulSet"),
    "daemonsets": _r("apps", "v1", "daemonsets", "DaemonSet"),
    "jobs": _r("batch", "v1", "jobs", "Job"),
    "cronjobs": _r("batch", "v1", "cronjobs", "CronJob"),
    "ingresses": _r("networking.k8s.io", "v1", "ingresses", "Ingress"),
    "networkpolicies": _r("networking.k8s.io", "v1", "networkpolicies",
                          "NetworkPolicy"),
    "horizontalpodautoscalers": _r("autoscaling", "v2",
                                   "horizontalpodautoscalers",
                                   "HorizontalPodAutoscaler"),
    "poddisruptionbudgets": _r("policy", "v1", "poddisruptionbudgets",
                                "PodDisruptionBudget"),
}

#: The aliases kubectl accepts, mapped onto the registry above.
ALIASES: dict[str, str] = {
    "po": "pods", "pod": "pods", "pods": "pods",
    "svc": "services", "service": "services", "services": "services",
    "ep": "endpoints", "endpoints": "endpoints",
    "cm": "configmaps", "configmap": "configmaps", "configmaps": "configmaps",
    "secret": "secrets", "secrets": "secrets",
    "pvc": "persistentvolumeclaims",
    "persistentvolumeclaim": "persistentvolumeclaims",
    "persistentvolumeclaims": "persistentvolumeclaims",
    "pv": "persistentvolumes", "persistentvolume": "persistentvolumes",
    "persistentvolumes": "persistentvolumes",
    "ev": "events", "event": "events", "events": "events",
    "ns": "namespaces", "namespace": "namespaces", "namespaces": "namespaces",
    "no": "nodes", "node": "nodes", "nodes": "nodes",
    "deploy": "deployments", "deployment": "deployments",
    "deployments": "deployments",
    "rs": "replicasets", "replicaset": "replicasets", "replicasets": "replicasets",
    "sts": "statefulsets", "statefulset": "statefulsets",
    "statefulsets": "statefulsets",
    "ds": "daemonsets", "daemonset": "daemonsets", "daemonsets": "daemonsets",
    "job": "jobs", "jobs": "jobs",
    "cj": "cronjobs", "cronjob": "cronjobs", "cronjobs": "cronjobs",
    "ing": "ingresses", "ingress": "ingresses", "ingresses": "ingresses",
    "netpol": "networkpolicies", "networkpolicy": "networkpolicies",
    "networkpolicies": "networkpolicies",
    "hpa": "horizontalpodautoscalers",
    "horizontalpodautoscaler": "horizontalpodautoscalers",
    "horizontalpodautoscalers": "horizontalpodautoscalers",
    "pdb": "poddisruptionbudgets",
    "poddisruptionbudget": "poddisruptionbudgets",
    "poddisruptionbudgets": "poddisruptionbudgets",
    "quota": "resourcequotas", "resourcequota": "resourcequotas",
    "resourcequotas": "resourcequotas",
    "limits": "limitranges", "limitrange": "limitranges",
    "limitranges": "limitranges",
}

#: The kinds the evidence collector asks for, in the API's own spelling.
KIND_TO_RESOURCE: dict[str, str] = {
    "Pod": "pods", "Service": "services", "ConfigMap": "configmaps",
    "Secret": "secrets", "PersistentVolumeClaim": "persistentvolumeclaims",
    "Event": "events", "Namespace": "namespaces", "Node": "nodes",
    "PersistentVolume": "persistentvolumes", "Deployment": "deployments",
    "ReplicaSet": "replicasets", "StatefulSet": "statefulsets",
    "DaemonSet": "daemonsets", "Job": "jobs", "CronJob": "cronjobs",
    "Ingress": "ingresses", "NetworkPolicy": "networkpolicies",
    "HorizontalPodAutoscaler": "horizontalpodautoscalers",
    "PodDisruptionBudget": "poddisruptionbudgets",
}


def resolve_resource(name: str) -> Resource | None:
    """The registry entry for a resource name or alias, or ``None``.

    Accepts ``pods``, ``po``, ``deployments.apps`` and ``deployment/api``.
    """
    raw = (name or "").strip().lower()
    if not raw:
        return None
    # "deployments.apps" -> the part before the dot is the resource.
    raw = raw.split(".", 1)[0]
    if "/" in raw:
        raw = raw.split("/", 1)[0]
    key = ALIASES.get(raw)
    if not key:
        return None
    return RESOURCES.get(key)


def resource_for_kind(kind: str) -> Resource | None:
    """The registry entry for an API kind (``Pod`` -> pods)."""
    key = KIND_TO_RESOURCE.get((kind or "").strip())
    return RESOURCES.get(key) if key else None


# --------------------------------------------------------------------------- #
# The HTTP client
# --------------------------------------------------------------------------- #

class ApiClient:
    """A small Kubernetes API client: read **and** write, over HTTPS.

    The watcher only ever reads; the tools read and write. Both go through
    this class, so there is one TLS setup, one token and one timeout policy in
    the whole platform.
    """

    def __init__(self, config: ApiConfig, *, timeout: int = 30):
        self.config = config
        self.timeout = timeout
        self._context: ssl.SSLContext | None = None

    # -- plumbing --------------------------------------------------------- #
    def _ssl_context(self) -> ssl.SSLContext:
        if self._context is not None:
            return self._context
        if self.config.ca_file:
            context = ssl.create_default_context(cafile=self.config.ca_file)
        elif self.config.verify:
            context = ssl.create_default_context()
        else:
            context = ssl._create_unverified_context()  # noqa: SLF001
        self._context = context
        return context

    def _url(self, path: str, params: dict | None = None) -> str:
        url = f"{self.config.base}{path}"
        query = urllib.parse.urlencode(
            {k: v for k, v in (params or {}).items() if v is not None})
        if query:
            url = f"{url}?{query}"
        return url

    def _headers(self, content_type: str = "", extra: dict | None = None) -> dict:
        headers = {
            "Authorization": f"Bearer {self.config.token}",
            "Accept": "application/json",
            "User-Agent": "auton-api/1",
        }
        if content_type:
            headers["Content-Type"] = content_type
        headers.update(extra or {})
        return headers

    def request(self, method: str, path: str, *, params: dict | None = None,
                body: bytes | str | None = None, content_type: str = "",
                headers: dict | None = None, timeout: int | None = None,
                raw: bool = False):
        """Send one request and return the response object.

        ``raw=True`` returns the response for streaming (logs, watches); the
        caller closes it. Otherwise the body is read and returned as bytes.
        """
        payload = body.encode("utf-8") if isinstance(body, str) else body
        request = urllib.request.Request(
            self._url(path, params), data=payload,
            headers=self._headers(content_type, headers), method=method.upper())
        try:
            response = urllib.request.urlopen(
                request, timeout=timeout or self.timeout,
                context=self._ssl_context())
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")
            except Exception:  # pragma: no cover - best effort
                pass
            message = f"the API server answered {exc.code}"
            reason = ""
            try:
                parsed = json.loads(detail or "{}")
                message = str(parsed.get("message") or message)
                reason = str(parsed.get("reason") or "")
            except (json.JSONDecodeError, AttributeError):
                if detail.strip():
                    message = detail.strip()[:400]
            if exc.code == 404:
                raise ApiNotFound(message, status=404, reason=reason,
                                  body=detail) from None
            if exc.code == 403:
                raise ApiForbidden(message, status=403, reason=reason,
                                   body=detail) from None
            if exc.code == 409:
                raise ApiConflict(message, status=409, reason=reason,
                                  body=detail) from None
            raise ApiError(message, status=exc.code, reason=reason,
                           body=detail) from None
        except urllib.error.URLError as exc:
            raise ApiUnavailable(
                f"cannot reach the API server: {exc.reason}") from None
        except (TimeoutError, socket.timeout):
            raise ApiUnavailable(
                f"the API server did not answer within "
                f"{timeout or self.timeout}s") from None
        except OSError as exc:
            raise ApiUnavailable(f"cannot reach the API server: {exc}") from None
        if raw:
            return response
        try:
            return response.read()
        finally:
            try:
                response.close()
            except Exception:  # pragma: no cover - best effort
                pass

    # -- reads ------------------------------------------------------------ #
    def get_json(self, path: str, params: dict | None = None,
                 timeout: int | None = None) -> dict:
        """GET one document and parse it as JSON."""
        body = self.request("GET", path, params=params, timeout=timeout)
        try:
            document = json.loads(body or b"{}")
        except json.JSONDecodeError as exc:
            raise ApiError(f"the API server sent invalid JSON: {exc}") from None
        if not isinstance(document, dict):
            raise ApiError("the API server sent an unexpected document")
        return document

    def get_text(self, path: str, params: dict | None = None,
                 timeout: int | None = None) -> str:
        """GET one document as text (the pod logs)."""
        body = self.request("GET", path, params=params, timeout=timeout)
        return body.decode("utf-8", "replace")

    def list(self, path: str, params: dict | None = None,
             timeout: int | None = None) -> tuple[list[dict], str]:
        """``list`` one collection: the items and the resourceVersion."""
        query = {"resourceVersion": "0"}
        query.update(params or {})
        document = self.get_json(path, query, timeout)
        metadata = document.get("metadata") or {}
        items = document.get("items")
        if not isinstance(items, list):
            items = []
        return items, str(metadata.get("resourceVersion") or "")

    def watch(self, path: str, resource_version: str,
              timeout_seconds: int = 300) -> Iterator[dict]:
        """Open a watch stream and yield the events as they arrive.

        The generator ends when the server closes the stream (which it does
        after ``timeout_seconds`` by design) or when the connection breaks.
        """
        params = {
            "watch": "true",
            "allowWatchBookmarks": "true",
            "timeoutSeconds": str(int(timeout_seconds)),
        }
        if resource_version:
            params["resourceVersion"] = str(resource_version)
        try:
            response = self.request("GET", path, params=params,
                                    timeout=timeout_seconds + 30, raw=True)
        except ApiError as exc:
            if exc.status == 410:
                raise WatchExpired(
                    "the resourceVersion is too old (410 Gone)") from None
            if exc.status in (401, 403):
                raise WatchForbidden(
                    f"the API server refused the request ({exc.status})"
                ) from None
            raise
        try:
            for raw in response:
                line = raw.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    # A truncated line is not fatal: the stream continues.
                    continue
                if isinstance(event, dict):
                    yield event
        finally:
            try:
                response.close()
            except Exception:  # pragma: no cover - best effort
                pass

    # -- writes ----------------------------------------------------------- #
    def post(self, path: str, body: str, *, content_type: str = "application/json",
             params: dict | None = None, timeout: int | None = None) -> dict:
        """POST a document (create)."""
        raw = self.request("POST", path, params=params, body=body,
                           content_type=content_type, timeout=timeout)
        return _parse(raw)

    def patch(self, path: str, body: str, *,
              patch_type: str = "merge", params: dict | None = None,
              timeout: int | None = None) -> dict:
        """PATCH a document.

        ``patch_type`` is ``merge``, ``strategic``, ``json`` or ``apply``
        (server-side apply, which sends YAML straight to the API server).
        """
        types = {
            "merge": "application/merge-patch+json",
            "strategic": "application/strategic-merge-patch+json",
            "json": "application/json-patch+json",
            "apply": "application/apply-patch+yaml",
        }
        content_type = types.get(patch_type, types["merge"])
        raw = self.request("PATCH", path, params=params, body=body,
                           content_type=content_type, timeout=timeout)
        return _parse(raw)

    def delete(self, path: str, *, params: dict | None = None,
               body: str | None = None, timeout: int | None = None) -> dict:
        """DELETE one object."""
        raw = self.request("DELETE", path, params=params, body=body,
                           content_type="application/json" if body else "",
                           timeout=timeout)
        return _parse(raw)


def _parse(raw: bytes) -> dict:
    if not raw:
        return {}
    try:
        document = json.loads(raw or b"{}")
    except json.JSONDecodeError:
        return {}
    return document if isinstance(document, dict) else {}


# --------------------------------------------------------------------------- #
# The shared client
# --------------------------------------------------------------------------- #

_CLIENT: ApiClient | None = None
_CLIENT_TRIED = False
_CLIENT_ERROR = ""

#: Counters, so the platform can prove which path is actually used.
COUNTERS: dict[str, int] = {"direct": 0, "direct_error": 0, "kubectl": 0,
                             "unavailable": 0}


def client() -> ApiClient | None:
    """The shared client, or ``None`` when there are no credentials."""
    global _CLIENT, _CLIENT_TRIED, _CLIENT_ERROR
    if _CLIENT_TRIED:
        return _CLIENT
    _CLIENT_TRIED = True
    if os.environ.get("AGENT_K8S_DIRECT", "1").strip().lower() in (
            "0", "false", "off", "no"):
        _CLIENT_ERROR = "the direct API client is disabled (AGENT_K8S_DIRECT=0)"
        COUNTERS["unavailable"] += 1
        return None
    config = resolve_config()
    if config is None:
        _CLIENT_ERROR = ("no API server credentials (not running in the "
                         "cluster, and AGENT_K8S_API/AGENT_K8S_TOKEN are unset)")
        COUNTERS["unavailable"] += 1
        return None
    try:
        _CLIENT = ApiClient(config)
    except Exception as exc:  # pragma: no cover - defensive
        _CLIENT_ERROR = f"could not build the API client: {exc}"
        COUNTERS["unavailable"] += 1
        return None
    return _CLIENT


def client_error() -> str:
    """Why there is no client (empty when there is one)."""
    client()
    return _CLIENT_ERROR


def reset_client() -> None:
    """Forget the cached client (the tests use it)."""
    global _CLIENT, _CLIENT_TRIED, _CLIENT_ERROR
    _CLIENT = None
    _CLIENT_TRIED = False
    _CLIENT_ERROR = ""


# --------------------------------------------------------------------------- #
# The exec channel (v4.channel.k8s.io over a WebSocket)
# --------------------------------------------------------------------------- #

def _ws_key() -> str:
    """A random Sec-WebSocket-Key."""
    return base64.b64encode(os.urandom(16)).decode("ascii")


def _ws_frame(payload: bytes, opcode: int = 0x1) -> bytes:
    """One masked WebSocket frame (a client must always mask)."""
    header = bytearray([0x80 | opcode])
    length = len(payload)
    if length < 126:
        header.append(0x80 | length)
    elif length < (1 << 16):
        header.append(0x80 | 126)
        header += struct.pack("!H", length)
    else:
        header.append(0x80 | 127)
        header += struct.pack("!Q", length)
    mask = os.urandom(4)
    header += mask
    masked = bytes(byte ^ mask[index % 4]
                   for index, byte in enumerate(payload))
    return bytes(header) + masked


def _ws_read(sock, buffer: bytes) -> tuple[bytes, bytes]:
    """Read one WebSocket frame; returns ``(payload, rest)``."""
    def need(count: int) -> None:
        nonlocal buffer
        while len(buffer) < count:
            chunk = sock.recv(65536)
            if not chunk:
                raise ApiUnavailable("the exec channel closed unexpectedly")
            buffer += chunk

    need(2)
    first, second = buffer[0], buffer[1]
    opcode = first & 0x0F
    length = second & 0x7F
    offset = 2
    if length == 126:
        need(4)
        length = struct.unpack("!H", buffer[2:4])[0]
        offset = 4
    elif length == 127:
        need(10)
        length = struct.unpack("!Q", buffer[2:10])[0]
        offset = 10
    masked = bool(second & 0x80)
    mask = b""
    if masked:
        need(offset + 4)
        mask = buffer[offset:offset + 4]
        offset += 4
    need(offset + length)
    payload = buffer[offset:offset + length]
    buffer = buffer[offset + length:]
    if masked:
        payload = bytes(byte ^ mask[index % 4]
                        for index, byte in enumerate(payload))
    if opcode == 0x8:  # close
        raise ApiUnavailable("the exec channel was closed")
    return payload, buffer


def _status_of(status_line: str) -> int:
    """The HTTP status of a response line (``HTTP/1.1 403 Forbidden``)."""
    parts = (status_line or "").split()
    for part in parts:
        if part.isdigit() and len(part) == 3:
            return int(part)
    return 0


def exec_command(pod: str, command: list[str], *, container: str = "",
                 namespace: str = "", timeout: int = 60,
                 client_obj: ApiClient | None = None) -> tuple[str, str]:
    """Run a command in a pod over the ``v4.channel.k8s.io`` WebSocket.

    Returns ``(stdout, stderr)``. The protocol is the Kubernetes remote
    command channel: the server sends binary frames whose first byte is the
    channel (1 = stdout, 2 = stderr, 3 = error) and the rest is the text.
    """
    api = client_obj or client()
    if api is None:
        raise ApiUnavailable(client_error() or "no API client")
    params = {
        "command": command,
        "container": container or "",
        "stdout": "true", "stderr": "true",
        "stdin": "false", "tty": "false",
    }
    path = f"/api/v1/namespaces/{namespace}/pods/{pod}/exec"
    query = urllib.parse.urlencode(params, doseq=True)
    key = _ws_key()
    parts = urllib.parse.urlsplit(api.config.base)
    host = parts.hostname or "kubernetes.default.svc"
    port = parts.port or 443
    raw_sock = socket.create_connection((host, port), timeout=timeout)
    if parts.scheme == "https":
        context = api._ssl_context()  # noqa: SLF001 - same TLS policy
        sock = context.wrap_socket(raw_sock, server_hostname=host)
    else:
        sock = raw_sock
    try:
        request = (
            f"GET {path}?{query} HTTP/1.1\r\n"
            f"Host: {parts.netloc}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            f"Sec-WebSocket-Protocol: v4.channel.k8s.io\r\n"
            f"Authorization: Bearer {api.config.token}\r\n"
            "User-Agent: auton-api/1\r\n"
            "\r\n")
        sock.sendall(request.encode("utf-8"))
        buffer = b""
        # The handshake answer.
        while b"\r\n\r\n" not in buffer:
            chunk = sock.recv(4096)
            if not chunk:
                raise ApiUnavailable("the exec channel did not answer")
            buffer += chunk
        head, buffer = buffer.split(b"\r\n\r\n", 1)
        status_line = head.split(b"\r\n", 1)[0].decode("utf-8", "replace")
        if " 101 " not in status_line:
            # The API server refused the upgrade. A refusal is a final answer
            # (RBAC said no), so it gets the same error class as a normal
            # request — the caller must not fall back to kubectl for it.
            status = _status_of(status_line)
            message = f"the exec channel refused the upgrade ({status_line.strip()})"
            if status == 403:
                raise ApiForbidden(message, status=403) from None
            if status == 404:
                raise ApiNotFound(message, status=404) from None
            if status:
                raise ApiError(message, status=status) from None
            raise ApiUnavailable(message) from None
        out, err = "", ""
        deadline = time.time() + max(5, int(timeout))
        while time.time() < deadline:
            sock.settimeout(max(1, min(10, int(deadline - time.time()))))
            try:
                payload, buffer = _ws_read(sock, buffer)
            except (TimeoutError, socket.timeout):
                continue
            except OSError:
                break
            if not payload:
                continue
            channel = payload[0]
            text = payload[1:].decode("utf-8", "replace")
            if channel == 1:
                out += text
            elif channel == 2:
                err += text
            elif channel == 3:
                err += text
                break
        return out, err
    finally:
        try:
            sock.close()
        except Exception:  # pragma: no cover - best effort
            pass


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def digest(value: str) -> str:
    """A short, stable digest of a value (for the audit records)."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def selector_matches(labels: dict | None, selector: str) -> bool:
    """Does a label set match a ``key=value`` selector?

    Only equality is supported — the same subset kubectl's field selector
    supports for these tools. An empty selector matches everything.
    """
    if not selector:
        return True
    labels = labels or {}
    for term in selector.split(","):
        term = term.strip()
        if not term:
            continue
        if "=" not in term:
            return False
        key, _, value = term.partition("=")
        if str(labels.get(key.strip(), "")) != value.strip():
            return False
    return True


_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$")


def valid_name(name: str) -> bool:
    """Is this a safe object name (no path traversal, no flags)?"""
    return bool(_NAME_RE.match((name or "").strip()))
