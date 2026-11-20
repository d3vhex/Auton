#!/usr/bin/env bash
# Rotate the password of the Auton task queue (Redis).
#
# The queue is the one component whose password is baked into a running
# process: Redis reads `requirepass` when it starts, and every client reads
# the mounted Secret once, when it starts. So a rotation is a *restart* of the
# server and of all four clients — there is no way to change the password of
# a running Redis without a restart.
#
# The order matters:
#   1. the Secret changes (the new pods mount the new value);
#   2. Redis restarts and comes back with the new password;
#   3. the clients restart and connect with the new password.
#
# Between (2) and (3) the clients cannot authenticate, so they fail closed
# (they refuse to start, they do not fall back to an unauthenticated
# connection). The window is seconds; the script waits for every Deployment
# and verifies the result.
#
# The password is never written in a manifest, never passed on a command line
# and never printed. It must be at least 16 characters from [A-Za-z0-9_-] —
# the Redis server writes it into a config file, and that alphabet keeps the
# file unambiguous.
#
# Usage:
#   scripts/rotate-redis-password.sh                     # generate one
#   scripts/rotate-redis-password.sh --password-file FILE
#   scripts/rotate-redis-password.sh --stdin             # read from stdin
#   scripts/rotate-redis-password.sh --status            # show the state
#
# Environment: AUTON_NAMESPACE, AUTON_REDIS_SECRET, AUTON_REDIS_DEPLOYMENTS.
set -euo pipefail

NAMESPACE="${AUTON_NAMESPACE:-auton}"
SECRET="${AUTON_REDIS_SECRET:-auton-redis}"
SECRET_KEY="password"
MOUNT_PATH="/etc/auton-redis/password"
# Every pod that talks to the queue, in the order they should come back.
DEPLOYMENTS="${AUTON_REDIS_DEPLOYMENTS:-redis auton auton-ops auton-scanner auton-worker}"

TMP_PW=""
MODE="generate"

die() { echo "error: $*" >&2; exit 1; }
say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
ok()  { printf '    \033[1;32mok\033[0m   %s\n' "$*"; }

usage() {
  awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"
  exit "${1:-0}"
}

cleanup() { [[ -n "$TMP_PW" ]] && rm -f "$TMP_PW"; }
trap cleanup EXIT

while [[ $# -gt 0 ]]; do
  case "$1" in
    --password-file) MODE="file"; PW_FILE="${2:-}"; shift 2 ;;
    --stdin)        MODE="stdin"; shift ;;
    --status)       MODE="status"; shift ;;
    -h|--help)      usage 0 ;;
    *) die "unknown argument: $1 (try --help)" ;;
  esac
done

command -v kubectl >/dev/null || die "kubectl not found in PATH"

# --- status ----------------------------------------------------------------- #
if [[ "$MODE" == "status" ]]; then
  say "queue password state"
  if kubectl -n "$NAMESPACE" get secret "$SECRET" >/dev/null 2>&1; then
    ok "Secret $NAMESPACE/$SECRET exists (key $SECRET_KEY)"
  else
    echo "    Secret $NAMESPACE/$SECRET is MISSING — the clients cannot authenticate" >&2
  fi
  printf '    redis-server requires a password: '
  kubectl -n "$NAMESPACE" exec deploy/redis -- sh -c \
    'redis-cli ping 2>&1 | head -1' 2>/dev/null || echo "(redis not reachable)"
  printf '    unauthenticated client is rejected: '
  kubectl -n "$NAMESPACE" exec deploy/redis -- sh -c \
    'redis-cli ping 2>&1 | grep -q NOAUTH && echo yes || echo NO' 2>/dev/null || echo "?"
  exit 0
fi

# --- obtain the new password ------------------------------------------------ #
TMP_PW="$(mktemp "${TMPDIR:-/tmp}/auton-redis-pw.XXXXXX")"
chmod 600 "$TMP_PW"

