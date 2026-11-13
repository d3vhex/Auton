#!/usr/bin/env python3
"""Model-provider abstraction for the agent loop.

One `chat()` call, four providers:

  ollama      local Ollama (default)              /api/chat via the ollama client
  openai      OpenAI and OpenAI-compatible APIs   {base}/chat/completions
  gemini      Gemini's OpenAI-compatible endpoint  {base}/chat/completions
  custom      vLLM / LM Studio / llama.cpp / ...  {base}/chat/completions
  anthropic   Claude                              {base}/v1/messages

Selection is configuration, not code: the agent reads it **per request** from

    $AGENT_MODEL_DIR/config/{provider,base_url,model,max_tokens,data_policy}
    $AGENT_MODEL_DIR/secret/api-key        (Secret mount, never logged)

with environment variables as the local-development fallback. Both the agent
and the ops controller mount the same ConfigMap/Secret, so switching a provider
never needs a pod restart.

Two invariants:

1. **No fallback.** When `provider` is not `ollama`, the local model is not
   contacted at all. If the remote provider is misconfigured (missing key,
   unreachable), the request fails; it does not silently degrade to Ollama.
2. **Remote data is redacted.** Every message sent to a non-local provider goes
   through `redact()` (tokens, passwords, keys, bearer headers, private-key
   blocks). `data_policy=local-only` refuses remote providers entirely.

Tool-call normalisation: Ollama's native `tool_calls` are returned as-is (the
loop has years of guards built around them). Remote providers' tool calls are
serialised into the same JSON-text shape the loop already parses, so the
assistant/tool history stays in one format and no provider-specific ids leak
into it.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import urllib.error
import urllib.request

import ollama

PROVIDERS = ("ollama", "openai", "gemini", "anthropic", "custom")
ALIASES = {"claude": "anthropic", "local": "ollama", "openai-compatible": "openai"}

DEFAULT_MODELS = {
    "ollama": "qwen2.5-coder:7b-instruct",
    "openai": "gpt-4o-mini",
    "gemini": "gemini-2.0-flash",
    "anthropic": "claude-sonnet-4-5",
    "custom": "",
}
DEFAULT_BASE = {
    "ollama": "http://localhost:11434",
    "openai": "https://api.openai.com/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
    "anthropic": "https://api.anthropic.com",
    "custom": "",
}


class LLMError(RuntimeError):
    """Provider misconfiguration or a failed remote call."""


def model_dir() -> str:
    return os.environ.get("AGENT_MODEL_DIR", "/etc/auton-model").strip()


def _read(name: str, env: str, default: str = "") -> str:
    """Prefer the mounted ConfigMap file; fall back to an env var."""
    path = os.path.join(model_dir(), "config", name)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            value = fh.read().strip()
        if value:
            return value
    except OSError:
        pass
    return os.environ.get(env, "").strip() or default


def _api_key() -> str:
    path = os.path.join(model_dir(), "secret", "api-key")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        pass
    return os.environ.get("AGENT_LLM_API_KEY", "").strip()


def canonical(provider: str) -> str:
    name = (provider or "ollama").strip().lower()
    name = ALIASES.get(name, name)
    if name not in PROVIDERS:
        raise LLMError(
            f"unknown provider {provider!r}; use one of {', '.join(PROVIDERS)}")
    return name


def config() -> dict:
    """Resolve the active provider configuration (read on every call)."""
    provider = canonical(_read("provider", "AGENT_PROVIDER", "ollama"))
    default_base = DEFAULT_BASE.get(provider, "")
    if provider == "ollama":
        default_base = os.environ.get("OLLAMA_HOST", "").strip() or default_base
    base_url = _read("base_url", "AGENT_LLM_BASE_URL",
                     default_base).rstrip("/")
    model = _read("model", "AGENT_LLM_MODEL",
                  DEFAULT_MODELS.get(provider) or
                  os.environ.get("AGENT_MODEL", DEFAULT_MODELS["ollama"]))
    max_tokens = _read("max_tokens", "AGENT_LLM_MAX_TOKENS", "1024")
    try:
        max_tokens = max(64, min(int(max_tokens), 8192))
    except (TypeError, ValueError):
        max_tokens = 1024
    policy = _read("data_policy", "AGENT_DATA_POLICY", "redact").lower()
    if policy not in ("redact", "allow", "local-only"):
        policy = "redact"
    key = _api_key()
    if provider == "custom" and not base_url:
        raise LLMError("provider=custom requires base_url "
                       "(e.g. http://vllm:8000/v1)")
    return {
        "provider": provider,
        "base_url": base_url,
        "model": model,
        "max_tokens": max_tokens,
        "data_policy": policy,
        "api_key": key,
        "is_local": provider == "ollama",
    }


def _mask(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "…"
    return f"{key[:3]}…{key[-4:]}"


def provider_info() -> dict:
    """UI-facing status; never returns the raw key."""
    try:
        cfg = config()
    except LLMError as exc:
        return {"ok": False, "error": str(exc)}
    info = {
        "ok": True,
        "provider": cfg["provider"],
        "model": cfg["model"],
        "base_url": cfg["base_url"],
        "data_policy": cfg["data_policy"],
        "local_model_enabled": cfg["is_local"],
        "api_key_set": bool(cfg["api_key"]),
        "api_key_hint": _mask(cfg["api_key"]),
    }
    # Report the same failure a request would hit: a remote provider without a
    # key never falls back to Ollama, so `ok` must not claim it is healthy.
    if not cfg["is_local"] and not cfg["api_key"]:
        info["ok"] = False
        info["error"] = (f"provider={cfg['provider']} has no API key; write one "
                         "to the auton-model Secret (POST /provider)")
    return info


# --------------------------------------------------------------------------- #
# Redaction (remote providers only)
# --------------------------------------------------------------------------- #

_PATTERNS = [
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]{8,}"), "Bearer <redacted>"),
    (re.compile(r"(?is)-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----"),
     "<private-key-redacted>"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "<aws-key-redacted>"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), "<github-token-redacted>"),
    (re.compile(r"(?i)(postgres|mysql|mongodb|redis)://[^\s:@/]+:[^\s@/]+@"),
     r"\1://<credentials>@"),
    (re.compile(r"(?i)\b(password|passwd|token|api[_-]?key|secret|authorization)"
                r"\s*[:=]\s*['\"]?[^\s'\"]{3,}"), r"\1=<redacted>"),
]


def redact(text: str) -> str:
    """Mask secrets before logs/manifests leave the cluster."""
    text = str(text or "")
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _neutral_messages(messages: list[dict], policy: str) -> list[dict]:
    """Strip Ollama-only fields and redact for a remote provider."""
    cleaned = []
    for message in messages:
        role = message.get("role", "user")
        content = message.get("content") or ""
        if role == "tool":
            # Only possible if the provider changed mid-run; keep the evidence.
            content = f"[tool output: {message.get('tool_name', 'tool')}]\n{content}"
            role = "user"
        if policy == "redact":
            content = redact(content)
        cleaned.append({"role": role, "content": content})
    return cleaned


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

def _post_json(url: str, headers: dict, payload: dict, timeout: int) -> dict:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace") if exc.fp else ""
        raise LLMError(f"{url} answered {exc.code}: {detail[:500]}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise LLMError(f"cannot reach {url}: {exc}") from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LLMError(f"{url} returned non-JSON: {raw[:300]}") from exc


def _timeout() -> int:
    try:
        return max(5, min(int(os.environ.get("AGENT_LLM_TIMEOUT", "120")), 600))
    except (TypeError, ValueError):
        return 120


def _call_json_as_text(calls: list[dict]) -> str:
    """Turn provider tool calls into the JSON text our loop already parses."""
    rendered = []
    for call in calls:
        rendered.append(json.dumps(
            {"name": call.get("name", ""), "arguments": call.get("arguments") or {}},
            ensure_ascii=False,
        ))
    return "\n".join(rendered)


def _openai_chat(cfg: dict, messages: list[dict], tools: list[dict]) -> str:
    payload: dict = {
        "model": cfg["model"],
        "messages": _neutral_messages(messages, cfg["data_policy"]),
        "temperature": 0,
        "max_tokens": cfg["max_tokens"],
    }
    if tools:
        payload["tools"] = [{"type": "function", "function": t["function"]}
                            for t in tools]
        payload["tool_choice"] = "auto"
    data = _post_json(
        f"{cfg['base_url']}/chat/completions",
        {"Authorization": f"Bearer {cfg['api_key']}"},
        payload, _timeout(),
    )
    try:
        message = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError(f"unexpected response from {cfg['provider']}: "
                       f"{json.dumps(data)[:300]}") from exc
    content = message.get("content") or ""
    calls = []
    for call in message.get("tool_calls") or []:
        fn = call.get("function") or {}
        arguments = fn.get("arguments")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                arguments = {"raw": arguments}
        calls.append({"name": fn.get("name", ""), "arguments": arguments or {}})
    if calls:
        text = _call_json_as_text(calls)
        return f"{content}\n{text}".strip() if content else text
    return content


def _anthropic_chat(cfg: dict, messages: list[dict], tools: list[dict]) -> str:
    neutral = _neutral_messages(messages, cfg["data_policy"])
    system = "\n\n".join(m["content"] for m in neutral if m["role"] == "system")
    convo = [m for m in neutral if m["role"] != "system"]
    # Anthropic requires alternating roles and no consecutive same-role turns.
    merged: list[dict] = []
    for message in convo:
        role = "assistant" if message["role"] == "assistant" else "user"
        if merged and merged[-1]["role"] == role:
            merged[-1]["content"] += "\n\n" + message["content"]
        else:
            merged.append({"role": role, "content": message["content"]})
    payload: dict = {
        "model": cfg["model"],
        "system": system,
        "messages": merged,
        "max_tokens": cfg["max_tokens"],
        "temperature": 0,
    }
    if tools:
        payload["tools"] = [
            {"name": t["function"]["name"],
             "description": t["function"].get("description", ""),
             "input_schema": t["function"].get("parameters") or
                             {"type": "object", "properties": {}}}
            for t in tools
        ]
    data = _post_json(
        f"{cfg['base_url']}/v1/messages",
        {"x-api-key": cfg["api_key"], "anthropic-version": "2023-06-01"},
        payload, _timeout(),
    )
    blocks = data.get("content")
    if not isinstance(blocks, list):
        raise LLMError(f"unexpected response from anthropic: "
                       f"{json.dumps(data)[:300]}")
    texts, calls = [], []
    for block in blocks:
        if block.get("type") == "text":
            texts.append(block.get("text") or "")
        elif block.get("type") == "tool_use":
            calls.append({"name": block.get("name", ""),
                          "arguments": block.get("input") or {}})
    if calls:
        texts.append(_call_json_as_text(calls))
    return "\n".join(t for t in texts if t)


async def _ollama_chat(cfg: dict, messages: list[dict], tools: list[dict]):
    client = ollama.AsyncClient(host=cfg["base_url"])
    options = {
        "temperature": 0,
        "num_ctx": int(os.environ.get("AGENT_NUM_CTX", "16384")),
        "repeat_penalty": float(os.environ.get("AGENT_REPEAT_PENALTY", "1.1")),
    }
    kwargs = {"model": cfg["model"], "messages": messages, "options": options}
    if tools:
        kwargs["tools"] = tools
    response = await client.chat(**kwargs)
    message = response.message
    calls = [
        {"name": call.function.name, "arguments": call.function.arguments}
        for call in (message.tool_calls or [])
    ]
    return message.content or "", calls


class Reply:
    def __init__(self, content: str = "", tool_calls: list[dict] | None = None):
        self.content = content
        self.tool_calls = tool_calls or []


async def chat(messages: list[dict], tools: list[dict]) -> Reply:
    """Send one chat turn to the configured provider.

    Ollama: native tool calls are preserved. Remote: tool calls are returned as
    JSON text in `content` (see the module docstring), and every message is
    redacted according to `data_policy`.
    """
    cfg = config()
    if cfg["is_local"]:
        content, calls = await _ollama_chat(cfg, messages, tools)
        return Reply(content, calls)

    # From here on the local model is out of the picture — no fallback.
    if cfg["data_policy"] == "local-only":
        raise LLMError(
            "data_policy=local-only forbids a remote provider "
            f"({cfg['provider']}); switch data_policy or use provider=ollama")
    if not cfg["api_key"]:
        raise LLMError(
            f"provider={cfg['provider']} has no API key; write it to "
            "auton-model Secret (scripts/set-provider.sh)")

    if cfg["provider"] == "anthropic":
        content = await asyncio.to_thread(
            _anthropic_chat, cfg, messages, tools)
    else:
        content = await asyncio.to_thread(
            _openai_chat, cfg, messages, tools)
    return Reply(content, [])


async def test() -> dict:
    """Tiny round-trip used by POST /provider/test."""
    reply = await chat([{"role": "user", "content": "Reply with exactly: ok"}], [])
    return {"ok": True, "reply": (reply.content or "").strip()[:120],
            **provider_info()}
