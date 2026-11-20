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
#   ./scripts/install.sh --allow-writes  # give the LLM executor write rights
#   ./scripts/install.sh --sync          # regenerate yamls/ from the chart first
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
# The LLM executor is READ-ONLY by default (Phase 4): its RBAC grants
# get/list/watch only. --allow-writes adds the write Role and flips the
# executor's AGENT_K8S_WRITE flag; without it the flag is cleared and the
# write Role is removed, so re-running the installer is enough to revoke.
#
# Phase 6: charts/auton-chart is the source of truth and yamls/ is generated from
# it. The installer refuses to apply manifests that drifted from the chart;
# --sync regenerates them first (it needs helm).
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
ALLOW_WRITES=0
SYNC=0

usage() {
  sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
  exit 0
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --create-cluster) CREATE_CLUSTER=1 ;;
    --skip-build)     SKIP_BUILD=1 ;;
    --skip-model-pull) SKIP_MODEL_PULL=1 ;;
    --no-ingress)     WITH_INGRESS=0 ;;
    --allow-writes)   ALLOW_WRITES=1 ;;
    --sync)           SYNC=1 ;;
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
if [[ "$SYNC" == 1 ]]; then
  step "Regenerating yamls/ from charts/auton-chart (Phase 6)"
  run "$ROOT/scripts/sync-yamls.sh"
fi

if [[ "$VERIFY_ONLY" == 0 ]]; then
  # Phase 6: the chart is the source of truth. Installing manifests that
  # drifted from it would deploy something nobody reviewed.
  step "Checking that yamls/ matches the chart"
  if command -v python3 >/dev/null 2>&1 \
     && python3 -c 'import yaml' >/dev/null 2>&1; then
    if ! python3 "$ROOT/scripts/sync-yamls.py" --check; then
      echo
      echo "yamls/ is out of date. Run: scripts/sync-yamls.sh" >&2
      exit 1
    fi
    info "yamls/ is in sync with charts/auton-chart"
  else
    warn "PyYAML is not installed: the yamls/ drift check was skipped"
  fi

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

  step "RBAC (auton-agent, auton-api, auton-scanner, auton-worker, cluster read-only)"
  run kubectl apply -f yamls/auton-rbac.yaml
  # Phase 4: the executor's write scope is opt-in. Applying the read-only
  # Role above already removed the write verbs from the `auton-agent`
  # identity, so the extra file is only needed when it was asked for.
  if [[ "$ALLOW_WRITES" == 1 ]]; then
    step "Executor write scope (opt-in): auton-agent-write"
    run kubectl apply -f yamls/auton-rbac-write.yaml
  else
    info "executor write scope: read-only (default; --allow-writes to enable)"
    run kubectl delete -f yamls/auton-rbac-write.yaml --ignore-not-found
  fi
  # Do not reset a provider someone switched with scripts/set-provider.sh.
  if kubectl -n "$NS" get configmap auton-model-config >/dev/null 2>&1; then
    info "provider config exists; keeping it (set-provider.sh changes it)"
  else
    run kubectl apply -f yamls/auton-model.yaml
  fi
  # Same for the repair policy: never reset a policy an operator tightened.
  if kubectl -n "$NS" get configmap auton-policy >/dev/null 2>&1; then
    info "repair policy exists; keeping it (set-policy.sh changes it)"
  else
    run kubectl apply -f yamls/auton-policy.yaml
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
  # Redis first: the API refuses to start with AGENT_QUEUE=redis and no backend
  # (no silent memory fallback), so applying it later would crash-loop the API.
  step "Redis queue (single instance, AOF on a PVC, password protected)"
  # The queue password lives in a Secret and is never written in a manifest.
  # An existing Secret is kept: re-running the installer must not lock the
  # running clients out. Rotate it with scripts/rotate-redis-password.sh.
  if kubectl -n "$NS" get secret auton-redis >/dev/null 2>&1; then
    info "Secret auton-redis exists; keeping the current password"
  else
    pw="${AUTON_REDIS_PASSWORD:-}"
    if [[ -z "$pw" ]]; then
      if command -v openssl >/dev/null 2>&1; then
        pw="$(openssl rand -hex 24)"
      else
        pw="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
      fi
    fi
    if ! printf '%s' "$pw" | grep -Eq '^[A-Za-z0-9_-]{16,}$'; then
      echo "the Redis password must be at least 16 characters from [A-Za-z0-9_-]" >&2
      exit 1
    fi
    if [[ "$DRY_RUN" == 1 ]]; then
      info "[dry-run] would create Secret auton-redis"
    else
      kubectl -n "$NS" create secret generic auton-redis \
        --from-literal=password="$pw" >/dev/null
      info "created (rotate it with: scripts/rotate-redis-password.sh)"
    fi
  fi
  run kubectl apply -f yamls/redis.yaml
  run kubectl -n "$NS" rollout status deploy/redis --timeout=300s

  step "Agent API (producer)"
  run kubectl apply -f yamls/agent-deployment.yaml
  if [[ "$IMAGE" != "auton:local" && "$DRY_RUN" == 0 ]]; then
    kubectl -n "$NS" set image deploy/auton "auton=$IMAGE" >/dev/null
  fi
  run kubectl -n "$NS" rollout status deploy/auton --timeout=300s

  step "Ops executor (the pod that runs the LLM)"
  run kubectl apply -f yamls/auton-ops-deployment.yaml
  if [[ "$IMAGE" != "auton:local" && "$DRY_RUN" == 0 ]]; then
    kubectl -n "$NS" set image deploy/auton-ops "ops=$IMAGE" >/dev/null
  fi
  # The write scope has two halves; keep them in step (Phase 4).
  if [[ "$ALLOW_WRITES" == 1 ]]; then
    run kubectl -n "$NS" set env deploy/auton-ops AGENT_K8S_WRITE=1
  else
    run kubectl -n "$NS" set env deploy/auton-ops AGENT_K8S_WRITE=0
  fi
  run kubectl -n "$NS" rollout status deploy/auton-ops --timeout=300s

  step "Scanners (read-only detection -> repair queue)"
  run kubectl apply -f yamls/auton-scanner-deployment.yaml
  if [[ "$IMAGE" != "auton:local" && "$DRY_RUN" == 0 ]]; then
    kubectl -n "$NS" set image deploy/auton-scanner "scanner=$IMAGE" >/dev/null
  fi
  run kubectl -n "$NS" rollout status deploy/auton-scanner --timeout=300s

  step "Repair workers (playbooks; the only writers)"
  run kubectl apply -f yamls/auton-worker-deployment.yaml
  if [[ "$IMAGE" != "auton:local" && "$DRY_RUN" == 0 ]]; then
    kubectl -n "$NS" set image deploy/auton-worker "worker=$IMAGE" >/dev/null
  fi
  run kubectl -n "$NS" rollout status deploy/auton-worker --timeout=300s

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

