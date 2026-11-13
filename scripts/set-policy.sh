#!/usr/bin/env bash
# Change the repair policy: what Auton may do, and when it may do it.
#
# The policy lives in ConfigMap `auton-policy` (namespace `auton`). The policy
# engine reads it on every decision, so a change takes effect without a
# restart — no pod rollout is needed.
#
#   scripts/set-policy.sh --show
#   scripts/set-policy.sh --mode observe-only
#   scripts/set-policy.sh --mode safe-auto --auto-tiers 0,1,2 --approval-tiers 3
#   scripts/set-policy.sh --deny-cause crashloop
#   scripts/set-policy.sh --require-approval-cause oom
#   scripts/set-policy.sh --reset
#
# The modes:
#
#   disabled           nothing runs — the off switch
#   observe-only       plans are computed and recorded, never executed
#   safe-auto          the auto tiers run unattended, the approval tiers wait
#   approval-required  every mutating tier waits for a human
#
# The tiers describe the risk of the action:
#
#   0  observe/report only            (no mutation)
#   1  transient and recreated         (restart a pod, undo a rollout, scale up)
#   2  changes the workload's spec     (raise a memory limit)
#   3  data-affecting / irreversible   (never automatic)
set -euo pipefail

NS="${AUTON_NAMESPACE:-auton}"
CONFIGMAP="${AUTON_POLICY_CONFIGMAP:-auton-policy}"
MODE=""
AUTO_TIERS=""
APPROVAL_TIERS=""
DENY_TIERS=""
DENY_CAUSE=""
REQUIRE_CAUSE=""
RESET=0
SHOW=0
JSON_FILE=""

usage() {
  sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
  cat <<'EOF'

Options:
  --show                     print the effective policy and exit
  --mode MODE                disabled | observe-only | safe-auto | approval-required
  --auto-tiers LIST          tiers that run unattended (e.g. 0,1,2)
  --approval-tiers LIST      tiers that wait for a human (e.g. 2,3)
  --deny-tiers LIST          tiers that are refused outright
  --deny-cause CAUSE         refuse one cause (a rule is added)
  --require-approval-cause CAUSE
                             make one cause wait for a human
  --json FILE                replace the whole policy document with FILE
  --reset                    restore the shipped default policy
  -h, --help                 this text
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --show) SHOW=1; shift ;;
    --mode) MODE="${2:-}"; shift 2 ;;
    --auto-tiers) AUTO_TIERS="${2:-}"; shift 2 ;;
    --approval-tiers) APPROVAL_TIERS="${2:-}"; shift 2 ;;
    --deny-tiers) DENY_TIERS="${2:-}"; shift 2 ;;
    --deny-cause) DENY_CAUSE="${2:-}"; shift 2 ;;
    --require-approval-cause) REQUIRE_CAUSE="${2:-}"; shift 2 ;;
    --json) JSON_FILE="${2:-}"; shift 2 ;;
    --reset) RESET=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

command -v kubectl >/dev/null 2>&1 || { echo "kubectl is required" >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "python3 is required" >&2; exit 1; }

if [[ "$SHOW" == 1 ]]; then
  kubectl -n "$NS" get configmap "$CONFIGMAP" -o jsonpath='{.data.mode}'; echo
  kubectl -n "$NS" get configmap "$CONFIGMAP" -o jsonpath='{.data.policy\.json}' \
    | python3 -c 'import json,sys; print(json.dumps(json.loads(sys.stdin.read().strip()), indent=2))'
  exit 0
fi

if [[ "$RESET" == 1 ]]; then
  echo "restoring the shipped default policy"
  kubectl apply -f "$(dirname "$0")/../yamls/auton-policy.yaml"
  kubectl -n "$NS" get configmap "$CONFIGMAP" -o jsonpath='{.data.mode}'; echo
  exit 0
fi

