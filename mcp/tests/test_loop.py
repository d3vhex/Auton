"""Tests for the pure decision helpers in agent_core (no Ollama needed)."""
from __future__ import annotations

import agent_core


def test_extract_plain_json():
    text = '{"name": "read_file", "arguments": {"path": "a.py"}}'
    assert agent_core.extract_tool_call(text, {"read_file"}) == ("read_file", {"path": "a.py"})


def test_extract_from_fence_and_prose():
    text = (
        "I will read the file.\n\n```json\n"
        '{"name": "grep", "arguments": {"pattern": "needle"}}\n```'
    )
    assert agent_core.extract_tool_call(text, {"grep"}) == ("grep", {"pattern": "needle"})


def test_extract_embedded_object_without_fence():
    text = 'Sure: {"name": "bash", "arguments": {"command": "ls"}} — running it now.'
    assert agent_core.extract_tool_call(text, {"bash"}) == ("bash", {"command": "ls"})


def test_extract_rejects_unknown_tool_and_plain_prose():
    assert agent_core.extract_tool_call('{"name": "nope", "arguments": {}}', {"bash"}) is None
    assert agent_core.extract_tool_call("plain prose", {"bash"}) is None


def test_extract_accepts_odd_argument_shapes():
    """Argument coercion is the caller's job, so extraction must not reject."""
    assert agent_core.extract_tool_call(
        '{"name": "bash", "arguments": "ls"}', {"bash"}
    ) == ("bash", "ls")
    assert agent_core.extract_tool_call(
        '{"name": "todo_write", "arguments": [{"content": "x"}]}', {"todo_write"}
    ) == ("todo_write", [{"content": "x"}])


def test_coerce_args_uses_the_tool_schema():
    bash = {"required": ["command"], "properties": {"command": {}}}
    todo = {"required": ["todos"], "properties": {"todos": {}}}
    assert agent_core.coerce_args({"path": "a"}, {}) == {"path": "a"}
    assert agent_core.coerce_args('{"path": "a"}', {}) == {"path": "a"}
    assert agent_core.coerce_args("ls", bash) == {"command": "ls"}
    assert agent_core.coerce_args([{"content": "x"}], todo) == {"todos": [{"content": "x"}]}
    assert agent_core.coerce_args([], todo) == {}
    assert agent_core.coerce_args(None, todo) == {}
    assert agent_core.coerce_args([1, 2], {"required": ["a", "b"]}) == {}


def test_as_args_handles_string_and_junk():
    assert agent_core._as_args({"a": 1}) == {"a": 1}
    assert agent_core._as_args('{"a": 1}') == {"a": 1}
    assert agent_core._as_args("not json") == {}
    assert agent_core._as_args(None) == {}


def test_looks_like_tool_call_heuristic():
    assert agent_core._looks_like_tool_call("") is True
    assert agent_core._looks_like_tool_call('{"name": "bash"}') is True
    assert agent_core._looks_like_tool_call('{"name": "nope", "arguments": {}}') is True
    assert agent_core._looks_like_tool_call(
        '```json\n{"name": "bash", "arguments": {"command": "ls"}}\n```'
    ) is True
    assert agent_core._looks_like_tool_call("Done — fib.py prints the numbers.") is False


def test_fenced_final_answer_is_not_a_tool_call():
    """Regression: a fenced summary used to trigger endless corrective retries."""
    summary = "Done. The file is:\n\n```python\nprint('hi')\n```\n\nIt prints `hi`."
    assert agent_core._looks_like_tool_call(summary) is False
    assert agent_core._looks_like_tool_call("```bash\npython3 hi.py\n```") is False


def test_announcement_detection():
    assert agent_core._looks_like_announcement("Next, I will run the tests to verify.")
    assert agent_core._looks_like_announcement("Let me check the file first.")
    assert agent_core._looks_like_announcement("I'll run pytest now.")
    assert not agent_core._looks_like_announcement(
        "Done. VERSION is now 0.2.0 and all 1 tests pass."
    )
    assert not agent_core._looks_like_announcement("x" * 600)


