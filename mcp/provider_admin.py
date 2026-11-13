#!/usr/bin/env python3
"""Write path for model-provider settings (UI -> cluster).

`GET /provider` only reports state. This module is the other half: it validates
a settings payload, writes it to the ConfigMap/Secret the agent already reads on
every request, and records the change in the audit log **without the key**.

Two stores:

  AGENT_PROVIDER_STORE=kubectl (default)
      patch ConfigMap `auton-model-config` and Secret `auton-model` in
      AGENT_PROVIDER_NAMESPACE (default AGENT_NAMESPACE, i.e. `auton`).
      Requires only `patch` on those two named objects (see auton-rbac.yaml);
      the API never needs `get secrets`.

  AGENT_PROVIDER_STORE=files
      write $AGENT_MODEL_DIR/config/* and $AGENT_MODEL_DIR/secret/api-key
      directly. Local development and the test suite; no cluster needed.

Invariants (same ones llm.py enforces at request time, checked before writing so
the UI cannot store a configuration the agent would then refuse):

  * a remote provider requires an API key (no silent fallback to Ollama)
  * data_policy=local-only refuses remote providers
  * custom requires base_url
  * the key is never returned, logged or copied into the audit record
"""
from __future__ import annotations

import json
import os
import pathlib

import audit
import llm

FIELDS = ("provider", "base_url", "model", "max_tokens", "data_policy")
POLICIES = ("redact", "allow", "local-only")
SECRET_FIELD = "api-key"


class ProviderError(ValueError):
    """Invalid payload or a failed write; the message is safe to show in the UI."""


# --------------------------------------------------------------------------- #
# Location
# --------------------------------------------------------------------------- #

def store() -> str:
    return (os.environ.get("AGENT_PROVIDER_STORE", "kubectl").strip().lower()
            or "kubectl")


def namespace() -> str:
    return (os.environ.get("AGENT_PROVIDER_NAMESPACE")
            or os.environ.get("AGENT_NAMESPACE") or "auton").strip()


def configmap_name() -> str:
    return os.environ.get("AGENT_PROVIDER_CONFIGMAP", "auton-model-config").strip()


def secret_name() -> str:
    return os.environ.get("AGENT_PROVIDER_SECRET", "auton-model").strip()


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #

def _clean_str(value, what: str, *, limit: int = 512) -> str:
    if value is None:
        return ""
    if not isinstance(value, (str, int, float)):
        raise ProviderError(f"{what} must be a string")
    text = str(value).strip()
    if len(text) > limit:
        raise ProviderError(f"{what} is longer than {limit} characters")
    if any(ch in text for ch in "\r\n\t"):
        raise ProviderError(f"{what} must be a single line")
    return text


def _max_tokens(value) -> str:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        raise ProviderError("max_tokens must be an integer") from None
    if not 64 <= number <= 8192:
        raise ProviderError("max_tokens must be between 64 and 8192")
    return str(number)


def current() -> dict:
    """The active configuration as strings (never the key)."""
    try:
        cfg = llm.config()
    except llm.LLMError:
        return {"provider": "ollama",
                "base_url": llm.DEFAULT_BASE["ollama"],
                "model": llm.DEFAULT_MODELS["ollama"],
                "max_tokens": "1024", "data_policy": "redact"}
    return {name: str(cfg[name]) for name in FIELDS}


def validate(payload: dict, base: dict | None = None) -> dict:
    """Return a write plan; raises ProviderError with a UI-safe message.

    Plan shape::

        {"config": {all five fields},
         "config_changes": {only the fields that differ},
         "api_key": "sk-..." | "",
         "clear_key": bool}
    """
    if not isinstance(payload, dict):
        raise ProviderError("body must be a JSON object")
    unknown = set(payload) - set(FIELDS) - {"api_key", "clear_api_key"}
    if unknown:
        raise ProviderError("unknown field(s): " + ", ".join(sorted(unknown)))

    old = dict(base or current())
    merged = dict(old)

    if "provider" in payload:
        try:
            merged["provider"] = llm.canonical(_clean_str(payload["provider"],
                                                          "provider", limit=40))
        except llm.LLMError as exc:
            raise ProviderError(str(exc)) from None
    provider = merged["provider"]

    if "base_url" in payload:
        merged["base_url"] = _clean_str(payload["base_url"], "base_url",
                                        limit=300).rstrip("/")
    if "model" in payload:
        merged["model"] = _clean_str(payload["model"], "model", limit=200)
    if "max_tokens" in payload:
        merged["max_tokens"] = _max_tokens(payload["max_tokens"])
    if "data_policy" in payload:
        policy = _clean_str(payload["data_policy"], "data_policy",
                            limit=20).lower()
        if policy not in POLICIES:
            raise ProviderError("data_policy must be one of "
                                + ", ".join(POLICIES))
        merged["data_policy"] = policy

    # A provider switch without explicit base_url/model falls back to that
    # provider's documented defaults instead of inheriting Ollama's.
    if provider != old["provider"]:
        if "base_url" not in payload:
            merged["base_url"] = llm.DEFAULT_BASE.get(provider, "").rstrip("/")
        if "model" not in payload:
            merged["model"] = llm.DEFAULT_MODELS.get(provider, "")

    if provider == "custom" and not merged["base_url"]:
        raise ProviderError("provider=custom requires base_url "
                            "(e.g. http://vllm:8000/v1)")
    if not merged["model"] and provider in llm.DEFAULT_MODELS:
        merged["model"] = llm.DEFAULT_MODELS[provider]
    if not merged["model"]:
        raise ProviderError("model is required")
    if merged["data_policy"] == "local-only" and provider != "ollama":
        raise ProviderError("data_policy=local-only refuses remote providers; "
                            "pick redact/allow or switch back to ollama")
    if provider == "ollama" and not merged["base_url"]:
        merged["base_url"] = llm.DEFAULT_BASE["ollama"]

    clear_key = bool(payload.get("clear_api_key"))
    raw_key = payload.get("api_key")
    api_key = "" if raw_key is None else _clean_str(raw_key, "api_key",
                                                    limit=4096)
    if api_key and clear_key:
        raise ProviderError("send either api_key or clear_api_key, not both")
    if api_key and any(ch.isspace() for ch in api_key):
        raise ProviderError("api_key must not contain whitespace")

    # Remote providers must have a key before the config is switched, otherwise
    # every request fails and the UI would have "saved" a broken setup.
    if provider != "ollama" and not clear_key:
        available = api_key or llm._api_key()  # noqa: SLF001 - same package
        if not available:
            raise ProviderError(f"provider={provider} requires an API key; "
                                "send api_key with this request")

    changes = {name: merged[name] for name in FIELDS if merged[name] != old[name]}
    return {"config": merged, "config_changes": changes,
            "api_key": api_key, "clear_key": clear_key}


