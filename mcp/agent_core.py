#!/usr/bin/env python3
"""Reusable agent loop: Ollama tool-calling driving the MCP tool server.

Shared by agent_client.py (CLI) and agent_service.py (HTTP pod). Handles tool
calls two ways, because small local models (e.g. qwen2.5-coder:7b) often emit
the call as JSON text instead of Ollama's structured `tool_calls`:
    (a) native   -> resp.message.tool_calls
    (b) fallback -> a {"name":..,"arguments":{..}} object parsed from content

Context discipline matters as much as tool calling with a 7B model:
  * every observation is capped (MAX_OBSERVATION_CHARS)
  * older observations are collapsed once the history exceeds MAX_HISTORY_CHARS
  * a malformed tool call gets one corrective retry before we give up
"""
from __future__ import annotations

import json
import os
import re
import sys
import time

import llm
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
MODEL = os.environ.get("AGENT_MODEL", "qwen2.5-coder:7b-instruct")
MAX_STEPS = max(1, int(os.environ.get("MAX_STEPS", "25")))
NUM_CTX = int(os.environ.get("AGENT_NUM_CTX", "16384"))
MAX_OBSERVATION_CHARS = int(os.environ.get("AGENT_MAX_OBSERVATION_CHARS", "8000"))
MAX_HISTORY_CHARS = int(os.environ.get("AGENT_MAX_HISTORY_CHARS", "60000"))
KEEP_RECENT_OBSERVATIONS = int(os.environ.get("AGENT_KEEP_OBSERVATIONS", "3"))
PARSE_RETRIES = max(0, int(os.environ.get("AGENT_PARSE_RETRIES", "1")))
REPEAT_LIMIT = max(1, int(os.environ.get("AGENT_REPEAT_LIMIT", "2")))
REPEAT_PENALTY = float(os.environ.get("AGENT_REPEAT_PENALTY", "1.1"))
SYSTEM_EXTRA = os.environ.get("AGENT_SYSTEM_EXTRA", "").strip()

SERVER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent_server.py")


def build_system_prompt(tools: list[dict]) -> str:
    lines = [
        "You are a precise, autonomous coding agent working inside a sandboxed workspace.",
        "You accomplish tasks by calling tools, observing their output, and iterating.",
        "",
        "HARD RULES — violating any of these fails the task:",
        "  1. NEVER edit, delete, skip or weaken a test file, test assertion or the test",
        "     command to make a test pass. Fix the implementation instead. If the test",
        "     is genuinely wrong, stop and explain why instead of silently changing it.",
        "  2. NEVER fake success: no stubs, no `pass`, no `return True`, no hardcoding",
        "     the exact values a test checks, no try/except that swallows the failure.",
        "  3. Never report the task done until you ran the real verification command and",
        "     saw it pass. Quote the actual output; do not assume it worked.",
        "",
        "Available tools:",
    ]
    for t in tools:
        fn = t["function"]
        props = fn["parameters"].get("properties", {})
        sig = ", ".join(props.keys())
        lines.append(f"  - {fn['name']}({sig}): {fn['description']}")
    lines += [
        "",
        "PROTOCOL — act ONE step at a time:",
        '  * To call a tool, reply with ONLY a single JSON object and nothing else:',
        '        {"name": "<tool>", "arguments": {<args>}}',
        "    No prose, no explanation, no markdown code fences around it.",
        "  * After each call you will receive an `Observation:` with the tool output.",
        "  * Do NOT write the whole solution as text up front — use write_file / apply_patch",
        "    to create and edit files, and bash / run_tests to execute and verify them.",
        "",
        "TOOL ROUTING:",
        "  * Use glob() to find files by name; use grep() to find code by content.",
        "  * read_file(path, start_line, end_line) for large files — never dump a whole",
        "    repository into the conversation.",
        "  * write_file() only for new files. For edits use apply_patch() (preferred) or",
        "    edit_file(); they return a diff, so you do not need to re-read the file.",
        "  * bash(background=true) for servers/watchers; poll with job(job_id).",
        "  * Verify work with bash() or run_tests() before declaring success.",
        "  * Kubernetes: ALWAYS start with k8s_snapshot() when asked what is wrong or",
        "    before any repair. It names the broken pods/workloads in PROBLEMS.",
        "  * For a flagged pod call k8s_diagnose(pod) — it includes status, owner,",
        "    warning events, current AND previous logs, plus a diagnosis and a",
        "    suggested_action. Follow the suggested_action; do not guess from names.",
        "  * k8s_events(since) when the pod looks healthy but something failed recently.",
        "  * Change only with k8s_apply/k8s_patch/k8s_scale/k8s_rollout/k8s_delete;",
        "    then verify with k8s_wait(...) or another k8s_snapshot().",
        "  * Never delete a PVC or read a Secret. Report why a human is needed and",
        "    give the exact command instead.",
        "  * The namespace must be one the tool allows (auton or auton-apps).",
        "  * n8n: n8n_workflows/n8n_activate/n8n_executions to manage workflows,",
        "    n8n_webhook to run one, n8n_create_agent_pipeline to generate one.",
        "  * If a tool returns an error, read its hint and change the call (usually:",
        "    copy the exact line from read_file). Never retry the same failing call.",
        "  * Never end a turn with a plan like 'Next I will run the tests' — make the",
        "    tool call in the same turn, then report the real output.",
        "  * When a test fails, fix the implementation (HARD RULE 1). Never rewrite",
        "    the test's assertions to match buggy code unless the task explicitly",
        "    asks you to change the tests.",
        "",
        "When the task is fully done AND verified, reply with a short plain-text",
        "summary (NOT JSON). That ends the session.",
    ]
    if SYSTEM_EXTRA:
        lines += ["", SYSTEM_EXTRA]
    return "\n".join(lines)


