"""Phase 6: the Helm chart is the source of truth for yamls/.

``charts/auton-chart`` describes every Kubernetes object; ``yamls/`` is the
generated artifact that ``kubectl apply`` consumes. These tests fail when the
two drift apart — a hand edit in yamls/, a chart change that was not
regenerated, or a resource the generator does not know where to put.

The checks that need no helm binary run everywhere; the "fresh render"
comparison is skipped when helm is not installed.
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import shutil
import subprocess

import pytest

yaml = pytest.importorskip("yaml")

ROOT = pathlib.Path(__file__).resolve().parents[2]
YAMLS = ROOT / "yamls"
CHART = ROOT / "charts" / "auton-chart"
VALUES = CHART / "values-k3d.yaml"
STAMP = YAMLS / ".chart-stamp"


def _sync_module():
    """Import scripts/sync-yamls.py (it is the single source of the layout)."""
    path = ROOT / "scripts" / "sync-yamls.py"
    spec = importlib.util.spec_from_file_location("sync_yamls", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _helm() -> str | None:
    return os.environ.get("HELM") or shutil.which("helm")


# --------------------------------------------------------------------------- #
# The stamp: the chart and the generated files are in sync.
# --------------------------------------------------------------------------- #

def test_the_stamp_exists_and_names_the_chart():
    assert STAMP.exists(), (
        "yamls/ was never generated — run scripts/sync-yamls.sh")
    stamp = json.loads(STAMP.read_text())
    assert stamp["chart"] == "charts/auton-chart"
    assert stamp["values"] == "charts/auton-chart/values-k3d.yaml"
    assert stamp["files"], "the stamp lists no generated file"


def test_yamls_are_in_sync_with_the_chart():
    """No chart change and no hand edit since the last sync."""
    problems = _sync_module().check()
    assert not problems, "yamls/ drifted from charts/auton-chart:\n" + "\n".join(
        problems)


def test_every_generated_file_carries_the_banner():
    sync = _sync_module()
    stamp = json.loads(STAMP.read_text())
    for name in stamp["files"]:
        text = (YAMLS / name).read_text()
        assert "GENERATED" in text.splitlines()[0], name
        assert "scripts/sync-yamls.sh" in text, name
    assert stamp["files"], "no generated file"
    # Every file the generator knows about is on disk.
    for name in sync.FILE_ORDER:
        assert (YAMLS / name).exists(), name


def test_the_hand_written_files_are_not_generated():
    """The GPU plugin and the smoke test belong to the cluster, not the chart."""
    stamp = json.loads(STAMP.read_text())
    for name in ("k8s-ds-amdgpu-dp.yaml", "gpu-test.yaml"):
        path = YAMLS / name
        assert path.exists(), f"{name} disappeared"
        assert name not in stamp["files"], f"{name} must not be generated"
        assert "GENERATED" not in path.read_text(), name


def test_the_generator_knows_every_resource_of_the_render():
    """A new kind/name in the chart must be given a home, not silently lost."""
    sync = _sync_module()
    # The names the chart creates today; a new one has to be added to NAMED
    # (or handled by a rule) or this test fails.
    for name in ("auton", "auton-ops", "auton-scanner", "auton-worker",
                 "ollama", "ollama-service", "n8n", "redis",
                 "agent-workspace", "ollama-models", "n8n-data", "redis-data",
                 "auton-model", "auton-model-config", "auton-policy",
                 "auton-routes"):
        assert name in sync.NAMED or name in sync.WRITE_NAMES, name


# --------------------------------------------------------------------------- #
# The render itself: a fresh `helm template` must produce the files on disk.
# --------------------------------------------------------------------------- #

@pytest.mark.skipif(not _helm(), reason="helm is not installed")
def test_a_fresh_render_matches_the_files_on_disk(tmp_path):
    helm = _helm()
    sync = _sync_module()

    render = tmp_path / "render.yaml"
    render_write = tmp_path / "render-write.yaml"
    base = [helm, "template", "auton", str(CHART), "--namespace", "auton",
            "-f", str(VALUES)]
    subprocess.run(base, check=True, stdout=render.open("w"))
    subprocess.run(base + ["--set", "agent.allowWrites=true"], check=True,
                   stdout=render_write.open("w"))

    chunks = sync.split_documents(render.read_text())
    files = sync.group(chunks)
    for chunk in sync.split_documents(render_write.read_text()):
        doc = next(iter([d for d in yaml.safe_load_all(chunk) if d]))
        name = (doc.get("metadata") or {}).get("name") or ""
        if name in sync.WRITE_NAMES:
            files.setdefault(sync.target_file(doc), []).append(chunk)

    for name, expected in files.items():
        path = YAMLS / name
        assert path.exists(), f"{name} is missing"
        assert path.read_text() == sync.render_file(expected), (
            f"{name} does not match a fresh render of the chart — "
            "run scripts/sync-yamls.sh")


@pytest.mark.skipif(not _helm(), reason="helm is not installed")
def test_the_render_is_deterministic(tmp_path):
    """Two renders must be identical: no random value leaks into yamls/."""
    helm = _helm()
    base = [helm, "template", "auton", str(CHART), "--namespace", "auton",
            "-f", str(VALUES)]
    first = subprocess.run(base, check=True, capture_output=True, text=True)
    second = subprocess.run(base, check=True, capture_output=True, text=True)
    assert first.stdout == second.stdout, (
        "the render is not reproducible (a generated Secret value?): "
        "the Secrets must stay outside the chart render")