check "Redis answers PING (authenticated)" \
  kubectl -n "$NS" exec deploy/redis -- sh -c \
  'REDISCLI_AUTH="$(cat /etc/auton-redis/password)" redis-cli ping | grep -qx PONG'

check "Redis rejects an unauthenticated client" \
  kubectl -n "$NS" exec deploy/redis -- sh -c \
  'redis-cli ping 2>&1 | grep -q NOAUTH'

check "API /health answers" \
  kubectl -n "$NS" exec deploy/auton -- python -c \
  'import json,urllib.request; print(json.load(urllib.request.urlopen("http://127.0.0.1:8080/health", timeout=5)))'

check "API uses the redis queue" \
  kubectl -n "$NS" exec deploy/auton -- python -c \
  'import json,urllib.request,sys
h=json.load(urllib.request.urlopen("http://127.0.0.1:8080/health", timeout=5))
sys.exit(0 if h.get("queue") == "redis" else 1)'

check "API authenticates to the queue (Phase 5)" \
  kubectl -n "$NS" exec deploy/auton -- python -c \
  'import json,urllib.request,sys
h=json.load(urllib.request.urlopen("http://127.0.0.1:8080/health", timeout=5))
sys.exit(0 if h.get("queue_auth") == "password" else 1)'

check "API serves /tasks with the token" \
  kubectl -n "$NS" exec deploy/auton -- python -c \
  'import json,os,urllib.request
r=urllib.request.Request("http://127.0.0.1:8080/tasks?limit=1",headers={"Authorization":"Bearer "+os.environ["AGENT_API_TOKEN"]})
print(json.load(urllib.request.urlopen(r, timeout=5)).get("count", 0))'

check "API serves /incidents with the token" \
  kubectl -n "$NS" exec deploy/auton -- python -c \
  'import json,os,urllib.request
r=urllib.request.Request("http://127.0.0.1:8080/incidents?limit=1",headers={"Authorization":"Bearer "+os.environ["AGENT_API_TOKEN"]})
print(json.load(urllib.request.urlopen(r, timeout=5)).get("count", 0))'

check "API serves /approvals with the token" \
  kubectl -n "$NS" exec deploy/auton -- python -c \
    'import json,os,urllib.request
r=urllib.request.Request("http://127.0.0.1:8080/approvals?limit=1",headers={"Authorization":"Bearer "+os.environ["AGENT_API_TOKEN"]})
print(json.load(urllib.request.urlopen(r, timeout=5)).get("count", 0))'

check "API serves the effective policy" \
  kubectl -n "$NS" exec deploy/auton -- python -c \
    'import json,os,urllib.request
