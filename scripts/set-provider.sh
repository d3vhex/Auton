#!/usr/bin/env bash
# Switch the model provider used by the Auton agent (and, later, the ops
# controller — they mount the same ConfigMap/Secret).
#
# The agent reads this configuration on EVERY request from a projected volume:
#
#   /etc/auton-model/config/{provider,base_url,model,max_tokens,data_policy}
#   /etc/auton-model/secret/api-key
#
# so no pod restart is normally needed. The script updates the ConfigMap and
# Secret, waits for the files inside the pod, and verifies what the agent sees.
#
# Providers: ollama (default), openai, gemini, anthropic (alias: claude),
#            custom (OpenAI-compatible; --base-url is required).
#
# When the provider is not ollama the local model is NOT used — there is no
# fallback. The API key is written to the auton-model Secret; the value is
# never echoed and never passed on a command line.
#
# Usage:
#   scripts/set-provider.sh --provider openai --model gpt-4o-mini \
#       --api-key-file /tmp/opencode/openai.key
#   scripts/set-provider.sh --provider claude --model claude-sonnet-4-5 \
#       --api-key-file ~/.config/auton/anthropic.key --test
#   scripts/set-provider.sh --provider custom --base-url http://vllm:8000/v1 \
#       --model qwen2.5-coder-32b --api-key-file /tmp/opencode/vllm.key
#   printf '%s' "$KEY" | scripts/set-provider.sh --provider openai --api-key-stdin
#   scripts/set-provider.sh --provider ollama          # back to local
#   scripts/set-provider.sh --provider gemini --model gemini-2.0-flash \
#       --api-key-file key.txt --data-policy local-only   # refuse remote data
#
# Environment: NAMESPACE, CONFIGMAP, SECRET_NAME, SECRET_KEY, DEPLOYMENT,
#              SYNC_TIMEOUT, POLL_INTERVAL.
set -euo pipefail

NAMESPACE="${NAMESPACE:-auton}"
CONFIGMAP="${CONFIGMAP:-auton-model-config}"
SECRET_NAME="${SECRET_NAME:-auton-model}"
SECRET_KEY="${SECRET_KEY:-api-key}"
DEPLOYMENT="${DEPLOYMENT:-auton}"
CONFIG_DIR="${CONFIG_DIR:-/etc/auton-model/config}"
SECRET_FILE="${SECRET_FILE:-/etc/auton-model/secret/api-key}"
SYNC_TIMEOUT="${SYNC_TIMEOUT:-180}"
POLL_INTERVAL="${POLL_INTERVAL:-5}"

PROVIDER=""
MODEL=""
BASE_URL=""
MAX_TOKENS=""
DATA_POLICY=""
KEY_FILE=""
READ_STDIN=0
CLEAR_KEY=0
RUN_TEST=0

die() { echo "error: $*" >&2; exit 1; }
say() { echo "==> $*"; }

usage() {
  awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"
  exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --provider)      PROVIDER="${2:-}"; shift 2 ;;
    --model)         MODEL="${2:-}"; shift 2 ;;
    --base-url)      BASE_URL="${2:-}"; shift 2 ;;
    --max-tokens)    MAX_TOKENS="${2:-}"; shift 2 ;;
    --data-policy)   DATA_POLICY="${2:-}"; shift 2 ;;
    --api-key-file)  KEY_FILE="${2:-}"; shift 2 ;;
    --api-key-stdin) READ_STDIN=1; shift ;;
    --clear-key)     CLEAR_KEY=1; shift ;;
    --test)          RUN_TEST=1; shift ;;
    --namespace)     NAMESPACE="${2:-}"; shift 2 ;;
    -h|--help)       usage 0 ;;
    *) die "unknown argument: $1 (try --help)" ;;
  esac
done

command -v kubectl >/dev/null || die "kubectl not found in PATH"
kubectl -n "$NAMESPACE" get deploy "$DEPLOYMENT" >/dev/null 2>&1 \
  || die "deployment $NAMESPACE/$DEPLOYMENT not found (is the cluster running?)"

