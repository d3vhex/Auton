"""Task queue semantics, exercised on both backends (memory and Redis)."""
from __future__ import annotations

import time

import pytest

import ops_queue
import task_store
from fake_redis import FakeRedis


@pytest.fixture(params=["memory", "redis"])
def store(request):
    if request.param == "memory":
        backend = ops_queue.MemoryBackend()
    else:
        # A real RedisBackend over the in-memory double: this covers the
        # XAUTOCLAIM / XDEL / consumer-group paths the cluster uses.
        backend = ops_queue.RedisBackend(client=FakeRedis())
    return task_store.TaskStore(backend, lease=30, max_attempts=2,
                                backoff=[1, 1], max_queue=50)


def _repair(store, name="api-1", cause="oom", **kwargs):
    return store.submit("repair", {
        "namespace": "auton-apps", "kind": "Pod", "name": name,
        "cause": cause, "severity": "critical",
    }, priority=kwargs.pop("priority", "normal"), **kwargs)


# --------------------------------------------------------------------------- #
# Submit
# --------------------------------------------------------------------------- #

def test_submit_defaults_and_derived_resource(store):
    record = _repair(store)
    assert record["id"].startswith("tsk-")
    assert record["status"] == "queued"
    assert record["queue"] == "repair"
    assert record["resource"] == "pod/api-1"
    assert record["timeline"][0]["to"] == "queued"
    assert store.get(record["id"])["cause"] == "oom"


def test_submit_rejects_unknown_queue_and_priority(store):
    with pytest.raises(task_store.TaskError, match="unknown queue"):
        store.submit("nonsense", {})
    with pytest.raises(task_store.TaskError, match="priority"):
        _repair(store, priority="urgent")


def test_idempotency_key_returns_the_same_task(store):
    first = _repair(store, idempotency_key="sha1:abc")
    second = _repair(store, idempotency_key="sha1:abc")
    assert second["duplicate"] is True
    assert second["id"] == first["id"]
    assert len(store.list()) == 1

    other = _repair(store, name="api-2", idempotency_key="sha1:def")
    assert other["id"] != first["id"] and not other.get("duplicate")


def test_queue_cap_is_enforced(store, monkeypatch):
    monkeypatch.setattr(store, "max_queue", 1)
    _repair(store, name="api-1")
    with pytest.raises(task_store.TaskError, match="is full"):
        _repair(store, name="api-2")


# --------------------------------------------------------------------------- #
# Claim / run / finish
# --------------------------------------------------------------------------- #

def test_claim_runs_and_finishes_resolved(store):
    record = _repair(store)
    claims = store.claim("repair", "worker-1")
    assert len(claims) == 1
    task = store.start(claims[0])
    assert task["status"] == "running"
    assert task["attempts"] == 1
    assert task["worker"] == "worker-1"

    store.verifying(claims[0])
    done = store.finish(claims[0], status="resolved",
                        result={"action": "oom_bump_memory", "ok": True},
                        verification="ready=1/1")
    assert done["status"] == "resolved"
    assert done["verified_at"]
    assert done["worker"] == ""
    assert store.backend.get(store.lock_key("pod/api-1")) is None
    assert store.backend.zcard(store.claimed_key()) == 0
    assert done["timeline"][-1]["to"] == "resolved"


def test_finish_rejects_unknown_status(store):
    _repair(store)
    claim = store.claim("repair", "worker-1")[0]
    with pytest.raises(task_store.TaskError, match="unknown status"):
        store.finish(claim, status="finished")


def test_same_resource_is_serialised_and_others_run_in_parallel(store):
    first = _repair(store, name="api-1")
    same = _repair(store, name="api-1", cause="crashloop")
    other = _repair(store, name="api-2")

    claims = store.claim("repair", "worker-1", limit=3)
    claimed = [c.task["id"] for c in claims]
    # Different resources are claimed together; the duplicate pod is not.
    assert first["id"] in claimed and other["id"] in claimed
    assert same["id"] not in claimed
    assert store.get(same["id"])["status"] == "queued"
    assert store.backend.zcard(store.delayed_key()) == 1

    # Once the first task releases its lock the deferred one can be taken.
    by_id = {c.task["id"]: c for c in claims}
    store.finish(by_id[first["id"]], status="resolved")
    store.backend.zadd(store.delayed_key(), time.time() - 1, same["id"])
    second = store.claim("repair", "worker-2", limit=3)
    assert [c.task["id"] for c in second] == [same["id"]]


def test_claim_skips_tasks_that_are_not_queued(store):
    record = _repair(store)
    claim = store.claim("repair", "worker-1")[0]
    store.finish(claim, status="resolved")
    # A redelivered message must not resurrect a finished task.
    store.backend.xadd(store.stream("repair", "normal"), {"id": record["id"]})
    assert store.claim("repair", "worker-2") == []


# --------------------------------------------------------------------------- #
# Retry / lease recovery / cancel
# --------------------------------------------------------------------------- #

