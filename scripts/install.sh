#!/usr/bin/env bash
# Bring a fresh k3d host from zero to a working Auton installation.
#
# This is the executable version of README "Full runbook" steps 1-7. It is
# idempotent: every step checks first and re-running is safe. It never prints
# the API token value unless --show-token is passed.
#
#   ./scripts/install.sh                 # full install (cluster must exist)
#   ./scripts/install.sh --create-cluster
#   ./scripts/install.sh --skip-build    # use an already imported image
#   ./scripts/install.sh --skip-model-pull
#   ./scripts/install.sh --no-ingress
#   ./scripts/install.sh --verify        # only run the checks
#   ./scripts/install.sh --dry-run       # print the commands, change nothing
#   ./scripts/install.sh --show-token    # print the bearer token at the end
#
# Environment:
#   AUTON_IMAGE        image to deploy (default auton:local). When it is
#                      auton:local the script builds and imports it; any other
#                      value (e.g. docker.io/you/auton:0.1.0) is pulled.
#   AUTON_CLUSTER      k3d cluster name (default ai-cluster)
#   AUTON_TOKEN        bearer token to install instead of generating one
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

CLUSTER="${AUTON_CLUSTER:-ai-cluster}"
IMAGE="${AUTON_IMAGE:-auton:local}"
NS=auton
APPS_NS=auton-apps
CREATE_CLUSTER=0
SKIP_BUILD=0
SKIP_MODEL_PULL=0
WITH_INGRESS=1
DRY_RUN=0
VERIFY_ONLY=0
SHOW_TOKEN=0

usage() {
  sed -n '2,23p' "$0" | sed 's/^# \{0,1\}//'
  exit 0
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --create-cluster) CREATE_CLUSTER=1 ;;
    --skip-build)     SKIP_BUILD=1 ;;
    --skip-model-pull) SKIP_MODEL_PULL=1 ;;
    --no-ingress)     WITH_INGRESS=0 ;;
    --verify)         VERIFY_ONLY=1 ;;
    --dry-run)        DRY_RUN=1 ;;
    --show-token)     SHOW_TOKEN=1 ;;
    -h|--help)        usage ;;
    *) echo "unknown option: $1" >&2; usage ;;
  esac
  shift
done

step()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
info()  { printf '    %s\n' "$*"; }
warn()  { printf '\033[1;33m    ! %s\033[0m\n' "$*"; }
run() {
  if [[ "$DRY_RUN" == 1 ]]; then
    printf '    [dry-run] %s\n' "$*"
  else
    "$@"
  fi
}

need() {
  command -v "$1" >/dev/null 2>&1 || { echo "missing dependency: $1" >&2; exit 1; }
}

need kubectl
need k3d
[[ "$SKIP_BUILD" == 1 || "$IMAGE" != "auton:local" ]] || need docker

# --------------------------------------------------------------------------- #
# 0. Cluster
# --------------------------------------------------------------------------- #
if [[ "$VERIFY_ONLY" == 0 ]]; then
  if [[ "$CREATE_CLUSTER" == 1 ]]; then
    if k3d cluster list "$CLUSTER" >/dev/null 2>&1; then
      info "cluster $CLUSTER already exists"
    else
      step "Creating k3d cluster $CLUSTER (GPU passthrough + :80)"
      run k3d cluster create "$CLUSTER" \
        -v /dev/kfd:/dev/kfd -v /dev/dri:/dev/dri \
        -p "80:80@loadbalancer"
    fi
    run k3d kubeconfig merge "$CLUSTER" \
      --kubeconfig-merge-default --kubeconfig-switch-context
  fi

  if ! kubectl cluster-info >/dev/null 2>&1; then
    cat >&2 <<EOF
kubectl cannot reach a cluster. Either create it:

  ./scripts/install.sh --create-cluster

or, if the k3d containers are stopped:

  k3d cluster start $CLUSTER
  k3d kubeconfig merge $CLUSTER --kubeconfig-merge-default --kubeconfig-switch-context