# --- current values, so a partial update keeps the rest --------------------- #
current() {
  kubectl -n "$NAMESPACE" get configmap "$CONFIGMAP" \
    -o "jsonpath={.data.$1}" 2>/dev/null || true
}

PROVIDER="${PROVIDER:-$(current provider)}"
PROVIDER="${PROVIDER:-ollama}"
PROVIDER="$(printf '%s' "$PROVIDER" | tr '[:upper:]' '[:lower:]')"
case "$PROVIDER" in
  claude) PROVIDER=anthropic ;;
  local)  PROVIDER=ollama ;;
  openai-compatible) PROVIDER=openai ;;
esac
case "$PROVIDER" in
  ollama|openai|gemini|anthropic|custom) ;;
  *) die "unknown provider '$PROVIDER' (ollama|openai|gemini|anthropic|custom)" ;;
esac

MODEL_ARG="$MODEL"
BASE_URL_ARG="$BASE_URL"
MAX_TOKENS_ARG="$MAX_TOKENS"
DATA_POLICY_ARG="$DATA_POLICY"

MODEL="${MODEL:-$(current model)}"
BASE_URL="${BASE_URL:-$(current base_url)}"
MAX_TOKENS="${MAX_TOKENS:-$(current max_tokens)}"
DATA_POLICY="${DATA_POLICY:-$(current data_policy)}"

# Sensible defaults per provider, but never overwrite an explicit value.
default_model() {
  case "$1" in
    ollama)    echo "qwen2.5-coder:7b-instruct" ;;
    openai)    echo "gpt-4o-mini" ;;
    gemini)    echo "gemini-2.0-flash" ;;
    anthropic) echo "claude-sonnet-4-5" ;;
    custom)    echo "" ;;
  esac
}
default_base() {
  case "$1" in
    ollama)    echo "http://ollama-service:11434" ;;
    openai)    echo "https://api.openai.com/v1" ;;
    gemini)    echo "https://generativelanguage.googleapis.com/v1beta/openai" ;;
    anthropic) echo "https://api.anthropic.com" ;;
    custom)    echo "" ;;
  esac
}

# A provider switch should not silently keep the previous provider's URL/model.
PREV_PROVIDER="$(current provider)"
if [[ -n "$PREV_PROVIDER" && "$PREV_PROVIDER" != "$PROVIDER" ]]; then
  [[ -n "$MODEL_ARG" ]] || MODEL=""
  [[ -n "$BASE_URL_ARG" ]] || BASE_URL=""
  [[ -n "$MAX_TOKENS_ARG" ]] || MAX_TOKENS="1024"
  [[ -n "$DATA_POLICY_ARG" ]] || DATA_POLICY="redact"
fi
MODEL="${MODEL:-$(default_model "$PROVIDER")}"
BASE_URL="${BASE_URL:-$(default_base "$PROVIDER")}"
MAX_TOKENS="${MAX_TOKENS:-1024}"
DATA_POLICY="${DATA_POLICY:-redact}"

case "$DATA_POLICY" in
  redact|allow|local-only) ;;
  *) die "data-policy must be redact, allow or local-only" ;;
esac
if [[ "$PROVIDER" == custom && -z "$BASE_URL" ]]; then
  die "provider=custom requires --base-url (e.g. http://vllm:8000/v1)"
fi
[[ -n "$MODEL" ]] || die "provider=$PROVIDER requires --model"

# --- key: never on the command line, never echoed --------------------------- #
TMP_KEY=""
if (( CLEAR_KEY )); then
  TMP_KEY="$(mktemp "${TMPDIR:-/tmp}/auton-model-key.XXXXXX")"
  chmod 600 "$TMP_KEY"
  : > "$TMP_KEY"
elif [[ -n "$KEY_FILE" ]]; then
  [[ -r "$KEY_FILE" ]] || die "cannot read key file: $KEY_FILE"
  TMP_KEY="$(mktemp "${TMPDIR:-/tmp}/auton-model-key.XXXXXX")"
  chmod 600 "$TMP_KEY"
  tr -d '\r\n' < "$KEY_FILE" > "$TMP_KEY"
