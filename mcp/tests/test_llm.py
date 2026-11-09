"""Tests for the provider layer: config, adapters, redaction, no fallback."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

import llm


@pytest.fixture()
def cfgdir(tmp_path, monkeypatch):
    """Isolated $AGENT_MODEL_DIR, no config files (env fallbacks still work)."""
    monkeypatch.setenv("AGENT_MODEL_DIR", str(tmp_path))
    for name in ("AGENT_PROVIDER", "AGENT_LLM_BASE_URL", "AGENT_LLM_MODEL",
                 "AGENT_LLM_API_KEY", "AGENT_DATA_POLICY"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def _write_config(home, **values):
    config = home / "config"
    config.mkdir(parents=True, exist_ok=True)
    for key, value in values.items():
        (config / key).write_text(str(value), encoding="utf-8")


def test_default_is_local_ollama(cfgdir, monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "http://ollama-service:11434")
    cfg = llm.config()
    assert cfg["provider"] == "ollama"
    assert cfg["is_local"] is True
    assert cfg["base_url"] == "http://ollama-service:11434"
    assert cfg["data_policy"] == "redact"


def test_files_win_over_environment(cfgdir, monkeypatch):
    monkeypatch.setenv("AGENT_PROVIDER", "openai")
    _write_config(cfgdir, provider="claude", base_url="https://claude.local",
                  model="claude-test", data_policy="allow")
    (cfgdir / "secret").mkdir()
    (cfgdir / "secret" / "api-key").write_text("sk-ant-secret\n")
    cfg = llm.config()
    assert cfg["provider"] == "anthropic"
    assert cfg["base_url"] == "https://claude.local"
    assert cfg["model"] == "claude-test"
    assert cfg["data_policy"] == "allow"
    assert cfg["api_key"] == "sk-ant-secret"


def test_custom_requires_a_base_url(cfgdir, monkeypatch):
    monkeypatch.setenv("AGENT_PROVIDER", "custom")
    with pytest.raises(llm.LLMError, match="base_url"):
        llm.config()
    info = llm.provider_info()
    assert info["ok"] is False and "base_url" in info["error"]


def test_provider_info_never_leaks_the_key(cfgdir, monkeypatch):
    monkeypatch.setenv("AGENT_PROVIDER", "openai")
    monkeypatch.setenv("AGENT_LLM_API_KEY", "sk-abcdefghijklmnop")
    info = llm.provider_info()
    assert info["api_key_set"] is True
    assert "abcdefghijklmnop" not in json.dumps(info)
    assert info["api_key_hint"] == "sk-…mnop"


def test_redaction_masks_common_secrets():
    text = (
        "password: hunter2\n"
        "API_KEY=abcd1234efgh\n"
        "Authorization: Bearer abcdefghijklmnop\n"
        "aws AKIAIOSFODNN7EXAMPLE\n"
        "postgres://user:pw@db:5432/app\n"
        "-----BEGIN RSA PRIVATE KEY-----\nMIIE...\n"
        "-----END RSA PRIVATE KEY-----\n"
    )
    masked = llm.redact(text)
    for secret in ("hunter2", "abcd1234efgh", "abcdefghijklmnop",
                   "AKIAIOSFODNN7EXAMPLE", "pw@db", "MIIE"):
        assert secret not in masked
    assert "<redacted>" in masked and "<credentials>" in masked


def _remote(cfgdir, monkeypatch, provider="openai", policy="redact"):
    monkeypatch.setenv("AGENT_PROVIDER", provider)
    monkeypatch.setenv("AGENT_LLM_API_KEY", "sk-test-123456789")
    monkeypatch.setenv("AGENT_DATA_POLICY", policy)
    monkeypatch.setenv("AGENT_LLM_BASE_URL", "https://api.example/v1")


def test_openai_adapter_turns_tool_calls_into_json_text(cfgdir, monkeypatch):
    _remote(cfgdir, monkeypatch)
    captured = {}

    def fake_post(url, headers, payload, timeout):
        captured.update(url=url, headers=headers, payload=payload)
        return {"choices": [{"message": {
            "content": "checking",
            "tool_calls": [{"function": {
                "name": "k8s_snapshot",
                "arguments": '{"namespace": "auton"}'}}],
        }}]}

    monkeypatch.setattr(llm, "_post_json", fake_post)
    reply = asyncio.run(llm.chat(
        [{"role": "user", "content": "password: hunter2\ncheck the cluster"}],
        [{"type": "function", "function": {
            "name": "k8s_snapshot", "description": "snapshot",
            "parameters": {"type": "object", "properties": {}}}}]))

    assert captured["url"] == "https://api.example/v1/chat/completions"
    assert captured["headers"]["Authorization"].startswith("Bearer sk-")
    assert captured["payload"]["tools"][0]["function"]["name"] == "k8s_snapshot"
    sent = json.dumps(captured["payload"])
    assert "hunter2" not in sent          # redacted before leaving the cluster
    assert "<redacted>" in sent
    assert reply.tool_calls == []         # remote calls arrive as text
    call = json.loads(reply.content.splitlines()[-1])
    assert call == {"name": "k8s_snapshot", "arguments": {"namespace": "auton"}}


def test_anthropic_adapter_uses_messages_api(cfgdir, monkeypatch):
    _remote(cfgdir, monkeypatch, provider="claude")
    monkeypatch.setenv("AGENT_LLM_BASE_URL", "https://api.anthropic.com")
    captured = {}

    def fake_post(url, headers, payload, timeout):
        captured.update(url=url, headers=headers, payload=payload)
        return {"content": [
            {"type": "text", "text": "looking"},
            {"type": "tool_use", "name": "k8s_get",
             "input": {"resource": "pods"}},
        ]}

    monkeypatch.setattr(llm, "_post_json", fake_post)
    tools = [{
        "type": "function",
        "function": {
            "name": "k8s_get",
            "description": "get",
            "parameters": {
                "type": "object",
                "properties": {"resource": {"type": "string"}},
            },
        },
    }]
    reply = asyncio.run(llm.chat(
        [{"role": "system", "content": "be brief"},
         {"role": "user", "content": "list pods"}], tools))

    assert captured["url"] == "https://api.anthropic.com/v1/messages"
    assert captured["headers"]["x-api-key"] == "sk-test-123456789"
    assert captured["headers"]["anthropic-version"]
    assert captured["payload"]["system"] == "be brief"
    assert captured["payload"]["messages"] == [{"role": "user", "content": "list pods"}]
    assert captured["payload"]["tools"][0]["input_schema"]["type"] == "object"
    assert json.loads(reply.content.splitlines()[-1])["name"] == "k8s_get"


def test_local_only_refuses_remote_without_any_http(cfgdir, monkeypatch):
    _remote(cfgdir, monkeypatch, policy="local-only")

    def boom(*args, **kwargs):
        raise AssertionError("must not call a remote provider")

    monkeypatch.setattr(llm, "_post_json", boom)
    with pytest.raises(llm.LLMError, match="local-only"):
        asyncio.run(llm.chat([{"role": "user", "content": "hi"}], []))


def test_missing_key_is_an_error_not_a_local_fallback(cfgdir, monkeypatch):
    monkeypatch.setenv("AGENT_PROVIDER", "openai")
    monkeypatch.delenv("AGENT_LLM_API_KEY", raising=False)

    async def no_local(*args):
        raise AssertionError("fallback to ollama is forbidden")

    monkeypatch.setattr(llm, "_ollama_chat", no_local)
    with pytest.raises(llm.LLMError, match="no API key"):
        asyncio.run(llm.chat([{"role": "user", "content": "hi"}], []))


def test_ollama_preserves_native_tool_calls(cfgdir, monkeypatch):
    monkeypatch.setenv("AGENT_PROVIDER", "ollama")
    captured = {}

    class FakeClient:
        def __init__(self, host):
            captured["host"] = host

        async def chat(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(message=SimpleNamespace(
                content="",
                tool_calls=[SimpleNamespace(function=SimpleNamespace(
                    name="bash", arguments={"command": "ls"}))],
            ))

    monkeypatch.setattr(llm.ollama, "AsyncClient", FakeClient)
    reply = asyncio.run(llm.chat([{"role": "user", "content": "hi"}],
                                 [{"type": "function", "function": {"name": "bash"}}]))
    assert captured["host"] == "http://localhost:11434"
    assert captured["tools"][0]["function"]["name"] == "bash"
    assert reply.tool_calls == [{"name": "bash", "arguments": {"command": "ls"}}]