def mcp_tools_to_ollama(tools) -> list[dict]:
    converted = []
    for t in tools:
        schema = (
            getattr(t, "input_schema", None)
            or getattr(t, "inputSchema", None)
            or {"type": "object", "properties": {}}
        )
        converted.append(
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description or "",
                    "parameters": schema,
                },
            }
        )
    return converted


def tool_result_text(result) -> str:
    parts = [getattr(b, "text", None) for b in result.content]
    parts = [p for p in parts if p is not None]
    return "\n".join(parts) if parts else "(no output)"


def _json_objects(s: str) -> list[str]:
    out, depth, start = [], 0, None
    for i, ch in enumerate(s):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start is not None:
                out.append(s[start : i + 1])
                start = None
    return out


def extract_tool_call(text: str, valid: set[str]):
    if not text:
        return None
    candidates: list[str] = []
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fenced:
        candidates.append(fenced.group(1).strip())
    candidates.append(text.strip())
    candidates.extend(_json_objects(text))
    for c in candidates:
        try:
            obj = json.loads(c)
        except Exception:
            continue
        if not isinstance(obj, dict) or obj.get("name") not in valid:
            continue
        return obj["name"], obj.get("arguments", {})
    return None


def coerce_args(args, params: dict) -> dict:
    """Turn whatever the model wrote into a dict of named arguments.

    Observed from qwen2.5-coder: arguments as a JSON string, `[]` for a
    no-argument tool, and a bare value for a single-parameter tool
    (`{"name": "todo_write", "arguments": [...]}`). The tool schema is the
    authority on how to wrap it.
    """
    if isinstance(args, str):
        try:
            decoded = json.loads(args)
        except Exception:
            decoded = None
        if isinstance(decoded, dict):
            return decoded
        if decoded is not None:
            args = decoded

    if isinstance(args, dict):
        return args
    if args is None or args == [] or args == "":
        return {}

    required = params.get("required") or list(params.get("properties", {}))
    if len(required) == 1:
        return {required[0]: args}
    return {}