if [[ -n "$JSON_FILE" ]]; then
  [[ -f "$JSON_FILE" ]] || { echo "no such file: $JSON_FILE" >&2; exit 1; }
  # Validate before writing: a broken document would deny every repair.
  python3 -c '
import json, sys
doc = json.load(open(sys.argv[1]))
if not isinstance(doc, dict):
    raise SystemExit("the policy must be a JSON object")
mode = str(doc.get("mode", "")).strip()
if mode not in ("disabled", "observe-only", "safe-auto", "approval-required"):
    raise SystemExit("mode must be one of disabled, observe-only, safe-auto, approval-required")
' "$JSON_FILE"
  kubectl -n "$NS" patch configmap "$CONFIGMAP" --type merge \
    -p "$(python3 -c '
import json, sys
print(json.dumps({"data": {"policy.json": open(sys.argv[1]).read()}}))
' "$JSON_FILE")"
  echo "policy document replaced"
  exit 0
fi

if [[ -z "$MODE" && -z "$AUTO_TIERS" && -z "$APPROVAL_TIERS" && -z "$DENY_TIERS" \
      && -z "$DENY_CAUSE" && -z "$REQUIRE_CAUSE" ]]; then
  echo "nothing to change (use --show, --mode, --tiers, --deny-cause …)" >&2
  usage >&2
  exit 2
fi

# Build the new document from the current one, so a partial change keeps
# everything else.
current="$(kubectl -n "$NS" get configmap "$CONFIGMAP" -o jsonpath='{.data.policy\.json}')"
if [[ -z "$current" ]]; then
  echo "configmap/$CONFIGMAP has no policy.json yet" >&2
  exit 1
fi

new_doc="$(printf '%s' "$current" | python3 -c '
import json, sys

mode = sys.argv[1]
auto = sys.argv[2]
approval = sys.argv[3]
deny = sys.argv[4]
deny_cause = sys.argv[5]
require_cause = sys.argv[6]

doc = json.loads(sys.stdin.read().strip() or "{}")


def tiers(text):
    if not text:
        return None
    out = []
    for part in text.split(","):
        part = part.strip()
        if part:
            number = int(part)
            if number not in (0, 1, 2, 3):
                raise SystemExit("tiers must be 0, 1, 2 or 3")
            out.append(number)
    return sorted(set(out))


if mode:
    if mode not in ("disabled", "observe-only", "safe-auto", "approval-required"):
        raise SystemExit("unknown mode: " + mode)
    doc["mode"] = mode
if auto:
    doc["auto_approve_tiers"] = tiers(auto)
if approval:
    doc["approval_tiers"] = tiers(approval)
if deny:
    doc["deny_tiers"] = tiers(deny)

rules = list(doc.get("rules") or [])
for cause, action in ((deny_cause, "deny"), (require_cause, "require_approval")):
    if not cause:
        continue
    # Replace an existing rule for the same cause instead of stacking them.
    rules = [r for r in rules
             if not ((r.get("match") or {}).get("cause") == cause)]
    rules.append({"match": {"cause": cause}, "action": action})
doc["rules"] = rules
print(json.dumps(doc, indent=2))
' "$MODE" "$AUTO_TIERS" "$APPROVAL_TIERS" "$DENY_TIERS" "$DENY_CAUSE" "$REQUIRE_CAUSE")"

kubectl -n "$NS" patch configmap "$CONFIGMAP" --type merge \
  -p "$(python3 -c '
import json, sys
print(json.dumps({"data": {"policy.json": sys.stdin.read()}}))
' <<<"$new_doc")"

# The flat key is the quick switch and must agree with the document.
if [[ -n "$MODE" ]]; then
  kubectl -n "$NS" patch configmap "$CONFIGMAP" --type merge \
    -p "$(python3 -c 'import json,sys; print(json.dumps({"data": {"mode": sys.argv[1]}}))' "$MODE")"
fi

echo "policy updated (effective immediately, no restart needed):"
kubectl -n "$NS" get configmap "$CONFIGMAP" -o jsonpath='{.data.mode}'; echo
