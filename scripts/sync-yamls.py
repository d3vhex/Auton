#!/usr/bin/env python3
"""Write yamls/ from a Helm render of charts/auton-chart.

The Helm chart is the **source of truth** for the Kubernetes manifests
(Phase 6). The raw files in ``yamls/`` are generated artifacts: they are
what ``kubectl apply`` consumes, and they must not be edited by hand.

This module holds the layout logic and is used by three callers:

* ``scripts/sync-yamls.py`` — writes the files (the command operators run).
* ``mcp/tests/test_manifests.py`` — proves the files are current.
* ``scripts/install.sh`` — ``--check`` mode, a fast drift check that needs
  no helm binary.

Usage
-----
    scripts/sync-yamls.py --render <file> [--render-write <file>] --write
    scripts/sync-yamls.py --check

The file layout is fixed: one file per role, so ``kubectl apply -f yamls/<file>``
still means something and the runbook stays readable.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import sys

try:
    import yaml
except ImportError:  # pragma: no cover - depends on the host
    sys.exit("sync-yamls.py needs PyYAML: pip install pyyaml")

ROOT = pathlib.Path(__file__).resolve().parents[1]
YAMLS = ROOT / "yamls"
CHART = ROOT / "charts" / "auton-chart"
VALUES = CHART / "values-k3d.yaml"
STAMP = YAMLS / ".chart-stamp"

#: The banner every generated file starts with.
BANNER = """\
# GENERATED — DO NOT EDIT.
#
# Source of truth: charts/auton-chart (Helm chart) with charts/auton-chart/values-k3d.yaml
# Regenerate:      scripts/sync-yamls.sh
#
# Edit the chart and re-run the script: a hand edit here is overwritten and
# fails the drift test in mcp/tests/test_manifests.py.
"""

#: Files that are **not** generated: they belong to the cluster, not to the
#: chart (the GPU device plugin and a one-shot smoke-test pod).
HAND_WRITTEN = ("k8s-ds-amdgpu-dp.yaml", "gpu-test.yaml")

#: The write scope is opt-in, so it is rendered separately and lives in its
#: own file — the same split scripts/set-agent-writes.sh relies on.
WRITE_NAMES = ("auton-agent-write",)

#: A stable order inside every generated file.
KIND_RANK = {
    "Namespace": 0,
    "ResourceQuota": 1,
    "LimitRange": 2,
    "ConfigMap": 3,
    "Secret": 4,
    "ServiceAccount": 5,
    "Role": 6,
    "RoleBinding": 7,
    "ClusterRole": 8,
    "ClusterRoleBinding": 9,
    "PersistentVolumeClaim": 10,
    "Job": 11,
    "Deployment": 12,
    "Service": 13,
    "Ingress": 14,
    "IngressRoute": 14,
}

#: name -> file, for the resources whose file is not implied by their kind.
NAMED = {
    "agent-workspace": "agent-deployment.yaml",
    "auton": "agent-deployment.yaml",
    "auton-ops": "auton-ops-deployment.yaml",
    "auton-scanner": "auton-scanner-deployment.yaml",
    "auton-worker": "auton-worker-deployment.yaml",
    "ollama-models": "ollama-deployment.yaml",
    "ollama": "ollama-deployment.yaml",
    "ollama-service": "ollama-deployment.yaml",
    "n8n-data": "n8n-deployment.yaml",
    "n8n": "n8n-deployment.yaml",
    "redis-data": "redis.yaml",
    "redis": "redis.yaml",
    "auton-model-config": "auton-model.yaml",
    "auton-model": "auton-model.yaml",
    "auton-policy": "auton-policy.yaml",
    "auton-routes": "ingress.yaml",
}

#: The order the files are written in (and the order the runbook lists them).
FILE_ORDER = (
    "namespace.yaml",
    "auton-apps.yaml",
    "auton-rbac.yaml",
    "auton-rbac-write.yaml",
    "auton-model.yaml",
    "auton-policy.yaml",
    "agent-deployment.yaml",
    "auton-ops-deployment.yaml",
    "auton-scanner-deployment.yaml",
    "auton-worker-deployment.yaml",
    "ollama-deployment.yaml",
    "n8n-deployment.yaml",
    "redis.yaml",
    "ingress.yaml",
)

_SEPARATOR = re.compile(r"(?m)^---\s*$")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: pathlib.Path) -> str:
    return sha256_bytes(path.read_bytes())


def chart_inputs_digest(chart: pathlib.Path = CHART) -> str:
    """A digest over every file of the chart.

    Any change to a template or a values file changes the digest, which is
    what the drift test compares against the stamp.
    """
    digest = hashlib.sha256()
    for path in sorted(chart.rglob("*")):
        if not path.is_file() or path.name.startswith("."):
            continue
        rel = path.relative_to(chart).as_posix()
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def split_documents(text: str) -> list[str]:
    """Split a rendered chart into its documents, keeping the comments.

    Helm separates documents with a ``---`` line and prefixes each one with
    a ``# Source:`` comment. Both are worth keeping in the generated file,
    so the raw text is split instead of re-dumping the parsed objects.
    """
    chunks = []
    for chunk in _SEPARATOR.split(text):
        stripped = chunk.strip("\n")
        if not stripped.strip():
            continue
        parsed = [doc for doc in yaml.safe_load_all(stripped) if doc]
        if len(parsed) != 1:
            raise ValueError(
                "a rendered chunk holds %d documents (a '---' inside a "
                "scalar?):\n%s" % (len(parsed), stripped[:400]))
        chunks.append(stripped)
    return chunks


def target_file(doc: dict) -> str:
    """The file a rendered resource belongs to."""
    kind = doc.get("kind") or ""
    meta = doc.get("metadata") or {}
    name = meta.get("name") or ""
    namespace = meta.get("namespace") or ""

    if kind == "Namespace":
        return "namespace.yaml" if name == "auton" else "auton-apps.yaml"
    if kind in ("ResourceQuota", "LimitRange"):
        return "auton-apps.yaml"
    if kind in ("ServiceAccount", "ClusterRole", "ClusterRoleBinding"):
        return "auton-rbac.yaml"
    if kind in ("Role", "RoleBinding"):
        if name in WRITE_NAMES:
            return "auton-rbac-write.yaml"
        return "auton-rbac.yaml"
    if kind in ("Ingress", "IngressRoute"):
        return "ingress.yaml"
    if kind == "Job":
        return "ollama-deployment.yaml"
    if name in NAMED:
        return NAMED[name]
    raise ValueError(f"no target file for {kind} {namespace}/{name}")


def group(chunks: list[str]) -> dict[str, list[str]]:
    """Group rendered documents into the yamls/ file layout."""
    files: dict[str, list[str]] = {}
    for chunk in chunks:
        doc = next(iter([d for d in yaml.safe_load_all(chunk) if d]))
        files.setdefault(target_file(doc), []).append(chunk)
    return files


def _sort_key(chunk: str) -> tuple:
    doc = next(iter([d for d in yaml.safe_load_all(chunk) if d]))
    meta = doc.get("metadata") or {}
    kind = doc.get("kind") or ""
    return (KIND_RANK.get(kind, 99), meta.get("namespace") or "",
            meta.get("name") or "")


def render_file(chunks: list[str]) -> str:
    """The text of one generated file."""
    body = "\n---\n".join(sorted(chunks, key=_sort_key))
    return f"{BANNER}\n{body}\n"


def write_all(files: dict[str, list[str]], out_dir: pathlib.Path = YAMLS) -> dict:
    """Write every generated file and the stamp. Returns the stamp."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written = {}
    for name in FILE_ORDER:
        chunks = files.get(name)
        if not chunks:
            continue
        path = out_dir / name
        path.write_text(render_file(chunks), encoding="utf-8")
        written[name] = sha256_file(path)
    # A file that is no longer produced by the chart must not linger.
    for path in out_dir.glob("*.yaml"):
        if path.name not in written and path.name not in HAND_WRITTEN:
            path.unlink()
    return written


