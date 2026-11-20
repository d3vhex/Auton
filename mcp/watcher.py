#!/usr/bin/env python3
"""The Kubernetes watcher: event-driven detection instead of blind polling.

Until now the scanner asked the API server the same question every 60 seconds:
*what does this namespace look like right now?* The answer was a full snapshot
of pods, deployments, statefulsets and PVCs, parsed five times a minute by
every scanner replica. A pod that was OOMKilled at second 3 was noticed at
second 60, and the API server paid for five full listings per minute per
replica.

This module replaces that with a real **watch**: one long-lived HTTPS stream
per resource, opened with the ``resourceVersion`` the API server handed us, so
the server pushes only what changed.

    ┌──────────┐   list()                    ┌──────────────────┐
    │ watcher  │ ──────────────────────────▶ │ api-server       │
    │          │   watch(rv=12345)            │                  │
    │          │ ◀────────────────────────── │ ADDED/MODIFIED/  │
    └──────────┘   event stream              │ DELETED/BOOKMARK │
                 ▲                           └──────────────────┘
                 │ reconnect with backoff
                 └── 410 Gone ──▶ re-list ──▶ new resourceVersion

Three rules shape the implementation:

**A real resourceVersion.** The watcher never re-lists to "catch up". It keeps
the last ``resourceVersion`` it saw and resumes the stream from exactly there.
The API server may close a watch at any time; that is normal, not an error.

**Failures are counted, not hidden.** Every reconnect, every expired
resourceVersion and every error is a counter, and :meth:`Watcher.stats` exposes
them. A watcher that silently stops watching is the worst possible outcome for
a self-healing platform, so "is it still connected?" must be answerable from
the outside.

**Degrade, never crash.** The watcher is an *optimisation*: it makes detection
faster and cheaper. If the API server is unreachable, the watcher reports
``available: False`` and the caller falls back to polling. Detection must never
stop because a watch stream did.

Only the standard library is used: :mod:`urllib` for HTTPS, :mod:`ssl` for the
cluster CA, :mod:`json` for the event stream. There is no client-go, no
websocket, no extra dependency to install in the image.

The HTTP plumbing itself lives in :mod:`k8s_api` — the same client the tools
use for their reads and writes — so the platform has one TLS setup, one token
and one timeout policy. The classes are re-exported here, so this module's
public surface did not change when the client moved.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Iterator

from k8s_api import (  # noqa: F401 - re-exported on purpose
    ApiClient as _ApiClient,
    ApiConfig,
    ApiError,
    ApiForbidden,
    ApiNotFound,
    ApiUnavailable,
    WatchError,
    WatchExpired,
    WatchForbidden,
)
import k8s_api as _k8s_api

#: Where the kubelet projects the pod's ServiceAccount credentials.
SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"

#: The API resources the platform watches, and their collection paths.
RESOURCES: dict[str, str] = {
    "pods": "/api/v1/namespaces/{namespace}/pods",
    "events": "/api/v1/namespaces/{namespace}/events",
    "persistentvolumeclaims":
        "/api/v1/namespaces/{namespace}/persistentvolumeclaims",
    "deployments": "/apis/apps/v1/namespaces/{namespace}/deployments",
    "replicasets": "/apis/apps/v1/namespaces/{namespace}/replicasets",
    "statefulsets": "/apis/apps/v1/namespaces/{namespace}/statefulsets",
}

#: The event types the API server sends.
EVENT_TYPES = ("ADDED", "MODIFIED", "DELETED", "BOOKMARK", "ERROR")

#: The default reconnect backoff, in seconds. The last value repeats.
DEFAULT_BACKOFF = (1.0, 2.0, 5.0, 10.0, 30.0)


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
#
# ApiConfig, in_cluster_config, config_from_env and resolve_config now live in
# k8s_api (imported above) — the tools and the watcher share one client. The
# wrappers below keep this module's own surface: they use *this* module's
# SA_DIR, so a caller (or a test) can point the credential lookup elsewhere.

def in_cluster_config() -> ApiConfig | None:
    """The credentials the kubelet projects into every pod."""
    return _k8s_api.in_cluster_config(SA_DIR)


def config_from_env() -> ApiConfig | None:
    """An explicit configuration, for a host run against a remote cluster."""
    return _k8s_api.config_from_env()


def resolve_config() -> ApiConfig | None:
    """The best available configuration, or ``None``."""
    return config_from_env() or in_cluster_config()


# The watch errors (WatchError, WatchExpired, WatchForbidden) are imported
# from k8s_api above — they belong to the transport, and the watcher re-
# exports them so its public surface did not change.


# --------------------------------------------------------------------------- #
# The API client
# --------------------------------------------------------------------------- #

class ApiClient(_ApiClient):
    """The shared client, with the watcher's own error vocabulary.

    The HTTP plumbing lives in :mod:`k8s_api`; the watcher only needs to turn
    an HTTP status into "expired" or "forbidden", because those two have a
    very specific meaning here: a 410 means *re-list and restart*, and a 403
    means *stop trying, the ServiceAccount may not watch this*.
    """

    @staticmethod
    def _watch_error(exc: ApiError) -> WatchError:
        if exc.status == 410:
            return WatchExpired(
                "the resourceVersion is too old (410 Gone)")
        if exc.status in (401, 403):
            return WatchForbidden(
                f"the API server refused the request ({exc.status})")
        return WatchError(str(exc))

    def list(self, path: str, params: dict | None = None,
             timeout: int | None = None) -> tuple[list[dict], str]:
        """``list`` one collection: the items and the resourceVersion."""
        try:
            return super().list(path, params, timeout)
        except ApiError as exc:
            raise self._watch_error(exc) from None

    def watch(self, path: str, resource_version: str,
              timeout_seconds: int = 300) -> Iterator[dict]:
        """Open a watch stream and yield the events as they arrive."""
        try:
            yield from super().watch(path, resource_version, timeout_seconds)
        except ApiError as exc:
            raise self._watch_error(exc) from None


# --------------------------------------------------------------------------- #
# The watcher
# --------------------------------------------------------------------------- #

class Watcher:
    """One watch stream for one resource, with backoff and counters.

    The watcher is a thread. :meth:`run` blocks until ``stop`` is set; it
    never raises out of the loop, because a watcher that dies takes the
    detection latency with it.
    """

    def __init__(self, client: ApiClient, resource: str, namespace: str = "",
                 *, name: str = "", timeout_seconds: int = 300,
                 backoff: tuple[float, ...] = DEFAULT_BACKOFF,
                 cache: "ObjectCache | None" = None):
        self.client = client
        self.resource = resource
        self.namespace = namespace
        self.name = name or f"{resource}/{namespace}"
        self.timeout_seconds = max(10, int(timeout_seconds))
        self.backoff = tuple(backoff) or DEFAULT_BACKOFF
        self.cache = cache

        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._resource_version = ""
        self._counters = {
            "lists": 0,
            "watches": 0,
            "events": 0,
            "bookmarks": 0,
            "errors": 0,
            "reconnects": 0,
            "expired": 0,
            "forbidden": 0,
        }
        self._last_error = ""
        self._last_event_at = 0.0
        self._last_connect_at = 0.0
        self._started_at = 0.0

    # -- state ------------------------------------------------------------ #
    @property
    def resource_version(self) -> str:
        with self._lock:
            return self._resource_version

    def stop(self) -> None:
        self._stop.set()

    def stopped(self) -> bool:
        return self._stop.is_set()

    def path(self) -> str:
        template = RESOURCES.get(self.resource)
        if not template:
            raise WatchError(f"unknown resource {self.resource!r}")
        return template.format(namespace=self.namespace)

    def _bump(self, counter: str, amount: int = 1) -> None:
        with self._lock:
            self._counters[counter] = self._counters.get(counter, 0) + amount

    def _set_resource_version(self, value: str) -> None:
        if not value:
            return
        with self._lock:
            self._resource_version = value

    # -- the loop --------------------------------------------------------- #
    def prime(self) -> list[dict]:
        """Take the initial list and remember its resourceVersion."""
        items, version = self.client.list(self.path())
        self._set_resource_version(version)
        self._bump("lists")
        if self.cache is not None:
            self.cache.replace(self.resource, items)
        return items

    def run(self, on_event=None) -> None:
        """Watch until stopped. Never raises."""
        self._started_at = time.time()
        attempt = 0
        primed = False
        while not self._stop.is_set():
            try:
                if not primed:
                    self.prime()
                    primed = True
                self._bump("watches")
                self._last_connect_at = time.time()
                for event in self.client.watch(self.path(),
                                               self.resource_version,
                                               self.timeout_seconds):
                    if self._stop.is_set():
                        break
                    self._handle(event, on_event)
                # The server closed the stream (it does that on purpose).
                attempt = 0
                self._bump("reconnects")
            except WatchExpired:
                # The resourceVersion is too old: the only correct answer is
                # a fresh list, which gives us a new version to watch from.
                self._bump("expired")
                self._bump("reconnects")
                self._bump("errors")
                self._last_error = "the resourceVersion expired; re-listed"
                primed = False
                attempt = 0
            except WatchForbidden as exc:
                # A permission problem does not fix itself by retrying.
                self._bump("forbidden")
                self._bump("errors")
                self._last_error = str(exc)
                self._stop.wait(self.backoff[0])
                attempt = len(self.backoff) - 1
            except WatchError as exc:
                self._bump("errors")
                self._bump("reconnects")
                self._last_error = str(exc)
                attempt = min(attempt + 1, len(self.backoff) - 1)
                self._stop.wait(self.backoff[attempt])
            except Exception as exc:  # a loop must never die
                self._bump("errors")
                self._bump("reconnects")
                self._last_error = f"{type(exc).__name__}: {exc}"
                attempt = min(attempt + 1, len(self.backoff) - 1)
                self._stop.wait(self.backoff[attempt])

    def _handle(self, event: dict, on_event) -> None:
        """Process one event from the stream."""
        kind = str(event.get("type") or "").upper()
        if kind not in EVENT_TYPES:
            return
        obj = event.get("object")
        obj = obj if isinstance(obj, dict) else {}
        version = str((obj.get("metadata") or {}).get("resourceVersion") or "")
        self._set_resource_version(version)

        if kind == "BOOKMARK":
            self._bump("bookmarks")
            return
        if kind == "ERROR":
            code = obj.get("code")
            self._bump("errors")
            self._last_error = f"the API server sent an ERROR event ({code})"
            if int(code or 0) == 410:
                raise WatchExpired(self._last_error)
            return

        self._bump("events")
        self._last_event_at = time.time()
        if self.cache is not None:
            try:
                self.cache.apply(self.resource, kind, obj)
            except Exception as exc:  # a bad cache must not kill the watch
                self._bump("errors")
                self._last_error = f"the cache update failed: {exc}"
        if on_event is not None:
            try:
                on_event(kind, obj)
            except Exception as exc:  # a bad handler must not kill the watch
                self._bump("errors")
                self._last_error = f"the event handler failed: {exc}"

    # -- observability ---------------------------------------------------- #
    def stats(self) -> dict:
        """Everything a human needs to know whether this watcher is alive."""
        with self._lock:
            counters = dict(self._counters)
            version = self._resource_version
            last_error = self._last_error
            last_event_at = self._last_event_at
            last_connect_at = self._last_connect_at
        now = time.time()
        age = round(now - last_event_at, 1) if last_event_at else None
        connected = bool(last_connect_at) and (now - last_connect_at) < \
            (self.timeout_seconds * 2 + 60)
        return {
            "name": self.name,
            "resource": self.resource,
            "namespace": self.namespace,
            "available": not counters["forbidden"] and counters["lists"] > 0,
            "connected": connected,
            "resource_version": version,
            "last_event_age_seconds": age,
            "last_connect_age_seconds":
                round(now - last_connect_at, 1) if last_connect_at else None,
            "uptime_seconds": round(now - self._started_at, 1)
            if self._started_at else 0,
            "last_error": last_error,
            **counters,
        }


# --------------------------------------------------------------------------- #
# The cache
# --------------------------------------------------------------------------- #

class ObjectCache:
    """The live state of the watched objects, kept from the event stream.

    The cache is the point of the whole exercise: detection reads it instead
    of asking the API server for a snapshot.

    Objects are keyed by ``(resource, namespace, name)``. The resource is part
    of the key on purpose: a Pod and a Deployment routinely share a name, and
    a cache that let one overwrite the other would show the fast path a
    Deployment where it expected a Pod.

    Some resources are **high churn**: Events come and go all day, and the
    API server only garbage-collects them after about an hour. A cache that
    kept every event forever would grow without bound, so a resource may have
    a time-to-live — after it, the object is no longer reported and is
    dropped by :meth:`prune`.
    """

    def __init__(self, ttl_seconds: dict | None = None) -> None:
        self._objects: dict[tuple[str, str, str], dict] = {}
        self._stamps: dict[tuple[str, str, str], float] = {}
        self._ttl: dict[str, float] = {str(k): float(v)
                                       for k, v in (ttl_seconds or {}).items()
                                       if float(v) > 0}
        self._lock = threading.Lock()

    def _expired(self, resource: str, stamp: float) -> bool:
        ttl = self._ttl.get(resource)
        if not ttl:
            return False
        return (time.time() - stamp) > ttl

    def apply(self, resource: str, event_type: str,
              obj: dict) -> tuple[str, str] | None:
        """Apply one event. Returns ``(namespace, name)`` or ``None``."""
        metadata = obj.get("metadata") or {}
        name = str(metadata.get("name") or "")
        namespace = str(metadata.get("namespace") or "")
        if not name:
            return None
        key = (resource, namespace, name)
        with self._lock:
            if event_type == "DELETED":
                self._objects.pop(key, None)
                self._stamps.pop(key, None)
            else:
                self._objects[key] = obj
                self._stamps[key] = time.time()
        return namespace, name

    def replace(self, resource: str, items: list[dict]) -> int:
        """Replace everything cached for one resource with a fresh list.

        Called after the initial list and after a ``410 Gone``, where the
        stream's history was dropped and the cache has to be re-based.
        """
        namespace = ""
        fresh: list[tuple[tuple[str, str, str], dict]] = []
        for obj in items:
            metadata = obj.get("metadata") or {}
            name = str(metadata.get("name") or "")
            if not name:
                continue
            namespace = namespace or str(metadata.get("namespace") or "")
            fresh.append(((resource, str(metadata.get("namespace") or ""),
                           name), obj))
        now = time.time()
        with self._lock:
            for key in [key for key in self._objects
                        if key[0] == resource and key[1] == namespace]:
                self._objects.pop(key, None)
                self._stamps.pop(key, None)
            for key, obj in fresh:
                self._objects[key] = obj
                self._stamps[key] = now
        return len(fresh)

    def objects(self, resource: str, namespace: str = "") -> list[dict]:
        """The cached objects of one resource (all namespaces when empty).

        An object past its TTL is **not** reported: it is stale, and a stale
        event is worse than no event.
        """
        now = time.time()
        with self._lock:
            items = list(self._objects.items())
            stamps = dict(self._stamps)
        out = []
        for (res, ns, _), obj in items:
            if res != resource or (namespace and ns != namespace):
                continue
            ttl = self._ttl.get(res)
            if ttl and (now - stamps.get((res, ns, _), now)) > ttl:
                continue
            out.append(obj)
        out.sort(key=lambda obj: str((obj.get("metadata") or {}).get("name")
                                     or ""))
        return out

    def names(self, resource: str, namespace: str = "") -> set[str]:
        with self._lock:
            items = list(self._objects.items())
        return {name for (res, ns, name), _ in items
                if res == resource and (not namespace or ns == namespace)}

    def prune(self) -> int:
        """Drop the objects that are past their TTL. Returns how many."""
        now = time.time()
        with self._lock:
            stale = [key for key, stamp in self._stamps.items()
                     if (now - stamp) > self._ttl.get(key[0], 0)]
            for key in stale:
                self._objects.pop(key, None)
                self._stamps.pop(key, None)
        return len(stale)

    def size(self) -> int:
        with self._lock:
            return len(self._objects)

    def clear(self) -> None:
        with self._lock:
            self._objects.clear()
            self._stamps.clear()