# --------------------------------------------------------------------------- #
# Writes
# --------------------------------------------------------------------------- #

def _atomic_write(path: pathlib.Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _write_files(plan: dict) -> str:
    root = pathlib.Path(llm.model_dir())
    for name, value in plan["config_changes"].items():
        _atomic_write(root / "config" / name, value + "\n")
    if plan["api_key"]:
        _atomic_write(root / "secret" / SECRET_FIELD, plan["api_key"] + "\n")
        return "wrote config + key"
    if plan["clear_key"]:
        try:
            (root / "secret" / SECRET_FIELD).unlink()
        except FileNotFoundError:
            pass
        return "wrote config; cleared key" if plan["config_changes"] else "cleared key"
    return "wrote config"


def _write_kubectl(plan: dict) -> str:
    import k8s_tools

    ns = namespace()
    steps: list[str] = []
    if plan["config_changes"]:
        patch = json.dumps({"data": plan["config_changes"]})
        _, error = k8s_tools._execute(  # noqa: SLF001 - shared kubectl helper
            ["patch", "configmap", configmap_name(), "--type", "merge",
             "-p", patch], namespace=ns)
        if error:
            raise ProviderError(f"cannot update ConfigMap "
                                f"{configmap_name()}: {error}")
        steps.append("configmap")
    if plan["api_key"]:
        patch = json.dumps({"stringData": {SECRET_FIELD: plan["api_key"]}})
        _, error = k8s_tools._execute(
            ["patch", "secret", secret_name(), "--type", "merge", "-p", patch],
            namespace=ns)
        if error:
            raise ProviderError(f"cannot update Secret {secret_name()}: {error}")
        steps.append("key")
    elif plan["clear_key"]:
        # null removes the key from the map (JSON merge-patch semantics).
        patch = json.dumps({"data": {SECRET_FIELD: None}})
        _, error = k8s_tools._execute(
            ["patch", "secret", secret_name(), "--type", "merge", "-p", patch],
            namespace=ns)
        if error:
            raise ProviderError(f"cannot clear Secret {secret_name()}: {error}")
        steps.append("key-cleared")
    return ", ".join(steps) or "no change"


def apply(payload: dict, *, actor: str = "api-manual") -> dict:
    """Validate, audit (fail-closed), write, audit the outcome."""
    plan = validate(payload)
    if not plan["config_changes"] and not plan["api_key"] and not plan["clear_key"]:
        return describe()
    key_op = ("set" if plan["api_key"]
              else "cleared" if plan["clear_key"] else "unchanged")
    args = {"changes": plan["config_changes"], "api_key": key_op,
            "store": store(), "namespace": namespace()}
    plan_text = (f"write {'/'.join(sorted(plan['config_changes'])) or 'nothing'} "
                 f"to ConfigMap {configmap_name()}"
                 + (f"; {key_op} api-key in Secret {secret_name()}"
                    if key_op != "unchanged" else ""))
    try:
        audit_id = audit.begin("provider_update", args, actor=actor,
                               plan=plan_text)
    except audit.AuditError as exc:
        raise ProviderError("refused: the audit log is not writable "
                            f"({exc})") from None

    try:
        result = (_write_files(plan) if store() == "files"
                  else _write_kubectl(plan))
    except ProviderError as exc:
        audit.finish(audit_id, status="failed", result=str(exc), actor=actor)
        raise
    except OSError as exc:
        audit.finish(audit_id, status="failed",
                     result=f"{type(exc).__name__}: {exc}", actor=actor)
        raise ProviderError(f"cannot write provider config: {exc}") from None

    audit.finish(audit_id, result=result, actor=actor)
    info = describe()
    info["audit_id"] = audit_id
    info["applied"] = plan["config_changes"]
    info["api_key_change"] = key_op
    return info


def clear_key(*, actor: str = "api-manual") -> dict:
    return apply({"clear_api_key": True}, actor=actor)


def describe() -> dict:
    """`GET /provider`: current state plus what the UI needs to edit it."""
    info = llm.provider_info()
    info["editable"] = True
    info["store"] = store()
    info["namespace"] = namespace()
    info["providers"] = list(llm.PROVIDERS)
    info["policies"] = list(POLICIES)
    info["defaults"] = {
        name: {"base_url": llm.DEFAULT_BASE.get(name, ""),
               "model": llm.DEFAULT_MODELS.get(name, "")}
        for name in llm.PROVIDERS
    }
    return info