r=urllib.request.Request("http://127.0.0.1:8080/policy",headers={"Authorization":"Bearer "+os.environ["AGENT_API_TOKEN"]})
d=json.load(urllib.request.urlopen(r, timeout=5))
assert d["ok"] is True, d.get("error")
print(d["policy"]["mode"])'

check "API serves /learning with the token (Phase 7)" \
  kubectl -n "$NS" exec deploy/auton -- python -c \
    'import json,os,urllib.request
r=urllib.request.Request("http://127.0.0.1:8080/learning?limit=1",headers={"Authorization":"Bearer "+os.environ["AGENT_API_TOKEN"]})
d=json.load(urllib.request.urlopen(r, timeout=5))
assert d.get("enabled") is True, d.get("error")
print(d["stats"]["outcomes"])'

check "the worker and the API share the learning store (Phase 7)" \
  bash -c 'kubectl -n "$0" exec deploy/auton-worker -- test -d /workspace/.ops/learning &&
            kubectl -n "$0" exec deploy/auton -- test -d /workspace/.ops/learning' "$NS"

# D5: the Prometheus exposition must be served, in the right content type,
# and it must need the token.
check "API serves the Prometheus exposition (D5)" \
  kubectl -n "$NS" exec deploy/auton -- python -c \
    'import os,urllib.request
r=urllib.request.Request("http://127.0.0.1:8080/metrics",headers={"Authorization":"Bearer "+os.environ["AGENT_API_TOKEN"]})
with urllib.request.urlopen(r, timeout=10) as resp:
    body=resp.read().decode()
    assert resp.headers.get("Content-Type","").startswith("text/plain"), resp.headers.get("Content-Type")
for family in ("auton_up","auton_queue_depth","auton_watch_available","auton_incidents","auton_policy_info","process_resident_memory_bytes"):
    assert family in body, family
print(len(body.splitlines()), "lines")'

check "the metrics endpoint needs the token (D5)" \
  kubectl -n "$NS" exec deploy/auton -- python -c \
    'import urllib.request,urllib.error
try:
    urllib.request.urlopen("http://127.0.0.1:8080/metrics", timeout=10)
except urllib.error.HTTPError as exc:
    assert exc.code == 401, exc.code
    print("401")
else:
    raise SystemExit("the exposition was served without a token")'

check "the repair policy is mounted in the workers" \
  kubectl -n "$NS" exec deploy/auton-worker -- test -f /etc/auton-policy/policy.json

check "provider is configured (masked key)" \
  kubectl -n "$NS" exec deploy/auton -- python -c \
  'import json,os,urllib.request
r=urllib.request.Request("http://127.0.0.1:8080/provider",headers={"Authorization":"Bearer "+os.environ["AGENT_API_TOKEN"]})
print(json.load(urllib.request.urlopen(r, timeout=5))["provider"])'

check "agent cannot read secrets (RBAC)" \
  bash -c 'kubectl -n "$0" auth can-i get secrets --as=system:serviceaccount:auton:auton-agent | grep -qx no' "$NS"

check "API identity cannot list pods (RBAC)" \
  bash -c 'kubectl -n "$0" auth can-i list pods --as=system:serviceaccount:auton:auton-api | grep -qx no' "$NS"

check "scanner cannot patch deployments (RBAC)" \
  bash -c 'kubectl -n "$0" auth can-i patch deployments --as=system:serviceaccount:auton:auton-scanner | grep -qx no' "$APPS_NS"

check "worker cannot patch auton (RBAC)" \
  bash -c 'kubectl -n "$0" auth can-i patch deployments --as=system:serviceaccount:auton:auton-worker | grep -qx no' "$NS"

check "worker can patch auton-apps (RBAC)" \
  bash -c 'kubectl -n "$0" auth can-i patch deployments --as=system:serviceaccount:auton:auton-worker | grep -qx yes' "$APPS_NS"

check "scanner may watch pods (RBAC)" \
  bash -c 'kubectl -n "$0" auth can-i watch pods --as=system:serviceaccount:auton:auton-scanner | grep -qx yes' "$APPS_NS"

if [[ "$ALLOW_WRITES" == 1 ]]; then
  check "executor may write (RBAC, Phase 4 --allow-writes)" \
    bash -c 'kubectl -n "$0" auth can-i patch deployments --as=system:serviceaccount:auton:auton-agent | grep -qx yes' "$APPS_NS"
else
  check "executor is read-only (RBAC, Phase 4)" \
    bash -c 'kubectl -n "$0" auth can-i patch deployments --as=system:serviceaccount:auton:auton-agent | grep -qx no' "$APPS_NS"
fi