case "$MODE" in
  file)
    [[ -n "${PW_FILE:-}" ]] || die "--password-file needs a path"
    [[ -r "$PW_FILE" ]] || die "cannot read password file: $PW_FILE"
    tr -d '\r\n' < "$PW_FILE" > "$TMP_PW"
    ;;
  stdin)
    [[ ! -t 0 ]] || die "--stdin needs a pipe"
    tr -d '\r\n' > "$TMP_PW"
    ;;
  generate)
    if command -v openssl >/dev/null 2>&1; then
      openssl rand -hex 24 > "$TMP_PW"
    else
      head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n' > "$TMP_PW"
    fi
    ;;
esac

NEW_PW="$(cat "$TMP_PW")"
[[ -n "$NEW_PW" ]] || die "the new password is empty"
if ! printf '%s' "$NEW_PW" | grep -Eq '^[A-Za-z0-9_-]{16,}$'; then
  die "the password must be at least 16 characters from [A-Za-z0-9_-]"
fi

OLD_PW="$(kubectl -n "$NAMESPACE" get secret "$SECRET" \
  -o jsonpath="{.data.${SECRET_KEY}}" 2>/dev/null | base64 -d 2>/dev/null || true)"
if [[ "$OLD_PW" == "$NEW_PW" ]]; then
  say "Secret $SECRET already holds this password; nothing to rotate."
  exit 0
fi

# --- 1. the Secret ---------------------------------------------------------- #
say "Updating Secret $NAMESPACE/$SECRET key $SECRET_KEY"
kubectl -n "$NAMESPACE" create secret generic "$SECRET" \
  --from-file="$SECRET_KEY=$TMP_PW" --dry-run=client -o yaml \
  | kubectl -n "$NAMESPACE" apply -f - >/dev/null
ok "Secret updated"

# --- 2. the server ---------------------------------------------------------- #
say "Restarting Redis (it reads requirepass when it starts)"
kubectl -n "$NAMESPACE" rollout restart "deploy/redis" >/dev/null
kubectl -n "$NAMESPACE" rollout status "deploy/redis" --timeout=300s
ok "Redis is back with the new password"

# --- 3. the clients --------------------------------------------------------- #
for dep in $DEPLOYMENTS; do
  [[ "$dep" == "redis" ]] && continue
  say "Restarting $dep (it reads the password once, at startup)"
  kubectl -n "$NAMESPACE" rollout restart "deploy/$dep" >/dev/null
  kubectl -n "$NAMESPACE" rollout status "deploy/$dep" --timeout=300s
  ok "$dep is back"
done

# --- 4. verify -------------------------------------------------------------- #
# kubectl prints a "Defaulted container ..." notice on stderr, so only stdout
# is compared here.
say "Verifying"
out="$(kubectl -n "$NAMESPACE" exec deploy/redis -- sh -c \
  "REDISCLI_AUTH=\"\$(cat $MOUNT_PATH)\" redis-cli ping" 2>/dev/null || true)"
[[ "$out" == "PONG" ]] || die "the authenticated PING failed: $out"
ok "an authenticated client gets PONG"

out="$(kubectl -n "$NAMESPACE" exec deploy/redis -- sh -c \
  'redis-cli ping' 2>&1 || true)"
[[ "$out" == *NOAUTH* ]] || die "an unauthenticated client was NOT rejected: $out"
ok "an unauthenticated client is rejected (NOAUTH)"

for dep in $DEPLOYMENTS; do
  [[ "$dep" == "redis" ]] && continue
  out="$(kubectl -n "$NAMESPACE" exec "deploy/$dep" -- python -c \
    'import ops_queue; print(ops_queue.redis_password() != "")' 2>/dev/null || true)"
  [[ "$out" == "True" ]] || die "$dep does not see a queue password: $out"
done
ok "every client reads the new password"

say "Done. The old password is no longer accepted by Redis."
