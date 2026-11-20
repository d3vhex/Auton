#!/usr/bin/env bash
# Enable or disable the **executor write scope** (Phase 4).
#
# The LLM executor (`auton-ops`, ServiceAccount `auton-agent`) is read-only by
# default: it may read, it may not change. This script is the *only* supported
# way to change that, because it changes both halves of the boundary at once:
#
#   1. the RBAC  — `yamls/auton-rbac-write.yaml` is applied (or deleted);
#   2. the tools — `AGENT_K8S_WRITE` on the executor Deployment is set to 1
#      (or back to 0) and the rollout is awaited.
#
# Doing only one of the two would leave the platform in a confusing state:
# RBAC without the flag gives "Forbidden" errors from the API server, the flag
# without RBAC gives a clean refusal from the tool. Both are safe; neither is
# what an operator meant.
#
# The repair path is untouched by this script: the scanner and the worker keep
# their own identities and their own, policy-gated permissions.
#
# Usage:
#   scripts/set-agent-writes.sh --enable     # executor may mutate
#   scripts/set-agent-writes.sh --disable    # back to read-only (default)
#   scripts/set-agent-writes.sh --status     # report the current scope
set -euo pipefail

NS="${AUTON_NAMESPACE:-auton}"
APPS_NS="${AUTON_APPS_NAMESPACE:-auton-apps}"
DEPLOY="auton-ops"
SA="auton-agent"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WRITE_MANIFEST="$ROOT/yamls/auton-rbac-write.yaml"

usage() {
  sed -n '2,25p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit 1
}

info() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
ok()   { printf '    \033[1;32mok\033[0m   %s\n' "$*"; }
bad()  { printf '    \033[1;31mFAIL\033[0m %s\n' "$*" >&2; }

current_flag() {
  kubectl -n "$NS" get deploy "$DEPLOY" \
    -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="AGENT_K8S_WRITE")].value}' 2>/dev/null || true
}

rbac_enabled() {
  if kubectl -n "$NS" get rolebinding auton-agent-write >/dev/null 2>&1; then
    return 0
  fi
  return 1
}

status() {
  local flag binding
  flag="$(current_flag)"
  if rbac_enabled; then binding="applied"; else binding="absent"; fi
  printf 'executor write scope: '
  if [[ "$flag" == "1" && "$binding" == "applied" ]]; then
    printf '\033[1;31mWRITES ENABLED\033[0m (AGENT_K8S_WRITE=%s, RBAC %s)\n' "$flag" "$binding"
  else
    printf '\033[1;32mread-only\033[0m (AGENT_K8S_WRITE=%s, RBAC %s)\n' "${flag:-unset}" "$binding"
  fi
  printf '  kubectl auth can-i patch deployments -n %s --as=system:serviceaccount:%s:%s -> ' \
    "$APPS_NS" "$NS" "$SA"
  kubectl auth can-i patch deployments -n "$APPS_NS" \
    --as="system:serviceaccount:$NS:$SA" 2>/dev/null || echo "no"
}

enable() {
  info "applying the executor write RBAC"
  kubectl apply -f "$WRITE_MANIFEST"
  info "setting AGENT_K8S_WRITE=1 on deploy/$DEPLOY"
  kubectl -n "$NS" set env "deploy/$DEPLOY" AGENT_K8S_WRITE=1
  kubectl -n "$NS" rollout status "deploy/$DEPLOY" --timeout=300s
  ok "the executor may now mutate (every action is still audit-gated)"
  status
}

disable() {
  info "removing the executor write RBAC"
  kubectl delete -f "$WRITE_MANIFEST" --ignore-not-found
  info "setting AGENT_K8S_WRITE=0 on deploy/$DEPLOY"
  kubectl -n "$NS" set env "deploy/$DEPLOY" AGENT_K8S_WRITE=0
  kubectl -n "$NS" rollout status "deploy/$DEPLOY" --timeout=300s
  ok "the executor is read-only again"
  status
}

case "${1:---status}" in
  --enable)  enable ;;
  --disable) disable ;;
  --status)  status ;;
  -h|--help) usage ;;
  *) echo "unknown argument: $1" >&2; usage ;;
esac
