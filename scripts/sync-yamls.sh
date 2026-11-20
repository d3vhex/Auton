#!/usr/bin/env bash
# Regenerate yamls/ from the Helm chart (Phase 6).
#
# The chart in charts/auton-chart is the source of truth for every Kubernetes
# manifest; yamls/ is the generated artifact that `kubectl apply` consumes.
#
#   scripts/sync-yamls.sh            # rewrite yamls/
#   scripts/sync-yamls.sh --check    # fail if yamls/ drifted from the chart
#
# The check mode needs no helm binary; the write mode does.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHART="$ROOT/charts/auton-chart"
VALUES="$CHART/values-k3d.yaml"
YAMLS="$ROOT/yamls"
RELEASE="auton"
NAMESPACE="auton"

SYNC="$ROOT/scripts/sync-yamls.py"

# --- locate helm ------------------------------------------------------------
HELM="${HELM:-}"
if [ -z "$HELM" ]; then
    if command -v helm >/dev/null 2>&1; then
        HELM="$(command -v helm)"
    elif [ -x /tmp/opencode/helm ]; then
        HELM=/tmp/opencode/helm
    else
        echo "sync-yamls: helm is not installed." >&2
        echo "  Install it, or set HELM=/path/to/helm." >&2
        exit 2
    fi
fi

PYTHON="${PYTHON:-}"
if [ -z "$PYTHON" ]; then
    if command -v python3 >/dev/null 2>&1; then
        PYTHON=python3
    else
        PYTHON=python
    fi
fi

# --- check only -------------------------------------------------------------
if [ "${1:-}" = "--check" ]; then
    exec "$PYTHON" "$SYNC" --check
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "sync-yamls: rendering $CHART with $VALUES"
"$HELM" template "$RELEASE" "$CHART" \
    --namespace "$NAMESPACE" \
    -f "$VALUES" > "$TMP/render.yaml"

# The executor's write scope is opt-in (Phase 4), so it is rendered from a
# second pass and written to its own file (yamls/auton-rbac-write.yaml).
echo "sync-yamls: rendering the opt-in write scope"
"$HELM" template "$RELEASE" "$CHART" \
    --namespace "$NAMESPACE" \
    -f "$VALUES" \
    --set agent.allowWrites=true > "$TMP/render-write.yaml"

"$PYTHON" "$SYNC" \
    --render "$TMP/render.yaml" \
    --render-write "$TMP/render-write.yaml" \
    --write \
    --out "$YAMLS"

echo "sync-yamls: done — yamls/ now matches charts/auton-chart"