elif (( READ_STDIN )); then
  TMP_KEY="$(mktemp "${TMPDIR:-/tmp}/auton-model-key.XXXXXX")"
  chmod 600 "$TMP_KEY"
  tr -d '\r\n' > "$TMP_KEY"
fi
if [[ -n "$TMP_KEY" ]]; then
  trap 'rm -f "$TMP_KEY"' EXIT
fi

if [[ "$PROVIDER" != ollama ]]; then
  EXISTING_KEY="$(kubectl -n "$NAMESPACE" get secret "$SECRET_NAME" \
    -o "jsonpath={.data.${SECRET_KEY}}" 2>/dev/null | base64 -d 2>/dev/null || true)"
  if [[ -z "$TMP_KEY" && -z "$EXISTING_KEY" ]]; then
    die "provider=$PROVIDER needs an API key: pass --api-key-file (or pipe it on stdin)"
  fi
fi

# --- apply ConfigMap (non-secret settings) --------------------------------- #
say "ConfigMap $NAMESPACE/$CONFIGMAP -> provider=$PROVIDER model=$MODEL policy=$DATA_POLICY"
kubectl -n "$NAMESPACE" create configmap "$CONFIGMAP" \
  --from-literal=provider="$PROVIDER" \
  --from-literal=base_url="$BASE_URL" \
  --from-literal=model="$MODEL" \
  --from-literal=max_tokens="$MAX_TOKENS" \
  --from-literal=data_policy="$DATA_POLICY" \
  --dry-run=client -o yaml \
  | kubectl -n "$NAMESPACE" apply -f - >/dev/null

if [[ -n "$TMP_KEY" ]]; then
  say "Updating Secret $NAMESPACE/$SECRET_NAME (value hidden)"
  kubectl -n "$NAMESPACE" create secret generic "$SECRET_NAME" \
    --from-file="$SECRET_KEY=$TMP_KEY" --dry-run=client -o yaml \
    | kubectl -n "$NAMESPACE" apply -f - >/dev/null
fi

# --- wait for the projected files to refresh ------------------------------- #
read_config() {
  kubectl -n "$NAMESPACE" exec "deploy/$DEPLOYMENT" -- \
    cat "$CONFIG_DIR/$1" 2>/dev/null | tr -d '\r\n'
}

say "Waiting up to ${SYNC_TIMEOUT}s for the pod to see the new configuration"
deadline=$((SECONDS + SYNC_TIMEOUT))
synced=0
while (( SECONDS < deadline )); do
  if [[ "$(read_config provider)" == "$PROVIDER" \
        && "$(read_config model)" == "$MODEL" ]]; then
    synced=1
    break
  fi
  sleep "$POLL_INTERVAL"
done

if (( ! synced )); then
  say "Projection did not catch up; restarting $NAMESPACE/$DEPLOYMENT (one-off)"
  kubectl -n "$NAMESPACE" rollout restart "deploy/$DEPLOYMENT" >/dev/null
  kubectl -n "$NAMESPACE" rollout status "deploy/$DEPLOYMENT" --timeout=180s
  [[ "$(read_config provider)" == "$PROVIDER" ]] \
    || die "the pod still reports provider=$(read_config provider)"
fi

# --- what the agent's own code sees ---------------------------------------- #
say "Agent view:"
kubectl -n "$NAMESPACE" exec "deploy/$DEPLOYMENT" -- python -c \
  'import llm, json; print(json.dumps(llm.provider_info(), indent=2))'

if (( RUN_TEST )); then
  say "Sending a test completion"
  kubectl -n "$NAMESPACE" exec "deploy/$DEPLOYMENT" -- python -c \
    'import asyncio, json, llm; print(json.dumps(asyncio.run(llm.test()), indent=2))'
fi

if [[ "$PROVIDER" == ollama ]]; then
  say "Local Ollama is active; remote providers are disabled."
else
  say "Provider $PROVIDER is active. The local model is NOT used (no fallback)."
  say "Remote calls redact secrets when data_policy=$DATA_POLICY."
fi
