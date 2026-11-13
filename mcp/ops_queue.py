#!/usr/bin/env python3
"""Storage primitives for the task queue.

The queue has two interchangeable backends with the same tiny interface:

  MemoryBackend  in-process dicts/lists — local development and the test suite
  RedisBackend   Redis (Streams + ZSET + strings) — the cluster

Everything with *semantics* (task states, leases, retries, locks, budgets) lives
in `task_store.py` exactly once, so memory and Redis cannot drift apart. A
backend only has to get these primitives right:

  strings   set/get/delete/set_nx/incr/expire/ttl
  hashes    hset/hgetall
  sorted    zadd/zrange/zrem/zcard
  streams   xadd/xreadgroup/xack/xautoclaim/xlen
  keys      scan (stats and pruning only)

Streams are used as a work queue with a consumer group in Redis. Recovery does
**not** depend on the pending-entries list: `TaskStore.claim()` first sweeps a
`claimed` ZSET for expired leases, which works identically in both backends.
A message redelivered by `XAUTOCLAIM` is therefore harmless — its record is no
longer `queued`, so it is skipped.
"""
from __future__ import annotations

import fnmatch
import itertools
import os
import threading
import time


class QueueError(RuntimeError):
    """Backend unavailable; callers must fail closed (no un-audited mutation)."""


# --------------------------------------------------------------------------- #
# In-memory backend
# --------------------------------------------------------------------------- #

class MemoryBackend:
    """Thread-safe dict/list implementation of the primitive interface."""

    name = "memory"

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._values: dict[str, str] = {}
        self._expires: dict[str, float] = {}
        self._hashes: dict[str, dict[str, str]] = {}
        self._zsets: dict[str, list[tuple[float, str]]] = {}
        self._streams: dict[str, list[tuple[str, dict[str, str]]]] = {}
        self._seq = itertools.count(1)

    # -- strings ---------------------------------------------------------- #
    def _expired(self, key: str) -> bool:
        deadline = self._expires.get(key)
        if deadline is not None and deadline <= time.time():
            self._values.pop(key, None)
            self._expires.pop(key, None)
            return True
        return False

    def set(self, key: str, value: str) -> None:
        with self._lock:
            self._values[key] = str(value)

    def get(self, key: str) -> str | None:
        with self._lock:
            if self._expired(key):
                return None
            return self._values.get(key)

    def delete(self, key: str) -> None:
        with self._lock:
            self._values.pop(key, None)
            self._expires.pop(key, None)
            self._hashes.pop(key, None)
            self._zsets.pop(key, None)
            self._streams.pop(key, None)

    def set_nx(self, key: str, value: str, ttl_ms: int = 0) -> bool:
        with self._lock:
            if not self._expired(key) and key in self._values:
                return False
            self._values[key] = str(value)
            if ttl_ms > 0:
                self._expires[key] = time.time() + ttl_ms / 1000.0
            return True

    def incr(self, key: str) -> int:
        with self._lock:
            if self._expired(key):
                value = 0
            else:
                value = int(self._values.get(key, "0") or 0)
            value += 1
            self._values[key] = str(value)
            return value

    def expire(self, key: str, ttl_ms: int) -> None:
        with self._lock:
            if ttl_ms > 0:
                self._expires[key] = time.time() + ttl_ms / 1000.0

    def ttl(self, key: str) -> int:
        with self._lock:
            if key not in self._values and key not in self._hashes \
                    and key not in self._zsets and key not in self._streams:
                return -2
            deadline = self._expires.get(key)
            if deadline is None:
                return -1
            return max(0, int((deadline - time.time()) * 1000))

    # -- hashes ----------------------------------------------------------- #
    def hset(self, key: str, mapping: dict[str, str]) -> None:
        with self._lock:
            bucket = self._hashes.setdefault(key, {})
            bucket.update({str(k): str(v) for k, v in mapping.items()})

    def hgetall(self, key: str) -> dict[str, str]:
        with self._lock:
            return dict(self._hashes.get(key, {}))

    # -- sorted sets ------------------------------------------------------ #
    def zadd(self, key: str, score: float, member: str) -> None:
        with self._lock:
            bucket = self._zsets.setdefault(key, [])
            bucket[:] = [item for item in bucket if item[1] != member]
            bucket.append((float(score), str(member)))
            bucket.sort()

    def zrange(self, key: str, start: int = 0, stop: int = -1) -> list[str]:
        with self._lock:
            bucket = sorted(self._zsets.get(key, []))
            if stop == -1:
                return [member for _, member in bucket[start:]]
            return [member for _, member in bucket[start:stop + 1]]

    def zrangebyscore(self, key: str, low: float, high: float,
                      limit: int = 0) -> list[str]:
        with self._lock:
            hits = [member for score, member in sorted(self._zsets.get(key, []))
                    if low <= score <= high]
            return hits[:limit] if limit else hits

    def zrem(self, key: str, member: str) -> None:
        with self._lock:
            bucket = self._zsets.get(key)
            if bucket:
                bucket[:] = [item for item in bucket if item[1] != member]

    def zcard(self, key: str) -> int:
        with self._lock:
            return len(self._zsets.get(key, []))

    # -- streams ---------------------------------------------------------- #
    def xadd(self, key: str, fields: dict[str, str],
             maxlen: int = 0) -> str:
        with self._lock:
            stream = self._streams.setdefault(key, [])
            stream_id = f"{int(time.time() * 1000)}-{next(self._seq)}"
            stream.append((stream_id, {str(k): str(v)
                                       for k, v in fields.items()}))
            if maxlen and len(stream) > maxlen:
                del stream[:-maxlen]
            return stream_id

    def xreadgroup(self, group: str, consumer: str, streams: list[str],
                   count: int = 1, block_ms: int = 0) -> list[tuple[str, list]]:
        """Pop available entries. No pending list: recovery is lease-based."""
        del group, consumer, block_ms
        out: list[tuple[str, list]] = []
        with self._lock:
            for key in streams:
                stream = self._streams.get(key) or []
                taken, stream[:] = stream[:count], stream[count:]
                if taken:
                    out.append((key, taken))
        return out

    def xack(self, key: str, group: str, stream_id: str) -> None:
        del key, group, stream_id

    def xdel(self, key: str, stream_id: str) -> None:
        del key, stream_id

    def xautoclaim(self, key: str, group: str, consumer: str,
                   min_idle_ms: int, count: int = 10) -> list:
        del key, group, consumer, min_idle_ms, count
        return []

    def xlen(self, key: str) -> int:
        with self._lock:
            return len(self._streams.get(key, []))

    def xrange(self, key: str, count: int = 0) -> list[tuple[str, dict]]:
        with self._lock:
            entries = list(self._streams.get(key, []))
            return entries[-count:] if count else entries

    # -- keys ------------------------------------------------------------- #
    def scan(self, pattern: str) -> list[str]:
        with self._lock:
            keys = set(self._values) | set(self._hashes) | set(self._zsets) \
                | set(self._streams)
            return [k for k in sorted(keys) if fnmatch.fnmatch(k, pattern)]


