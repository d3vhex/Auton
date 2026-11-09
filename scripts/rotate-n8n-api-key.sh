#!/usr/bin/env bash
# Rotate the n8n public-API key that the Auton agent uses.
#
# The agent reads /etc/n8n-api/api-key on every call (see N8N_API_KEY_FILE), so
# in principle only the Secret has to change. The file comes from a *projected*
# Secret volume, which kubelet refreshes asynchronously (typically within a
# minute, bounded by --sync-period). This script updates the Secret, waits for
# the file inside the pod to actually change, and only restarts the Deployment
# when the projection does not catch up.
#
# It does NOT create or revoke keys in n8n: create the new key first in the n8n
# UI (Settings -> n8n API, scopes: workflow + execution) or via the /rest API,
# then feed it here and revoke the old one afterwards.
#
# Usage:
#   scripts/rotate-n8n-api-key.sh --key-file /tmp/opencode/n8n-api-key
#   scripts/rotate-n8n-api-key.sh            # reads the key from stdin / prompt
#   scripts/rotate-n8n-api-key.sh --restart  # always restart after updating
#   scripts/rotate-n8n-api-key.sh --no-restart --skip-verify
#
# Environment: NAMESPACE, SECRET_NAME, SECRET_KEY, DEPLOYMENT, MOUNT_PATH.
set -euo pipefail

NAMESPACE="${NAMESPACE:-auton}"
SECRET_NAME="${SECRET_NAME:-n8n-api}"
SECRET_KEY="${SECRET_KEY:-api-key}"
DEPLOYMENT="${DEPLOYMENT:-auton}"
MOUNT_PATH="${MOUNT_PATH:-/etc/n8n-api/api-key}"
SYNC_TIMEOUT="${SYNC_TIMEOUT:-150}"   # seconds to wait for the projected volume
POLL_INTERVAL="${POLL_INTERVAL:-5}"

KEY_FILE=""
RESTART_MODE="auto"                   # auto | always | never
VERIFY=1

die() { echo "error: $*" >&2; exit 1; }
say() { echo "==> $*"; }

usage() {
  awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"
  exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --key-file)  KEY_FILE="${2:-}"; shift 2 ;;
    --restart)   RESTART_MODE="always"; shift ;;
    --no-restart) RESTART_MODE="never"; shift ;;
    --skip-verify) VERIFY=0; shift ;;
    --namespace) NAMESPACE="${2:-}"; shift 2 ;;
    -h|--help)   usage 0 ;;
    *) die "unknown argument: $1 (try --help)" ;;
  esac
done

command -v kubectl >/dev/null || die "kubectl not found in PATH"
kubectl -n "$NAMESPACE" get deploy "$DEPLOYMENT" >/dev/null 2>&1 \
  || die "deployment $NAMESPACE/$DEPLOYMENT not found (is the cluster running?)"

# --- obtain the new key without leaking it through the process list --------- #
TMP_KEY="$(mktemp "${TMPDIR:-/tmp}/n8n-api-key.XXXXXX")"
chmod 600 "$TMP_KEY"
trap 'rm -f "$TMP_KEY"' EXIT

if [[ -n "$KEY_FILE" ]]; then
  [[ -r "$KEY_FILE" ]] || die "cannot read key file: $KEY_FILE"
  tr -d '\r\n' < "$KEY_FILE" > "$TMP_KEY"
elif [[ ! -t 0 ]]; then
  tr -d '\r\n' > "$TMP_KEY"
else
  printf 'New n8n API key (input hidden): ' >&2
  IFS= read -rs NEW_KEY; echo >&2
  printf '%s' "$NEW_KEY" > "$TMP_KEY"
  unset NEW_KEY
fi

NEW_KEY_TRIMMED="$(cat "$TMP_KEY")"
[[ -n "$NEW_KEY_TRIMMED" ]] || die "the new key is empty"
[[ "$NEW_KEY_TRIMMED" != *[[:space:]]* ]] || die "the new key contains whitespace"

OLD_KEY="$(kubectl -n "$NAMESPACE" get secret "$SECRET_NAME" \
  -o jsonpath="{.data.${SECRET_KEY}}" 2>/dev/null | base64 -d 2>/dev/null || true)"
if [[ "$OLD_KEY" == "$NEW_KEY_TRIMMED" ]]; then
  say "Secret $SECRET_NAME/$SECRET_KEY already holds this key; nothing to rotate."
  exit 0
fi

# --- update the Secret (idempotent) ---------------------------------------- #
say "Updating Secret $NAMESPACE/$SECRET_NAME key $SECRET_KEY"
kubectl -n "$NAMESPACE" create secret generic "$SECRET_NAME" \
  --from-file="$SECRET_KEY=$TMP_KEY" --dry-run=client -o yaml \
  | kubectl -n "$NAMESPACE" apply -f - >/dev/null

# --- wait for the projected volume to catch up ----------------------------- #
read_in_pod() {
  kubectl -n "$NAMESPACE" exec "deploy/$DEPLOYMENT" -- cat "$MOUNT_PATH" 2>/dev/null \
    | tr -d '\r\n'
}

say "Waiting up to ${SYNC_TIMEOUT}s for $MOUNT_PATH to be refreshed"
deadline=$((SECONDS + SYNC_TIMEOUT))
synced=0
while (( SECONDS < deadline )); do
  if [[ "$(read_in_pod)" == "$NEW_KEY_TRIMMED" ]]; then
    synced=1
    break
  fi
  sleep "$POLL_INTERVAL"
done

if (( synced )); then
  say "Projected volume is up to date (no restart needed)."
else
  case "$RESTART_MODE" in
    never)
      die "projected volume did not refresh within ${SYNC_TIMEOUT}s and --no-restart was given"
      ;;
    auto|always)
      say "Volume did not refresh; restarting $NAMESPACE/$DEPLOYMENT"
      kubectl -n "$NAMESPACE" rollout restart "deploy/$DEPLOYMENT" >/dev/null
      kubectl -n "$NAMESPACE" rollout status "deploy/$DEPLOYMENT" --timeout=180s
      [[ "$(read_in_pod)" == "$NEW_KEY_TRIMMED" ]] \
        || die "the pod still does not see the new key after the restart"
      say "Pod is serving the new key."
      ;;
  esac
fi

# --- verify the key actually works against n8n ----------------------------- #
if (( VERIFY )); then
  say "Verifying the key with the agent's n8n tools"
  out="$(kubectl -n "$NAMESPACE" exec "deploy/$DEPLOYMENT" -- \
    python -c 'import n8n_tools; print(n8n_tools.n8n_workflows("list"))' 2>&1 || true)"
  if [[ "$out" == *'"ok": false'* || -z "$out" ]]; then
    echo "$out" >&2
    die "the key is in place but n8n rejected it (revoked? wrong scopes?)"
  fi
  say "n8n accepted the new key."
fi

say "Done. Revoke the old key in the n8n UI (Settings -> n8n API) if you have not already."
