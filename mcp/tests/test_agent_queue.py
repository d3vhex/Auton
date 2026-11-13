"""Agent queue in Redis mode: API producer + `auton-ops` executor consumer."""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

import pytest

import agent_service
import agent_worker
import ops_queue
import task_store
from fake_redis import FakeRedis


def _request(base: str, path: str, payload: dict | None = None,
             method: str | None = None) -> tuple[int, dict]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"{base}{path}", data=data, method=method or ("POST" if data else "GET"),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode() or "{}")


def _store(**kwargs) -> task_store.TaskStore:
    return task_store.TaskStore(ops_queue.MemoryBackend(), lease=30,
                                max_attempts=2, backoff=[0, 0],
                                max_queue=kwargs.get("max_queue", 8))


@pytest.fixture()
def redis_server(monkeypatch):
    store = task_store.TaskStore(ops_queue.RedisBackend(client=FakeRedis()),
                                 lease=30, max_attempts=2, backoff=[0, 0],
                                 max_queue=8)
    monkeypatch.setattr(agent_service, "QUEUE_KIND", "redis")
    monkeypatch.setattr(agent_service, "AGENT_STORE", store)
    monkeypatch.setattr(agent_service, "AGENT_STORE_ERROR", "")
    httpd = agent_service.ThreadingHTTPServer(
        ("127.0.0.1", 0), agent_service.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", store
    finally:
        httpd.shutdown()
        httpd.server_close()


# --------------------------------------------------------------------------- #
# API producer
# --------------------------------------------------------------------------- #

def test_async_run_enqueues_into_the_agent_queue(redis_server):
    base, store = redis_server
    status, payload = _request(base, "/run?async=1", {"task": "fix the build"})
    assert status == 202
    job_id = payload["job_id"]
    record = store.get(job_id)
    assert record["queue"] == "agent"
    assert record["status"] == "queued"
    assert record["task"] == "fix the build"

    status, listing = _request(base, "/jobs")
    assert status == 200
    assert [job["job_id"] for job in listing["jobs"]] == [job_id]

    status, health = _request(base, "/health")
    assert status == 200
    assert health["queue"] == "redis"
    assert health["queue_depth"] == 1


def test_job_view_follows_the_executor(redis_server):
    base, store = redis_server
    job_id = _request(base, "/run?async=1", {"task": "count to 3"})[1]["job_id"]

    claim = store.claim("agent", "ops-1")[0]
    store.start(claim)
    store.append_log(job_id, "[agent] working")
    store.append_log(job_id, "[agent] done")
    store.finish(claim, status="done", result={
        "final": "counted to 3", "steps": [], "stop_reason": "final",
        "elapsed": 1.2})

    status, payload = _request(base, f"/jobs/{job_id}")
    assert status == 200
    assert payload["status"] == "done"
    assert payload["done"] is True
    assert payload["result"]["final"] == "counted to 3"
    assert payload["stop_reason"] == "final"
    assert "[agent] working" in payload["logs"]


def test_cancel_queued_redis_job(redis_server):
    base, store = redis_server
    job_id = _request(base, "/run?async=1", {"task": "slow"})[1]["job_id"]
    status, payload = _request(base, f"/jobs/{job_id}/cancel", method="POST")
    assert status == 200
    assert payload["cancelled"] is True
    assert store.get(job_id)["status"] == "cancelled"
    assert _request(base, f"/jobs/{job_id}")[1]["status"] == "cancelled"


def test_unknown_and_non_agent_tasks_are_404(redis_server):
    base, store = redis_server
    repair = store.submit("repair", {"namespace": "auton-apps", "kind": "Pod",
                                     "name": "api-1", "cause": "oom"})
    assert _request(base, f"/jobs/{repair['id']}")[0] == 404
    assert _request(base, "/jobs/not-a-task")[0] == 404
    assert _request(base, "/jobs/nope/cancel")[0] == 404


def test_queue_full_is_a_429(redis_server):
    base, store = redis_server
    store.max_queue = 1
    assert _request(base, "/run?async=1", {"task": "one"})[0] == 202
    status, payload = _request(base, "/run?async=1", {"task": "two"})
    assert status == 429
    assert "full" in payload["error"]


def test_sync_run_waits_for_the_executor(redis_server):
    base, store = redis_server

    def executor():
        deadline = time.time() + 5
        while time.time() < deadline:
            claims = store.claim("agent", "ops-sync")
            if claims:
                claim = claims[0]
                store.start(claim)
                store.finish(claim, status="done", result={
                    "final": "executed", "steps": [], "stop_reason": "final",
                    "elapsed": 0.1})
                return
            time.sleep(0.02)

    thread = threading.Thread(target=executor, daemon=True)
    thread.start()
    status, payload = _request(base, "/run", {"task": "run me"})
    thread.join(timeout=5)
    assert status == 200
    assert payload["final"] == "executed"
    assert payload["stop_reason"] == "final"


def test_redis_mode_never_starts_the_local_worker(monkeypatch):
    monkeypatch.setattr(agent_service, "QUEUE_KIND", "redis")
    monkeypatch.setattr(agent_service, "_WORKER", None)
    agent_service.start_worker()
    assert agent_service._WORKER is None


# --------------------------------------------------------------------------- #
# Executor
# --------------------------------------------------------------------------- #

def _agent_task(store, text="do the thing"):
    return store.submit("agent", {"task": text})


def test_executor_runs_a_task_and_records_the_result(monkeypatch):
    store = _store()
    task = _agent_task(store)
    seen = {}

    async def fake_run(task_text, *, log, should_stop):
        seen["task"] = task_text
        log("[agent] hello")
        assert should_stop() is False
        return {"final": "all done", "steps": [{"tool": "bash"}],
                "stop_reason": "final", "elapsed": 0.3}

    monkeypatch.setattr(agent_worker, "run_agent", fake_run)
    claim = store.claim("agent", "ops-1")[0]
    status = agent_worker.run_one(store, claim)

    assert status == "done"
    assert seen["task"] == "do the thing"
    record = store.get(task["id"])
    assert record["status"] == "done"
    assert record["result"]["final"] == "all done"
    assert record["started_ts"] and record["finished_ts"]
    assert "[agent] hello" in store.logs(task["id"])
    # The GPU slot is released even on the happy path.
    assert store.backend.get(store.gpu_key()) is None
    assert store.active_count("agent") == 0


def test_executor_honours_cancellation(monkeypatch):
    store = _store()
    task = _agent_task(store, "long job")
    claim = store.claim("agent", "ops-1")[0]

    async def fake_run(task_text, *, log, should_stop):
        store.cancel(task["id"], by="api")
        log("[agent] cancel requested")
        assert should_stop() is True
        return {"final": "(cancelled)", "steps": [],
                "stop_reason": "cancelled", "elapsed": 0.1}

    monkeypatch.setattr(agent_worker, "run_agent", fake_run)
    assert agent_worker.run_one(store, claim) == "cancelled"
    record = store.get(task["id"])
    assert record["status"] == "cancelled"
    assert record["cancel_requested"] is True
    assert store.backend.get(store.gpu_key()) is None


def test_executor_records_an_exception_as_an_error_without_retry(monkeypatch):
    store = _store()
    task = _agent_task(store)

    async def fake_run(*args, **kwargs):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(agent_worker, "run_agent", fake_run)
    claim = store.claim("agent", "ops-1")[0]
    assert agent_worker.run_one(store, claim) == "error"
    record = store.get(task["id"])
    assert record["status"] == "error"
    assert record["attempts"] == 1
    assert "provider exploded" in record["result"]["error"]
    assert store.backend.get(store.gpu_key()) is None


def test_executor_defers_when_the_gpu_slot_is_busy(monkeypatch):
    store = _store()
    task = _agent_task(store)
    store.acquire_gpu("someone-else", ttl=60)

    async def must_not_run(*args, **kwargs):
        pytest.fail("the GPU slot is held by another executor")

    monkeypatch.setattr(agent_worker, "run_agent", must_not_run)
    claim = store.claim("agent", "ops-1")[0]
    assert agent_worker.run_one(store, claim) == "deferred"
    record = store.get(task["id"])
    assert record["status"] == "queued"
    assert record["attempts"] == 0          # deferral is not a failed attempt
    assert store.backend.zcard(store.delayed_key()) == 1
