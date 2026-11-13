"""A tiny in-memory Redis double for tests.

Only the commands `ops_queue.RedisBackend` issues are implemented, with the
same semantics the store relies on: streams with consumer groups (`>`
delivery, pending entries, `XAUTOCLAIM`), `XLEN`-shrinking `XDEL`, TTLs, and
sorted-set ordering by `(score, member)`.

The point is coverage, not fidelity: every test in `test_task_store.py` runs
against both `MemoryBackend` and `RedisBackend(FakeRedis())`, so the two
backends cannot drift apart in the code paths the store uses.
"""
from __future__ import annotations

import fnmatch
import time


def _id_parts(stream_id: str) -> tuple[int, int]:
    ms, _, seq = str(stream_id).partition("-")
    try:
        return int(ms), int(seq or 0)
    except ValueError:
        return 0, 0


class FakeRedis:
    def __init__(self) -> None:
        self._strings: dict[str, str] = {}
        self._hashes: dict[str, dict[str, str]] = {}
        self._zsets: dict[str, dict[str, float]] = {}
        self._streams: dict[str, list[tuple[str, dict[str, str]]]] = {}
        self._expiry: dict[str, float] = {}
        self._groups: dict[tuple[str, str], dict] = {}
        self._seq = 0

    # -- helpers ---------------------------------------------------------- #
    @staticmethod
    def _now_ms() -> int:
        return int(time.time() * 1000)

    def _drop(self, key: str) -> None:
        for bucket in (self._strings, self._hashes, self._zsets,
                       self._streams, self._expiry):
            bucket.pop(key, None)

    def _alive(self, key: str) -> bool:
        deadline = self._expiry.get(key)
        exists = (key in self._strings or key in self._hashes
                  or key in self._zsets or key in self._streams)
        if not exists:
            return False
        if deadline is not None and deadline <= self._now_ms():
            self._drop(key)
            return False
        return True

    # -- strings ---------------------------------------------------------- #
    def set(self, key: str, value: str, nx: bool = False, px: int | None = None):
        if nx and self._alive(key):
            return None
        self._drop(key)
        self._strings[key] = str(value)
        if px:
            self._expiry[key] = self._now_ms() + int(px)
        return True

    def get(self, key: str) -> str | None:
        return self._strings.get(key) if self._alive(key) else None

    def delete(self, *keys: str) -> int:
        removed = 0
        for key in keys:
            if self._alive(key):
                removed += 1
            self._drop(key)
        return removed

    def incr(self, key: str) -> int:
        current = int(self.get(key) or 0) if self._alive(key) else 0
        # INCR preserves the existing TTL; assign the string directly.
        self._strings[key] = str(current + 1)
        return current + 1

    def pexpire(self, key: str, ms: int) -> bool:
        if not self._alive(key):
            return False
        self._expiry[key] = self._now_ms() + int(ms)
        return True

    def pttl(self, key: str) -> int:
        if not self._alive(key):
            return -2
        deadline = self._expiry.get(key)
        return -1 if deadline is None else max(0, deadline - self._now_ms())

    # -- hashes ----------------------------------------------------------- #
    def hset(self, key: str, mapping: dict[str, str]) -> int:
        bucket = self._hashes.setdefault(key, {})
        added = 0
        for field, value in mapping.items():
            if field not in bucket:
                added += 1
            bucket[str(field)] = str(value)
        return added

    def hgetall(self, key: str) -> dict[str, str]:
        return dict(self._hashes.get(key, {})) if self._alive(key) else {}

    # -- sorted sets ------------------------------------------------------ #
    def zadd(self, key: str, mapping: dict[str, float]) -> int:
        bucket = self._zsets.setdefault(key, {})
        added = 0
        for member, score in mapping.items():
            if member not in bucket:
                added += 1
            bucket[str(member)] = float(score)
        return added

    def _members(self, key: str) -> list[str]:
        if not self._alive(key):
            return []
        bucket = self._zsets.get(key, {})
        return [member for member, _score in
                sorted(bucket.items(), key=lambda item: (item[1], item[0]))]

    def zrange(self, key: str, start: int = 0, stop: int = -1) -> list[str]:
        members = self._members(key)
        size = len(members)
        if start < 0:
            start = max(size + start, 0)
        if stop < 0:
            stop = size + stop
        return members[start:stop + 1]

    def zrangebyscore(self, key: str, low: float, high: float,
                      start: int | None = None, num: int | None = None) -> list[str]:
        if not self._alive(key):
            return []
        bucket = self._zsets.get(key, {})
        hits = [member for member, score in
                sorted(bucket.items(), key=lambda item: (item[1], item[0]))
                if float(low) <= score <= float(high)]
        if start:
            hits = hits[start:]
        return hits[:num] if num else hits

    def zrem(self, key: str, *members: str) -> int:
        bucket = self._zsets.get(key)
        if not bucket:
            return 0
        removed = 0
        for member in members:
            if bucket.pop(str(member), None) is not None:
                removed += 1
        return removed

    def zcard(self, key: str) -> int:
        if not self._alive(key):
            return 0
        return len(self._zsets.get(key, {}))

    # -- streams ---------------------------------------------------------- #
    def _stream(self, key: str, create: bool = False) -> list:
        if not self._alive(key) and create:
            self._drop(key)
            self._streams[key] = []
        return self._streams.get(key, []) if self._alive(key) or create else []

    def xadd(self, key: str, fields: dict[str, str], maxlen: int | None = None,
             approximate: bool = False) -> str:
        del approximate
        stream = self._stream(key, create=True)
        self._seq += 1
        stream_id = f"{self._now_ms()}-{self._seq}"
        stream.append((stream_id, {str(k): str(v) for k, v in fields.items()}))
        if maxlen and len(stream) > maxlen:
            del stream[:len(stream) - maxlen]
        return stream_id

    def xgroup_create(self, key: str, group: str, id: str = "0",
                      mkstream: bool = False) -> bool:
        if mkstream:
            self._stream(key, create=True)
        slot = (key, group)
        if slot in self._groups:
            return False
        self._groups[slot] = {"last": id, "pending": {}}
        return True

    def xreadgroup(self, group: str, consumer: str,
                   streams: dict[str, str], count: int | None = None,
                   block: int | None = None):
        del block
        out = []
        for key, position in streams.items():
            if position != ">":
                continue
            state = self._groups.setdefault((key, group),
                                            {"last": "0", "pending": {}})
            last = _id_parts(state["last"])
            delivered = [(sid, fields) for sid, fields in self._stream(key)
                         if _id_parts(sid) > last]
            if count:
                delivered = delivered[:count]
            if not delivered:
                continue
            state["last"] = delivered[-1][0]
            now = self._now_ms()
            for stream_id, _fields in delivered:
                state["pending"][stream_id] = [consumer, now]
            out.append((key, [(sid, dict(fields))
                              for sid, fields in delivered]))
        return out

    def xack(self, key: str, group: str, *stream_ids: str) -> int:
        state = self._groups.get((key, group))
        if not state:
            return 0
        acked = 0
        for stream_id in stream_ids:
            if state["pending"].pop(stream_id, None) is not None:
                acked += 1
        return acked

    def xdel(self, key: str, *stream_ids: str) -> int:
        stream = self._stream(key)
        drop = set(stream_ids)
        before = len(stream)
        stream[:] = [entry for entry in stream if entry[0] not in drop]
        return before - len(stream)

    def xautoclaim(self, key: str, group: str, consumer: str,
                   min_idle_time: int = 0, count: int = 10):
        state = self._groups.get((key, group))
        if not state:
            return "0-0", [], []
        now = self._now_ms()
        current = dict(self._stream(key))
        entries = []
        deleted = []
        for stream_id, (owner, delivered) in list(state["pending"].items()):
            if now - delivered < min_idle_time:
                continue
            fields = current.get(stream_id)
            if fields is None:  # the entry was XDELed by another consumer
                state["pending"].pop(stream_id, None)
                deleted.append(stream_id)
                continue
            if owner == consumer:
                continue
            state["pending"][stream_id] = [consumer, now]
            entries.append((stream_id, dict(fields)))
            if len(entries) >= count:
                break
        return "0-0", entries, deleted

    def xlen(self, key: str) -> int:
        return len(self._stream(key))

    def xrange(self, key: str, start: str = "-", end: str = "+",
               count: int | None = None):
        del start, end
        entries = list(self._stream(key))
        return entries[-count:] if count else entries

    # -- keys ------------------------------------------------------------- #
    def scan_iter(self, match: str = "*", count: int = 100):
        del count
        keys = set(self._strings) | set(self._hashes) | set(self._zsets) \
            | set(self._streams)
        for key in sorted(keys):
            if self._alive(key) and fnmatch.fnmatch(key, match):
                yield key

    def ping(self) -> bool:
        return True