def _as_args(args) -> dict:
    """Ollama returns arguments as a dict; some models/templates send a JSON string."""
    if isinstance(args, dict):
        return args
    if isinstance(args, str):
        try:
            parsed = json.loads(args or "{}")
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _clip(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    head = int(limit * 0.7)
    tail = limit - head
    return f"{text[:head]}\n…[{len(text) - limit} chars elided]…\n{text[-tail:]}"


def _is_observation(message: dict) -> bool:
    if message.get("role") == "tool":
        return True
    content = str(message.get("content", ""))
    return message.get("role") == "user" and (
        content.startswith("Observation from ") or content.startswith("[tool output:")
    )


def compact_messages(messages: list[dict], *,
                     keep_recent: int = KEEP_RECENT_OBSERVATIONS,
                     max_chars: int = MAX_HISTORY_CHARS) -> list[dict]:
    """Collapse old tool observations so the prompt does not grow without bound.

    Mutates and returns `messages`. The most recent observations stay verbatim;
    everything older becomes a one-line marker. This is what keeps a 25-step run
    inside a 16k context window.
    """
    total = sum(len(str(m.get("content", ""))) for m in messages)
    if total <= max_chars:
        return messages
    indices = [i for i, m in enumerate(messages) if _is_observation(m)]
    if len(indices) <= keep_recent:
        return messages
    for i in indices[:-keep_recent]:
        content = str(messages[i].get("content", ""))
        if content.startswith("[elided"):
            continue
        tool = messages[i].get("tool_name") or "tool"
        messages[i] = {**messages[i],
                       "content": f"[elided {len(content)} chars of {tool} output]"}
    return messages


def _looks_like_tool_call(text: str) -> bool:
    """True when a reply probably intended to be a tool call but failed to parse.

    Deliberately *not* triggered by markdown fences alone: models habitually wrap
    final answers (and code samples) in ``` blocks, and treating those as broken
    tool calls sends the loop into an endless "call the same tool again" cycle.
    Only a JSON object that actually carries a call-shaped key counts.
    """
    t = (text or "").strip()
    if not t:
        return True
    if t.startswith("{") or t.startswith("["):
        return True
    for candidate in _json_objects(t):
        try:
            obj = json.loads(candidate)
        except Exception:
            continue
        if isinstance(obj, dict) and any(k in obj for k in ("name", "tool", "function")):
            return True
    return False


_ANNOUNCE_RE = re.compile(
    r"(?i)^\s*(next|then|now|let me|i will|i'll|i am going to|i'm going to)\b"
)


def _looks_like_announcement(text: str) -> bool:
    """True for a short 'Next, I will run the tests' reply with no tool call.

    Small models sometimes end the turn with a plan instead of acting on it.
    Nudging once turns that into the missing tool call; a genuine final summary
    of a completed task rarely opens with "Next, ...".
    """
    t = (text or "").strip()
    return bool(t) and len(t) < 500 and bool(_ANNOUNCE_RE.match(t))


def _call_signature(name: str, args: dict) -> str:
    """Stable identity for a tool call, used to detect loops."""
    try:
        payload = json.dumps(args, sort_keys=True, default=str)
    except Exception:  # pragma: no cover - args are JSON-safe in practice
        payload = repr(args)
    return f"{name}|{payload}"


def _correction(valid: set[str]) -> str:
    return (
        "Your last message was not a valid tool call. Reply with ONLY one JSON object:\n"
        '{"name": "<tool>", "arguments": {<args>}}\n'
        "Valid tool names: " + ", ".join(sorted(valid)) + "\n"
        "No prose and no markdown fences until the task is complete."
    )


async def run_agent(task: str, *, log=lambda *_: None, should_stop=None) -> dict:
    """Run the agent loop for one task.

    Returns {final, steps, stop_reason, elapsed}, where stop_reason is one of
    "final", "max_steps", "parse_failed", "repeat", "cancelled" or "error".
    """
    started = time.time()
    steps: list[dict] = []
    stop = should_stop or (lambda: False)

    def result(final: str, reason: str) -> dict:
        return {"final": final, "steps": steps, "stop_reason": reason,
                "elapsed": round(time.time() - started, 1)}

    server = StdioServerParameters(
        command=sys.executable, args=[SERVER_PATH], env=os.environ.copy()
    )
    async with stdio_client(server) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listed = await session.list_tools()
            tools = mcp_tools_to_ollama(listed.tools)
            valid = {t["function"]["name"] for t in tools}
            schemas = {
                t["function"]["name"]: t["function"].get("parameters") or {}
                for t in tools
            }
            log(f"[agent] tools: {', '.join(sorted(valid))}")
            info = llm.provider_info()
            log(f"[agent] provider: {info.get('provider')} "
                f"model={info.get('model')} "
                f"local={info.get('local_model_enabled')} "
                f"data_policy={info.get('data_policy')}")

            messages: list[dict] = [
                {"role": "system", "content": build_system_prompt(tools)},
                {"role": "user", "content": task},
            ]

            async def dispatch(name: str, args: dict) -> str:
                nonlocal steps
                t0 = time.time()
                log(f"    -> {name}({args})")
                try:
                    tool_result = await session.call_tool(name, args)
                    out = tool_result_text(tool_result)
                except Exception as exc:
                    out = f"ERROR calling {name}: {exc}"
                out = _clip(out, MAX_OBSERVATION_CHARS)
                steps.append({"tool": name, "arguments": args, "output": out,
                              "ms": int((time.time() - t0) * 1000)})
                preview = out if len(out) < 400 else out[:400] + " …[truncated]"
                log(f"    <- {preview}")
                return out

            # Loop guard. A 7B model will happily re-run the same command forever
            # after a successful observation; the second identical call gets a
            # nudge instead of an execution, the third ends the run.
            last_signature: str | None = None
            repeats = 0
            repeat_nudged = False
            last_observation = ""
            # A model can dodge the exact-signature guard by varying an argument
            # that has no effect on the result (depth=13, 14, 15 … all clamp to
            # 5). Comparing consecutive outputs catches "this call changed
            # nothing" without having to guess which arguments are cosmetic.
            last_output: tuple[str, str] | None = None
            same_output = 0
            same_output_nudged = False
            # Failures are counted per (tool, args), not consecutively: a model
            # that alternates "bad edit" -> "read file" -> "same bad edit" never
            # repeats a signature twice in a row, so the consecutive guard misses it.
            failures: dict[str, int] = {}
            nudged_failures: set[str] = set()

            async def run_call(name: str, args: dict, *, native: bool) -> str | None:
                """Execute a call (or nudge on a loop). None means 'stop the run'."""
                nonlocal last_signature, repeats, repeat_nudged, last_observation
                nonlocal last_output, same_output, same_output_nudged
                signature = _call_signature(name, args)
                if signature == last_signature:
                    repeats += 1
                else:
                    last_signature, repeats, repeat_nudged = signature, 0, False

                if repeats >= REPEAT_LIMIT:
                    if repeat_nudged:
                        log(f"    !! repeated {name} {repeats + 1}x — stopping")
                        return None
                    repeat_nudged = True
                    out = (
                        f"[loop guard] You already ran {name} with these exact "
                        "arguments and the result is above. Do not run it again: "
                        "either take a different step or reply with a plain-text "
                        "summary of what was accomplished."
                    )
                    log(f"    !! repeated {name} — nudge")
                else:
                    out = await dispatch(name, args)
                    last_observation = out
                    if last_output and last_output[0] == name and last_output[1] == out:
                        same_output += 1
                    else:
                        same_output, same_output_nudged = 0, False
                    last_output = (name, out)
                    if same_output >= REPEAT_LIMIT:
                        if same_output_nudged:
                            log(f"    !! {name} returned identical output "
                                f"{same_output + 1}x — stopping")
                            return None
                        same_output_nudged = True
                        out += (
                            "\n[loop guard] This call returned exactly the same "
                            "output as the previous one, so it made no progress. "
                            "Change the approach: take a different step, or reply "
                            "with a plain-text summary of what was accomplished."
                        )
                        log(f"    !! {name} returned identical output — nudge")
                    if out.startswith('{"ok": false'):
                        failures[signature] = failures.get(signature, 0) + 1
                        count = failures[signature]
                        if count > REPEAT_LIMIT:
                            log(f"    !! {name} failed {count}x with the same args — stopping")
                            return None
                        if count == REPEAT_LIMIT and signature not in nudged_failures:
                            nudged_failures.add(signature)
                            out += (
                                f"\n[loop guard] This exact call has failed {count} "
                                "times. Change the arguments using the hint above "
                                "instead of retrying it unchanged."
                            )
                            log(f"    !! {name} failed {count}x — nudge")

                if native:
                    messages.append({"role": "tool", "tool_name": name,
                                     "content": out})
                else:
                    messages.append({
                        "role": "user",
                        "content": f"[tool output: {name}]\n{out}\n[end tool output] "
                                   "Continue the task: make the next tool call, or "
                                   "reply with the final plain-text summary.",
                    })
                return out

            parse_failures = 0
            announcement_nudged = False
            for step in range(1, MAX_STEPS + 1):
                if stop():
                    log("[agent] cancelled")
                    return result("(cancelled)", "cancelled")

                compact_messages(messages)
                try:
                    reply = await llm.chat(messages, tools)
                except Exception as exc:
                    log(f"[agent] provider error: {exc}")
                    return result(
                        f"ERROR talking to the model provider: "
                        f"{type(exc).__name__}: {exc}", "error")

                content = reply.content or ""
                native = reply.tool_calls or []

                if native:
                    parse_failures = 0
                    log(f"[step {step}] (native tool_calls)")
                    messages.append(
                        {
                            "role": "assistant",
                            "content": content,
                            "tool_calls": [
                                {"function": {"name": tc["name"],
                                              "arguments": tc["arguments"]}}
                                for tc in native
                            ],
                        }
                    )
                    for tc in native:
                        args = coerce_args(_as_args(tc["arguments"]),
                                           schemas.get(tc["name"], {}))
                        if await run_call(tc["name"], args, native=True) is None:
                            return result(last_observation or "(repeated tool call)",
                                          "repeat")
                    continue

                call = extract_tool_call(content, valid)
                if call:
                    parse_failures = 0
                    name, args = call
                    log(f"[step {step}] (parsed from content)")
                    messages.append({"role": "assistant", "content": content})
                    args = coerce_args(args, schemas.get(name, {}))
                    if await run_call(name, args, native=False) is None:
                        return result(last_observation or "(repeated tool call)",
                                      "repeat")
                    continue

                if _looks_like_announcement(content) and not announcement_nudged:
                    announcement_nudged = True
                    log(f"[step {step}] plan without action — nudge")
                    messages.append({"role": "assistant", "content": content})
                    messages.append({
                        "role": "user",
                        "content": (
                            "Do not stop after announcing the next step. Perform it "
                            "now with a tool call, then report the actual result."
                        ),
                    })
                    continue

                if re.match(r"(?i)^\s*(observation\b|\[tool output)", content or ""):
                    # The model copied the tool output back as its "answer".
                    if parse_failures < PARSE_RETRIES:
                        parse_failures += 1
                        log(f"[step {step}] echoed the observation — corrective retry "
                            f"({parse_failures}/{PARSE_RETRIES})")
                        messages.append({"role": "assistant", "content": content})
                        messages.append({
                            "role": "user",
                            "content": (
                                "That text was the tool's output, not your answer. "
                                "Continue the task: call the next tool, or reply with a "
                                "plain-text summary of what you changed and verified."
                            ),
                        })
                        continue
                    return result(content, "parse_failed")

                if _looks_like_tool_call(content) and parse_failures < PARSE_RETRIES:
                    parse_failures += 1
                    log(f"[step {step}] malformed tool call — corrective retry "
                        f"({parse_failures}/{PARSE_RETRIES})")
                    messages.append({"role": "assistant", "content": content})
                    messages.append({"role": "user", "content": _correction(valid)})
                    continue

                log("[agent] FINAL")
                return result(content or "(empty response)",
                              "parse_failed" if _looks_like_tool_call(content) else "final")

            return result(last_observation or f"(stopped after {MAX_STEPS} steps)",
                          "max_steps")