check "executor may still read (RBAC, Phase 4)" \
  bash -c 'kubectl -n "$0" auth can-i get pods --as=system:serviceaccount:auton:auton-agent | grep -qx yes' "$APPS_NS"

check "the scanner watches the API server" \
  bash -c 'for _ in $(seq 1 30); do
  if kubectl -n "$0" exec deploy/auton -- python -c \
    "import json,os,urllib.request
r=urllib.request.Request(\"http://127.0.0.1:8080/tasks/stats\",headers={\"Authorization\":\"Bearer \"+os.environ[\"AGENT_API_TOKEN\"]})
d=json.load(urllib.request.urlopen(r, timeout=5))
streams=[w for w in d.get(\"watchers\",[]) if w.get(\"resource\") in (\"pods\",\"deployments\")]
assert streams, \"no watcher stream is published\"
assert any(w.get(\"available\") for w in streams), \"no watcher is available\"
print(len(streams))" >/dev/null 2>&1; then exit 0; fi
  sleep 2
done; exit 1' "$NS"

# D6: the platform talks to the API server directly over HTTPS. The checks
# below prove that the direct client is present and used, and that the watch
# covers every resource the snapshot needs.
check "the direct API client reaches the API server (D6)" \
  kubectl -n "$NS" exec deploy/auton-scanner -- python -c \
    'import k8s_api
c = k8s_api.client()
assert c is not None, k8s_api.client_error()
items, rv = c.list("/api/v1/namespaces/auton-apps/pods")
assert rv, "no resourceVersion"
print(len(items), "pods, rv=" + rv)'

check "the k8s tools use the direct path, not kubectl (D6)" \
  kubectl -n "$NS" exec deploy/auton-worker -- python -c \
    'import k8s_api, k8s_tools
before = dict(k8s_api.COUNTERS)
out = k8s_tools.k8s_get("pods", namespace="auton-apps")
assert "ok" in out or "NAME" in out or "No resources" in out, out[:200]
after = k8s_api.COUNTERS
assert after["kubectl"] == before["kubectl"], "kubectl was used"
assert after["direct"] > before["direct"], "the direct path was not used"
print("direct=" + str(after["direct"]), "kubectl=" + str(after["kubectl"]))'

check "the watch covers the snapshot resources (D6)" \
  kubectl -n "$NS" exec deploy/auton -- python -c \
    'import json,os,urllib.request
r=urllib.request.Request("http://127.0.0.1:8080/tasks/stats",headers={"Authorization":"Bearer "+os.environ["AGENT_API_TOKEN"]})
d=json.load(urllib.request.urlopen(r, timeout=5))
resources={w.get("resource") for w in d.get("watchers",[])}
missing={"pods","deployments","statefulsets","persistentvolumeclaims","events"} - resources
assert not missing, "not watched: " + ",".join(sorted(missing))
print(",".join(sorted(resources)))'

check "the scanner publishes the transport counters (D6)" \
  kubectl -n "$NS" exec deploy/auton -- python -c \
    'import json,os,urllib.request
r=urllib.request.Request("http://127.0.0.1:8080/tasks/stats",headers={"Authorization":"Bearer "+os.environ["AGENT_API_TOKEN"]})
d=json.load(urllib.request.urlopen(r, timeout=5))
scanner=[w for w in d.get("watchers",[]) if w.get("resource")=="summary"]
assert scanner, "no scanner summary"
transport=scanner[0].get("transport") or {}
assert "direct" in transport and "kubectl" in transport, transport
print(transport)'

if command -v python3 >/dev/null 2>&1 \
   && python3 -c 'import yaml' >/dev/null 2>&1; then
  check "yamls/ is in sync with charts/auton-chart (Phase 6)" \
    python3 "$ROOT/scripts/sync-yamls.py" --check
else
  info "skipped the yamls/ drift check (PyYAML is not installed)"
fi

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
  Ops logs   kubectl -n $NS logs deploy/auton-ops --tail=50      (LLM executor)
  Scanner    kubectl -n $NS logs deploy/auton-scanner --tail=50  (read-only)
  Workers    kubectl -n $NS logs deploy/auton-worker --tail=50   (playbooks)
  Queue      kubectl -n $NS exec deploy/redis -- sh -c \\
               'REDISCLI_AUTH="\$(cat /etc/auton-redis/password)" redis-cli xlen auton:q:repair:normal'
  Rotate it  scripts/rotate-redis-password.sh --status
  Provider   scripts/set-provider.sh --provider ollama   (or openai|gemini|anthropic|custom)
  n8n        scripts/rotate-n8n-api-key.sh --help

Next: build the notification workflow, then POST a test incident:

  kubectl -n $NS exec deploy/auton -- python -c \\
    'import n8n_tools; print(n8n_tools.n8n_create_ops_pipeline(kind="both"))'
EOF
