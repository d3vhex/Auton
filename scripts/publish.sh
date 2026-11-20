#!/usr/bin/env bash
# Build and publish the Auton image and Helm chart to Docker Hub.
#
# The image is pushed to docker.io/<namespace>/auton:<version> (and :latest);
# the chart is packaged and pushed as an OCI artifact to
# oci://registry-1.docker.io/<namespace>.
#
# The chart's push target is the *namespace*, because Helm appends the chart
# name to it: the chart lands in <namespace>/auton-chart. Docker Hub allows
# exactly two path segments (a target with a path of its own would produce an
# invalid three-segment repository and the push would be refused with
# "insufficient_scope"), and the chart cannot share the image's repository
# because a tag maps to one manifest and both want the version tag.
#
# Authentication: export DOCKERHUB_USER and DOCKERHUB_TOKEN (a token, not the
# account password), or run `docker login` / `helm registry login` yourself
# first. The script never prints the token.
#
#   ./scripts/publish.sh                 # build, push image + chart
#   ./scripts/publish.sh --no-push       # build and package only
#   ./scripts/publish.sh --image-only
#   ./scripts/publish.sh --chart-only
#   ./scripts/publish.sh --version 0.3.0 # must match charts/auton-chart/Chart.yaml
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

NAMESPACE="${DOCKERHUB_NAMESPACE:-d3vhec}"
IMAGE_NAME="${IMAGE_NAME:-auton}"
CHART_DIR="charts/auton-chart"
DIST_DIR="dist"

CHART_VERSION="$(awk '/^version:/{print $2; exit}' "$CHART_DIR/Chart.yaml")"
VERSION="${AUTON_VERSION:-$CHART_VERSION}"

PUBLISH_IMAGE=1
PUBLISH_CHART=1
PUSH=1

usage() {
  sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'
  exit 0
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-push)    PUSH=0 ;;
    --image-only) PUBLISH_CHART=0 ;;
    --chart-only) PUBLISH_IMAGE=0 ;;
    --version)    VERSION="${2:?missing version}"; shift ;;
    -h|--help)    usage ;;
    *) echo "unknown option: $1" >&2; usage ;;
  esac
  shift
done

if [[ "$VERSION" != "$CHART_VERSION" ]]; then
  echo "version mismatch: --version/ AUTON_VERSION=$VERSION but $CHART_DIR/Chart.yaml has $CHART_VERSION" >&2
  echo "bump Chart.yaml first (and appVersion) so the image tag and chart agree." >&2
  exit 1
fi

HELM="${HELM:-helm}"
need() { command -v "$1" >/dev/null 2>&1 || { echo "missing: $1" >&2; exit 1; }; }

IMAGE="docker.io/${NAMESPACE}/${IMAGE_NAME}:${VERSION}"

login() {
  if [[ -n "${DOCKERHUB_TOKEN:-}" ]]; then
    : "${DOCKERHUB_USER:?DOCKERHUB_USER is required with DOCKERHUB_TOKEN}"
    printf '%s' "$DOCKERHUB_TOKEN" | docker login -u "$DOCKERHUB_USER" --password-stdin >/dev/null
    if command -v "$HELM" >/dev/null 2>&1; then
      printf '%s' "$DOCKERHUB_TOKEN" \
        | "$HELM" registry login registry-1.docker.io -u "$DOCKERHUB_USER" --password-stdin >/dev/null
    fi
    echo "logged in as $DOCKERHUB_USER"
  else
    echo "no DOCKERHUB_TOKEN: relying on an existing docker/helm login"
  fi
}

if [[ "$PUBLISH_IMAGE" == 1 ]]; then
  need docker
  echo "==> building $IMAGE"
  docker build -t "$IMAGE" -t "docker.io/${NAMESPACE}/${IMAGE_NAME}:latest" ./mcp

  if [[ "$PUSH" == 1 ]]; then
    login
    echo "==> pushing $IMAGE"
    docker push "$IMAGE"
    docker push "docker.io/${NAMESPACE}/${IMAGE_NAME}:latest"
  fi
fi

if [[ "$PUBLISH_CHART" == 1 ]]; then
  need "$HELM"
  echo "==> packaging $CHART_DIR"
  mkdir -p "$DIST_DIR"
  "$HELM" lint "$CHART_DIR"
  "$HELM" package "$CHART_DIR" --destination "$DIST_DIR"

  if [[ "$PUSH" == 1 ]]; then
    login
    # Helm appends the chart name to the push target, so the target is the
    # namespace and the repository becomes <namespace>/<chart-name>. Docker
    # Hub allows exactly two path segments — a target with a path of its own
    # ("<namespace>/charts") would produce an invalid three-segment
    # repository and the push would be refused with "insufficient_scope".
    echo "==> pushing oci://registry-1.docker.io/${NAMESPACE}/auton-chart:${VERSION}"
    "$HELM" push "$DIST_DIR/auton-chart-${VERSION}.tgz" \
      "oci://registry-1.docker.io/${NAMESPACE}"
  fi
fi

echo
echo "done. install with:"
echo "  helm install auton oci://registry-1.docker.io/${NAMESPACE}/auton-chart \\"
echo "    --version $VERSION --namespace auton --create-namespace"