EOF
    exit 1
  fi

  # ------------------------------------------------------------------------- #
  # 1. Namespaces, token, RBAC, provider config
  # ------------------------------------------------------------------------- #
  step "Namespaces and policy"
  run kubectl apply -f yamls/namespace.yaml
  run kubectl apply -f yamls/auton-apps.yaml

  step "API token (Secret auton-api-token)"
  if kubectl -n "$NS" get secret auton-api-token >/dev/null 2>&1; then
    info "already exists; keeping the current token"
  else
    token="${AUTON_TOKEN:-}"
    if [[ -z "$token" ]]; then
      if command -v openssl >/dev/null 2>&1; then
        token="$(openssl rand -hex 24)"
      else
        token="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
      fi
    fi
    if [[ "$DRY_RUN" == 1 ]]; then
      info "[dry-run] would create Secret auton-api-token"
    else
      kubectl -n "$NS" create secret generic auton-api-token \
        --from-literal=token="$token" >/dev/null
      info "created (read it with: kubectl -n $NS get secret auton-api-token -o jsonpath='{.data.token}' | base64 -d)"
    fi
  fi

  step "RBAC (auton-agent, auton-ops, cluster read-only)"
  run kubectl apply -f yamls/auton-rbac.yaml
  # Do not reset a provider someone switched with scripts/set-provider.sh.
  if kubectl -n "$NS" get configmap auton-model-config >/dev/null 2>&1; then
    info "provider config exists; keeping it (set-provider.sh changes it)"
  else
    run kubectl apply -f yamls/auton-model.yaml
  fi

  # ------------------------------------------------------------------------- #
  # 2. GPU device plugin (skipped when the node already advertises the GPU)
  # ------------------------------------------------------------------------- #
  gpu_capacity="$(kubectl get nodes -o jsonpath='{.items[0].status.capacity.amd\.com/gpu}' 2>/dev/null || true)"
  if [[ "${gpu_capacity:-}" == "1" ]]; then
    info "node already advertises amd.com/gpu=1"
  else
    step "Installing the AMD GPU device plugin"
    run kubectl apply -f yamls/k8s-ds-amdgpu-dp.yaml
    run kubectl -n kube-system rollout status ds/amdgpu-device-plugin-daemonset --timeout=180s
  fi

  # ------------------------------------------------------------------------- #
  # 3. Ollama + model
  # ------------------------------------------------------------------------- #
  step "Ollama"
  run kubectl apply -f yamls/ollama-deployment.yaml
  run kubectl -n "$NS" rollout status deploy/ollama --timeout=300s

  if [[ "$SKIP_MODEL_PULL" == 0 ]]; then
    step "Model qwen2.5-coder:7b-instruct (PVC-backed, survives restarts)"
    if [[ "$DRY_RUN" == 1 ]]; then
      info "[dry-run] would pull the model if absent"
    elif kubectl -n "$NS" exec deploy/ollama -- ollama list 2>/dev/null \
         | grep -q 'qwen2.5-coder:7b-instruct'; then
      info "already present"
    else
      kubectl -n "$NS" exec deploy/ollama -- \
        ollama pull qwen2.5-coder:7b-instruct
    fi
  fi

  # ------------------------------------------------------------------------- #
  # 4. Agent image
  # ------------------------------------------------------------------------- #
  step "Image $IMAGE"
  if [[ "$SKIP_BUILD" == 1 ]]; then
    info "--skip-build: not building or importing"
  elif [[ "$IMAGE" == "auton:local" ]]; then
    run docker build -t "$IMAGE" ./mcp
    run k3d image import "$IMAGE" -c "$CLUSTER"
  else
    info "registry image: the Deployment will pull it (imagePullPolicy must not be IfNotPresent-only for a mutable tag)"
  fi

  # ------------------------------------------------------------------------- #
  # 5. Deployments
  # ------------------------------------------------------------------------- #
  step "Agent API"
  run kubectl apply -f yamls/agent-deployment.yaml
  if [[ "$IMAGE" != "auton:local" && "$DRY_RUN" == 0 ]]; then
    kubectl -n "$NS" set image deploy/auton "auton=$IMAGE" >/dev/null
  fi
  run kubectl -n "$NS" rollout status deploy/auton --timeout=300s

  step "Autonomous ops controller"
  run kubectl apply -f yamls/auton-ops-deployment.yaml
  if [[ "$IMAGE" != "auton:local" && "$DRY_RUN" == 0 ]]; then
    kubectl -n "$NS" set image deploy/auton-ops "ops=$IMAGE" >/dev/null
  fi
  run kubectl -n "$NS" rollout status deploy/auton-ops --timeout=300s

  step "n8n"
  run kubectl apply -f yamls/n8n-deployment.yaml
  run kubectl -n "$NS" rollout status deploy/n8n --timeout=300s

  if [[ "$WITH_INGRESS" == 1 ]]; then
    step "Ingress (Traefik, *.localhost)"
    run kubectl apply -f yamls/ingress.yaml
  fi