# --------------------------------------------------------------------------- #
# Redis backend
# --------------------------------------------------------------------------- #

class RedisBackend:
    """Redis implementation of the same primitives (lazy `redis` import)."""

    name = "redis"

    def __init__(self, url: str = "", password: str = "", timeout: float = 5.0,
                 client=None) -> None:
        if client is not None:
            self._client = client
            return
        try:
            import redis  # imported here so a memory-only install stays light
        except ImportError as exc:  # pragma: no cover - image installs it
            raise QueueError("the 'redis' package is not installed; "
                             "AGENT_QUEUE=redis needs it") from exc
        self._url = url or os.environ.get("AGENT_REDIS_URL",
                                          "redis://redis:6379/0")
        try:
            self._client = redis.Redis.from_url(
                self._url, password=password or None, decode_responses=True,
                socket_timeout=timeout, socket_connect_timeout=timeout,
                health_check_interval=30)
        except Exception as exc:  # pragma: no cover - bad URL
            raise QueueError(f"cannot configure Redis: {exc}") from exc

    def ping(self) -> bool:
        try:
            return bool(self._client.ping())
        except Exception as exc:
            raise QueueError(f"Redis is unreachable: {exc}") from exc

    # -- strings ---------------------------------------------------------- #
    def set(self, key: str, value: str) -> None:
        self._client.set(key, value)

    def get(self, key: str) -> str | None:
        return self._client.get(key)

    def delete(self, key: str) -> None:
        self._client.delete(key)

    def set_nx(self, key: str, value: str, ttl_ms: int = 0) -> bool:
        return bool(self._client.set(key, value, nx=True,
                                     px=ttl_ms if ttl_ms > 0 else None))

    def incr(self, key: str) -> int:
        return int(self._client.incr(key))

    def expire(self, key: str, ttl_ms: int) -> None:
        self._client.pexpire(key, ttl_ms)

    def ttl(self, key: str) -> int:
        return int(self._client.pttl(key))

    # -- hashes ----------------------------------------------------------- #
    def hset(self, key: str, mapping: dict[str, str]) -> None:
        self._client.hset(key, mapping={str(k): str(v)
                                        for k, v in mapping.items()})

    def hgetall(self, key: str) -> dict[str, str]:
        return dict(self._client.hgetall(key) or {})

    # -- sorted sets ------------------------------------------------------ #
    def zadd(self, key: str, score: float, member: str) -> None:
        self._client.zadd(key, {member: float(score)})

    def zrange(self, key: str, start: int = 0, stop: int = -1) -> list[str]:
        return list(self._client.zrange(key, start, stop))

    def zrangebyscore(self, key: str, low: float, high: float,
                      limit: int = 0) -> list[str]:
        if limit:
            return list(self._client.zrangebyscore(key, low, high,
                                                   start=0, num=limit))
        return list(self._client.zrangebyscore(key, low, high))

    def zrem(self, key: str, member: str) -> None:
        self._client.zrem(key, member)

    def zcard(self, key: str) -> int:
        return int(self._client.zcard(key))

    # -- streams ---------------------------------------------------------- #
    def xadd(self, key: str, fields: dict[str, str],
             maxlen: int = 0) -> str:
        kwargs = {"maxlen": maxlen, "approximate": True} if maxlen else {}
        return self._client.xadd(key, {str(k): str(v)
                                       for k, v in fields.items()}, **kwargs)

    def xreadgroup(self, group: str, consumer: str, streams: list[str],
                   count: int = 1, block_ms: int = 0) -> list[tuple[str, list]]:
        for key in streams:
            try:
                self._client.xgroup_create(key, group, id="0", mkstream=True)
            except Exception as exc:  # BUSYGROUP is expected after the first run
                if "BUSYGROUP" not in str(exc):
                    raise
        response = self._client.xreadgroup(
            group, consumer, {key: ">" for key in streams},
            count=count, block=block_ms or None)
        return [(key, entries) for key, entries in (response or [])]

    def xack(self, key: str, group: str, stream_id: str) -> None:
        self._client.xack(key, group, stream_id)

    def xdel(self, key: str, stream_id: str) -> None:
        # Redis keeps acknowledged entries in the stream until they are deleted,
        # so XLEN alone would never report a shrinking queue.
        self._client.xdel(key, stream_id)

    def xautoclaim(self, key: str, group: str, consumer: str,
                   min_idle_ms: int, count: int = 10) -> list:
        try:
            result = self._client.xautoclaim(
                key, group, consumer, min_idle_time=min_idle_ms, count=count)
        except Exception:
            return []
        # redis-py returns (next_id, entries, deleted_ids) on newer servers and
        # (next_id, entries) on older ones.
        entries = result[1] if len(result) > 1 else []
        return entries or []

    def xlen(self, key: str) -> int:
        return int(self._client.xlen(key))

    def xrange(self, key: str, count: int = 0) -> list[tuple[str, dict]]:
        kwargs = {"count": count} if count else {}
        return [(stream_id, fields)
                for stream_id, fields in self._client.xrange(key, "-", "+",
                                                             **kwargs)]

    # -- keys ------------------------------------------------------------- #
    def scan(self, pattern: str) -> list[str]:
        return [key for key in self._client.scan_iter(match=pattern, count=200)]


# --------------------------------------------------------------------------- #
# Factory
# --------------------------------------------------------------------------- #

def create_backend(kind: str = "", client=None):
    """Build the configured backend; raises QueueError if it is unusable.

    `AGENT_QUEUE` selects it: `memory` (default for local development/tests) or
    `redis` (the cluster). There is no silent fallback from redis to memory:
    two API pods with separate in-memory queues would look healthy and lose
    work.
    """
    kind = (kind or os.environ.get("AGENT_QUEUE", "memory")).strip().lower()
    if kind == "memory":
        return MemoryBackend()
    if kind != "redis":
        raise QueueError(f"unknown AGENT_QUEUE={kind!r} (use memory or redis)")
    password = os.environ.get("AGENT_REDIS_PASSWORD", "").strip()
    key_file = os.environ.get("AGENT_REDIS_PASSWORD_FILE", "").strip()
    if key_file:
        try:
            with open(key_file, encoding="utf-8") as fh:
                password = fh.read().strip()
        except OSError as exc:
            raise QueueError(f"cannot read AGENT_REDIS_PASSWORD_FILE: {exc}") from exc
    backend = RedisBackend(url=os.environ.get("AGENT_REDIS_URL", ""),
                           password=password, client=client)
    backend.ping()
    return backend