def build_stamp(files: dict[str, str], render: pathlib.Path,
                render_write: pathlib.Path | None) -> dict:
    return {
        "chart": "charts/auton-chart",
        "values": "charts/auton-chart/values-k3d.yaml",
        "chart_inputs_sha256": chart_inputs_digest(),
        "render_sha256": sha256_file(render),
        "render_write_scope_sha256": (
            sha256_file(render_write) if render_write else None),
        "files": files,
    }


def check(out_dir: pathlib.Path = YAMLS) -> list[str]:
    """Return the drift problems; an empty list means the files are current.

    Needs no helm binary: the stamp carries the digest of the chart inputs
    and of every generated file.
    """
    problems: list[str] = []
    stamp_path = out_dir / STAMP.name
    if not stamp_path.exists():
        return [f"{stamp_path.relative_to(ROOT)} is missing: "
                "run scripts/sync-yamls.sh"]
    try:
        stamp = json.loads(stamp_path.read_text())
    except ValueError as exc:
        return [f"{stamp_path.relative_to(ROOT)} is not valid JSON: {exc}"]

    current = chart_inputs_digest()
    if stamp.get("chart_inputs_sha256") != current:
        problems.append(
            "the chart changed since yamls/ was generated "
            f"(stamp {stamp.get('chart_inputs_sha256', '?')[:12]} != "
            f"chart {current[:12]}): run scripts/sync-yamls.sh")

    for name, digest in sorted((stamp.get("files") or {}).items()):
        path = out_dir / name
        if not path.exists():
            problems.append(f"yamls/{name} is missing: run scripts/sync-yamls.sh")
            continue
        actual = sha256_file(path)
        if actual != digest:
            problems.append(
                f"yamls/{name} was edited by hand (it is generated): "
                "run scripts/sync-yamls.sh")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--render", type=pathlib.Path,
                        help="the rendered chart (helm template output)")
    parser.add_argument("--render-write", type=pathlib.Path,
                        help="the render with agent.allowWrites=true")
    parser.add_argument("--write", action="store_true",
                        help="write yamls/ and the stamp")
    parser.add_argument("--check", action="store_true",
                        help="verify that yamls/ matches the chart")
    parser.add_argument("--out", type=pathlib.Path, default=YAMLS)
    args = parser.parse_args(argv)

    if args.check:
        problems = check(args.out)
        for problem in problems:
            print(f"drift: {problem}", file=sys.stderr)
        if problems:
            return 1
        print("yamls/ is in sync with charts/auton-chart")
        return 0

    if not args.render:
        parser.error("--render is required unless --check is used")

    chunks = split_documents(args.render.read_text(encoding="utf-8"))
    files = group(chunks)

    if args.render_write:
        write_chunks = split_documents(
            args.render_write.read_text(encoding="utf-8"))
        # Only the write scope comes from the second render: the rest of the
        # chart is identical there and would duplicate the first render.
        for chunk in write_chunks:
            doc = next(iter([d for d in yaml.safe_load_all(chunk) if d]))
            name = (doc.get("metadata") or {}).get("name") or ""
            if name in WRITE_NAMES:
                files.setdefault(target_file(doc), []).append(chunk)

    written = write_all(files, args.out)
    stamp = build_stamp(written, args.render, args.render_write)
    STAMP.write_text(json.dumps(stamp, indent=2, sort_keys=True) + "\n",
                     encoding="utf-8")
    for name in FILE_ORDER:
        if name in written:
            print(f"wrote yamls/{name}")
    print(f"wrote {STAMP.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
