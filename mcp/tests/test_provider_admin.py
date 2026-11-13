"""Provider settings write path (UI -> ConfigMap/Secret or files)."""
from __future__ import annotations

import json
import pathlib

import pytest

import audit
import llm
import provider_admin


@pytest.fixture()
def files(tmp_path, monkeypatch):
    """File store rooted in a temporary AGENT_MODEL_DIR."""
    root = tmp_path / "model"
    (root / "config").mkdir(parents=True)
    (root / "secret").mkdir(parents=True)
    (root / "config" / "provider").write_text("ollama\n")
    (root / "config" / "base_url").write_text("http://ollama-service:11434\n")
    (root / "config" / "model").write_text("qwen2.5-coder:7b-instruct\n")
    (root / "config" / "max_tokens").write_text("1024\n")
    (root / "config" / "data_policy").write_text("redact\n")
    monkeypatch.setenv("AGENT_MODEL_DIR", str(root))
    monkeypatch.setenv("AGENT_PROVIDER_STORE", "files")
    monkeypatch.delenv("AGENT_LLM_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    return root


def _read(root: pathlib.Path, name: str) -> str:
    return (root / "config" / name).read_text().strip()


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #

def test_unknown_field_is_rejected():
    with pytest.raises(provider_admin.ProviderError, match="unknown field"):
        provider_admin.validate({"provder": "ollama"})


def test_unknown_provider_is_rejected():
    with pytest.raises(provider_admin.ProviderError, match="unknown provider"):
        provider_admin.validate({"provider": "mystery"})


def test_local_only_refuses_remote_provider():
    with pytest.raises(provider_admin.ProviderError, match="local-only"):
        provider_admin.validate({"provider": "openai", "data_policy": "local-only",
                                 "api_key": "sk-test-123456789"})


def test_custom_requires_base_url():
    with pytest.raises(provider_admin.ProviderError, match="requires base_url"):
        provider_admin.validate({"provider": "custom", "api_key": "sk-test-1"})


def test_remote_provider_requires_a_key():
    with pytest.raises(provider_admin.ProviderError, match="requires an API key"):
        provider_admin.validate({"provider": "openai"})


def test_max_tokens_is_bounded():
    with pytest.raises(provider_admin.ProviderError, match="between 64 and 8192"):
        provider_admin.validate({"max_tokens": 10})
    with pytest.raises(provider_admin.ProviderError, match="must be an integer"):
        provider_admin.validate({"max_tokens": "lots"})


def test_key_and_clear_are_mutually_exclusive():
    with pytest.raises(provider_admin.ProviderError, match="not both"):
        provider_admin.validate({"api_key": "sk-test-1", "clear_api_key": True})


def test_provider_switch_fills_documented_defaults():
    plan = provider_admin.validate({"provider": "anthropic",
                                    "api_key": "sk-ant-123456"})
    assert plan["config"]["base_url"] == llm.DEFAULT_BASE["anthropic"]
    assert plan["config"]["model"] == llm.DEFAULT_MODELS["anthropic"]


# --------------------------------------------------------------------------- #
# File store
# --------------------------------------------------------------------------- #

def test_apply_writes_config_and_key(files, audit_dir):
    info = provider_admin.apply({"provider": "openai",
                                 "api_key": "sk-test-abcdef123456"})

    assert _read(files, "provider") == "openai"
    assert _read(files, "base_url") == llm.DEFAULT_BASE["openai"]
    assert _read(files, "model") == llm.DEFAULT_MODELS["openai"]
    assert (files / "secret" / "api-key").read_text().strip() == \
        "sk-test-abcdef123456"

    assert info["provider"] == "openai"
    assert info["local_model_enabled"] is False
    assert info["api_key_set"] is True
    assert info["api_key_change"] == "set"
    assert "sk-test" not in json.dumps(info)

    rows = audit.recent(10)
    assert [r["action"] for r in rows if r["phase"] == "begin"] == \
        ["provider_update"]
    assert rows[-1]["phase"] == "end" and rows[-1]["status"] == "ok"
    text = (audit_dir / "audit.jsonl").read_text()
    assert "provider_update" in text
    assert "sk-test-abcdef" not in text


def test_apply_keeps_the_existing_key_when_none_is_sent(files):
    provider_admin.apply({"provider": "openai", "api_key": "sk-test-keepme-1"})
    info = provider_admin.apply({"model": "gpt-4o", "max_tokens": 2048})
    assert _read(files, "model") == "gpt-4o"
    assert _read(files, "max_tokens") == "2048"
    assert info["api_key_change"] == "unchanged"
    assert (files / "secret" / "api-key").read_text().strip() == "sk-test-keepme-1"


def test_clear_key_removes_the_secret(files):
    provider_admin.apply({"provider": "openai", "api_key": "sk-test-goneme-1"})
    info = provider_admin.clear_key()
    assert not (files / "secret" / "api-key").exists()
    assert info["api_key_set"] is False
    # The provider is now misconfigured and reports it instead of falling back.
    assert info["ok"] is False


def test_no_op_apply_does_not_write(files, audit_dir):
    provider_admin.apply({"max_tokens": 1024})  # same value
    assert not (audit_dir / "audit.jsonl").exists()


def test_audit_failure_refuses_the_write(files, monkeypatch):
    def boom(*args, **kwargs):
        raise audit.AuditError("volume is gone")

    monkeypatch.setattr(audit, "begin", boom)
    with pytest.raises(provider_admin.ProviderError, match="audit log is not"):
        provider_admin.apply({"max_tokens": 4096})
    assert _read(files, "max_tokens") == "1024"


# --------------------------------------------------------------------------- #
# kubectl store
# --------------------------------------------------------------------------- #

def test_kubectl_store_patches_only_the_named_objects(monkeypatch, audit_dir):
    import k8s_tools

    monkeypatch.setenv("AGENT_PROVIDER_STORE", "kubectl")
    monkeypatch.setenv("AGENT_PROVIDER_NAMESPACE", "auton")
    calls: list[list[str]] = []

    def fake_execute(args, stdin=None, timeout=30, namespace=None):
        calls.append([namespace or "", *args])
        return "", None

    monkeypatch.setattr(k8s_tools, "_execute", fake_execute)
    monkeypatch.setattr(llm, "_api_key", lambda: "")

    provider_admin.apply({"provider": "gemini", "model": "gemini-2.0-flash",
                          "api_key": "sk-gemini-987654"})

    assert calls[0][:4] == ["auton", "patch", "configmap", "auton-model-config"]
    assert calls[1][:4] == ["auton", "patch", "secret", "auton-model"]
    config_patch = json.loads(calls[0][calls[0].index("-p") + 1])
    assert config_patch["data"]["provider"] == "gemini"
    secret_patch = json.loads(calls[1][calls[1].index("-p") + 1])
    assert secret_patch["stringData"]["api-key"] == "sk-gemini-987654"


def test_kubectl_failure_is_reported_and_audited(monkeypatch, audit_dir):
    import k8s_tools

    monkeypatch.setenv("AGENT_PROVIDER_STORE", "kubectl")

    def fake_execute(args, stdin=None, timeout=30, namespace=None):
        return "", '{"error": "Forbidden"}'

    monkeypatch.setattr(k8s_tools, "_execute", fake_execute)
    with pytest.raises(provider_admin.ProviderError, match="Forbidden"):
        provider_admin.apply({"max_tokens": 2048})

    rows = audit.recent(10)
    assert rows[-1]["status"] == "failed"
    assert "Forbidden" in rows[-1]["result"]


def test_describe_lists_editable_options(files):
    info = provider_admin.describe()
    assert info["editable"] is True
    assert info["store"] == "files"
    assert set(info["providers"]) == set(llm.PROVIDERS)
    assert info["defaults"]["ollama"]["model"] == llm.DEFAULT_MODELS["ollama"]
    assert "api_key" not in info