def test_retry_backs_off_then_needs_human(store):
    record = _repair(store)
    first = store.claim("repair", "worker-1")[0]
    assert store.retry(first, reason="patch failed") == "queued"
    assert store.get(record["id"])["status"] == "queued"
    assert store.backend.zcard(store.delayed_key()) == 1

    # Make the delayed entry due and take it again.
    store.backend.zadd(store.delayed_key(), time.time() - 1, record["id"])
    second = store.claim("repair", "worker-2")[0]
    assert second.task["attempts"] == 2
    assert store.retry(second, reason="still broken") == "needs_human"
    final = store.get(record["id"])
    assert final["status"] == "needs_human"
    assert "gave up" in final["timeline"][-1]["note"]
    assert store.backend.xlen(store.dead_key()) == 1


def test_expired_lease_is_recovered(store):
    record = _repair(store)
    claim = store.claim("repair", "dead-worker")[0]
    store.start(claim)

    # Simulate a crash: the lease is in the past and nobody will finish it.
    store.update(record["id"], lease_until=0)
    store.backend.zadd(store.claimed_key(), time.time() - 5, record["id"])

    recovered = store.recover()
    assert recovered == [record["id"]]
    assert store.get(record["id"])["status"] == "queued"
    assert store.backend.get(store.lock_key("pod/api-1")) is None

    retaken = store.claim("repair", "worker-2")[0]
    assert retaken.task["attempts"] == 2


def test_lease_expiry_after_last_attempt_needs_human(store):
    record = _repair(store)
    claim = store.claim("repair", "worker-1")[0]
    store.update(record["id"], lease_until=0, attempts=2)
    store.backend.zadd(store.claimed_key(), time.time() - 5, record["id"])
    store.recover()
    assert store.get(record["id"])["status"] == "needs_human"


def test_cancel_queued_and_running(store):
    queued = _repair(store, name="api-1")
    cancelled = store.cancel(queued["id"])
    assert cancelled["status"] == "cancelled"
    assert store.claim("repair", "worker-1") == []

    running = _repair(store, name="api-2")
    claim = store.claim("repair", "worker-1")[0]
    store.start(claim)
    record = store.cancel(running["id"])
    assert record["status"] == "running"
    assert store.cancel_requested(running["id"]) is True
    finished = store.finish(claim, status="cancelled")
    assert finished["status"] == "cancelled"
    assert store.backend.get(store.lock_key("pod/api-2")) is None


def test_cancel_unknown_task(store):
    with pytest.raises(task_store.TaskError, match="unknown task"):
        store.cancel("tsk-00000000")


# --------------------------------------------------------------------------- #
# Coordination
# --------------------------------------------------------------------------- #

def test_cooldown_round_trip(store):
    assert store.in_cooldown("pod/api-1", "oom") is False
    store.set_cooldown("pod/api-1", "oom", 60)
    assert store.in_cooldown("pod/api-1", "oom") is True
    assert store.cooldown_remaining("pod/api-1", "oom") > 0
    assert store.in_cooldown("pod/api-1", "crashloop") is False


def test_hourly_budget_is_atomic(store):
    assert store.take_budget(2) == (True, 1)
    assert store.take_budget(2) == (True, 2)
    assert store.take_budget(2) == (False, 3)
    assert store.budget_used() == 3
    assert store.take_budget(0) == (False, 0)


def test_heartbeats_are_listed(store):
    store.heartbeat("worker", "w1")
    store.heartbeat("scanner", "s1")
    roles = {(w["role"], w["name"]) for w in store.workers()}
    assert roles == {("worker", "w1"), ("scanner", "s1")}
    assert all(w["alive"] for w in store.workers())


def test_logs_round_trip(store):
    record = _repair(store)
    store.append_log(record["id"], "step 1")
    store.append_log(record["id"], "step 2")
    assert store.logs(record["id"]) == ["step 1", "step 2"]


def test_stats_report_depth_and_statuses(store):
    _repair(store, name="api-1")
    _repair(store, name="api-2")
    claim = store.claim("repair", "worker-1")[0]
    store.finish(claim, status="resolved")
    stats = store.stats()
    assert stats["backend"] == store.backend.name
    assert stats["statuses"]["resolved"] == 1
    assert stats["statuses"]["queued"] == 1
    assert stats["queue_depth"]["repair_normal"] == 1
    assert stats["oldest_queued"] is not None


def test_list_filters_and_order(store):
    first = _repair(store, name="api-1")
    time.sleep(0.002)
    second = _repair(store, name="api-2")
    agent = store.submit("agent", {"task": "write fib.py"})

    assert [r["id"] for r in store.list(queue="repair")] == [second["id"],
                                                            first["id"]]
    assert [r["id"] for r in store.list(queue="agent")] == [agent["id"]]
    store.cancel(first["id"])
    assert [r["id"] for r in store.list(status="cancelled")] == [first["id"]]


# --------------------------------------------------------------------------- #
# Backend selection
# --------------------------------------------------------------------------- #

def test_unknown_backend_is_rejected(monkeypatch):
    monkeypatch.setenv("AGENT_QUEUE", "kafka")
    with pytest.raises(ops_queue.QueueError, match="unknown AGENT_QUEUE"):
        ops_queue.create_backend()


def test_redis_backend_without_the_package_fails_closed(monkeypatch):
    monkeypatch.setenv("AGENT_QUEUE", "redis")
    try:
        import redis  # noqa: F401
    except ImportError:
        with pytest.raises(ops_queue.QueueError, match="redis.*not installed"):
            ops_queue.create_backend()