def test_call_signature_is_stable():
    a = agent_core._call_signature("bash", {"command": "ls", "cwd": "."})
    b = agent_core._call_signature("bash", {"cwd": ".", "command": "ls"})
    c = agent_core._call_signature("bash", {"command": "pwd"})
    assert a == b
    assert a != c


def test_clip_keeps_head_and_tail():
    out = agent_core._clip("a" * 1000 + "b" * 1000, 100)
    assert out.startswith("a")
    assert out.endswith("b")
    assert "chars elided" in out


def test_compact_keeps_recent_observations():
    messages = [{"role": "user", "content": "task"}]
    messages += [
        {"role": "tool", "tool_name": "bash", "content": "x" * 1000}
        for _ in range(6)
    ]
    agent_core.compact_messages(messages, keep_recent=2, max_chars=100)
    # last two stay intact, older ones become markers
    assert messages[-1]["content"] == "x" * 1000
    assert messages[-2]["content"] == "x" * 1000
    assert messages[1]["content"].startswith("[elided")
    assert messages[4]["content"].startswith("[elided")


def test_compact_noop_below_threshold():
    messages = [{"role": "tool", "tool_name": "bash", "content": "small"}]
    agent_core.compact_messages(messages, keep_recent=1, max_chars=10_000)
    assert messages[0]["content"] == "small"


def test_compact_recognises_fallback_observations():
    messages = [
        {"role": "user", "content": "Observation from grep:\n" + "y" * 500},
        {"role": "user", "content": "Observation from grep:\n" + "z" * 500},
    ]
    agent_core.compact_messages(messages, keep_recent=1, max_chars=10)
    assert messages[0]["content"].startswith("[elided")
    assert messages[1]["content"].startswith("Observation from grep:")


def test_system_prompt_lists_tools_and_rules():
    tools = [{
        "type": "function",
        "function": {
            "name": "apply_patch",
            "description": "patch files",
            "parameters": {"properties": {"path": {}, "patch": {}}},
        },
    }]
    prompt = agent_core.build_system_prompt(tools)
    assert "apply_patch(path, patch): patch files" in prompt
    assert "TOOL ROUTING" in prompt
    assert "plain-text" in prompt
    # The "do not rewrite tests" rule must be near the top, not buried.
    head = prompt.split("Available tools:")[0]
    assert "HARD RULES" in head
    assert "NEVER edit, delete, skip or weaken a test" in head


def test_list_directory_treats_zero_max_entries_as_default(ws, srv):
    srv.write_file("a.txt", "a")
    srv.write_file("b.txt", "b")
    out = srv.list_directory(".", max_entries=0)
    assert "a.txt" in out and "b.txt" in out
    assert "stopped at" not in out


def test_loop_guard_stops_on_identical_output_with_different_args(ws, monkeypatch):
    """A model dodging the exact-signature guard must still be stopped.

    The 7B model repeatedly called list_directory with depth=13, 14, 15 … which
    all clamp to the same result; every call had a new signature, so only the
    "same output twice in a row" guard can end the run.
    """
    import asyncio

    import pytest

    pytest.importorskip("mcp")
    (ws / "only.txt").write_text("x", encoding="utf-8")

    calls = {"n": 0}

    async def fake_chat(messages, tools):
        calls["n"] += 1
        text = ('{"name": "list_directory", "arguments": '
                f'{{"path": ".", "depth": {calls["n"]}}}}}')
        return agent_core.llm.Reply(text, [])

    monkeypatch.setattr(agent_core.llm, "chat", fake_chat)
    result = asyncio.run(agent_core.run_agent("list the workspace"))
    assert result["stop_reason"] == "repeat"
    assert calls["n"] <= 6, "the guard must stop the run instead of burning all steps"