fi

# --------------------------------------------------------------------------- #
# 6. Verification
# --------------------------------------------------------------------------- #
step "Verification"

if [[ "$DRY_RUN" == 1 ]]; then
  info "[dry-run] stopping before checks"
  exit 0
fi

fail=0
check() {
  local label="$1"; shift
  if "$@" >/dev/null 2>&1; then
    printf '    \033[1;32mok\033[0m   %s\n' "$label"
  else
    printf '    \033[1;31mFAIL\033[0m %s\n' "$label"
    fail=$((fail + 1))
  fi
}

token="$(kubectl -n "$NS" get secret auton-api-token -o jsonpath='{.data.token}' 2>/dev/null | base64 -d || true)"
if [[ "$SHOW_TOKEN" == 1 && -n "$token" ]]; then
  info "token: $token"
fi

check "API /health answers" \
  kubectl -n "$NS" exec deploy/auton -- python -c \
  'import json,urllib.request; print(json.load(urllib.request.urlopen("http://127.0.0.1:8080/health", timeout=5)))'

check "API serves /incidents with the token" \
  kubectl -n "$NS" exec deploy/auton -- python -c \
  'import json,os,urllib.request
r=urllib.request.Request("http://127.0.0.1:8080/incidents?limit=1",headers={"Authorization":"Bearer "+os.environ["AGENT_API_TOKEN"]})
print(json.load(urllib.request.urlopen(r, timeout=5)).get("count", 0))'

check "provider is configured (masked key)" \
  kubectl -n "$NS" exec deploy/auton -- python -c \
  'import json,os,urllib.request
r=urllib.request.Request("http://127.0.0.1:8080/provider",headers={"Authorization":"Bearer "+os.environ["AGENT_API_TOKEN"]})
print(json.load(urllib.request.urlopen(r, timeout=5))["provider"])'

check "agent cannot read secrets (RBAC)" \
  bash -c 'kubectl -n "$0" auth can-i get secrets --as=system:serviceaccount:auton:auton-agent | grep -qx no' "$NS"

check "ops controller cannot patch auton" \
  bash -c 'kubectl -n "$0" auth can-i patch deployments --as=system:serviceaccount:auton:auton-ops | grep -qx no' "$NS"

check "ops controller can patch auton-apps" \
  bash -c 'kubectl -n "$0" auth can-i patch deployments --as=system:serviceaccount:auton:auton-ops | grep -qx yes' "$APPS_NS"

if [[ "$fail" -gt 0 ]]; then
  echo
  echo "verification failed ($fail check(s)). See README Troubleshooting." >&2
  exit 1
fi

cat <<EOF

Auton is installed.

  API        kubectl -n $NS port-forward svc/auton 8080:8080
             curl -s localhost:8080/health
  Ingress    http://auton.localhost (if :80 was mapped at cluster creation)
  Ops logs   kubectl -n $NS logs deploy/auton-ops --tail=50
  Provider   scripts/set-provider.sh --provider ollama   (or openai|gemini|anthropic|custom)
  n8n        scripts/rotate-n8n-api-key.sh --help

Next: build the notification workflow, then POST a test incident:

  kubectl -n $NS exec deploy/auton -- python -c \\
    'import n8n_tools; print(n8n_tools.n8n_create_ops_pipeline(kind="both"))'
EOF
