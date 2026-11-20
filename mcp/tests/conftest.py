"""Shared fixtures for the agent tests.

`agent_server.WORKSPACE` is read once at import time, so the workspace path is
set here *before* importing the module. Each test gets the same directory,
emptied between tests.
"""
from __future__ import annotations

import os
import pathlib
import shutil
import sys
import tempfile

import pytest

WORKSPACE = pathlib.Path(tempfile.mkdtemp(prefix="agent-workspace-"))
os.environ["AGENT_WORKSPACE"] = str(WORKSPACE)
# Audit records live outside the workspace so workspace-listing tests stay clean.
AUDIT_DIR = pathlib.Path(tempfile.mkdtemp(prefix="agent-audit-"))
os.environ["AGENT_AUDIT_DIR"] = str(AUDIT_DIR)
# The Learn stage (Phase 7) keeps its outcomes and profiles in their own
# directory; it is emptied between tests (see the fixture below).
LEARN_DIR = pathlib.Path(tempfile.mkdtemp(prefix="agent-learn-"))
os.environ["AGENT_LEARN_DIR"] = str(LEARN_DIR)
# The HTTP tests exercise routing, not auth; production sets AGENT_API_TOKEN.
os.environ.setdefault("AGENT_AUTH_DISABLED", "1")
os.environ.setdefault("AGENT_K8S_ENABLED", "1")
os.environ.setdefault("AGENT_K8S_WRITE", "1")

# Make `import agent_server` work when pytest is run from mcp/.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))


def _empty(directory: pathlib.Path) -> None:
    for child in directory.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child, ignore_errors=True)
        else:
            try:
                child.unlink()
            except OSError:
                pass


@pytest.fixture()
def ws():
    """An empty workspace for the current test."""
    _empty(WORKSPACE)
    yield WORKSPACE


@pytest.fixture(autouse=True)
def isolated_learning():
    """Every test starts with an empty learning store.

    The Learn stage writes outcomes and profiles to disk; without this the
    state of one test would leak into the next (a learned OOM step, a
    quarantined resource, ...).
    """
    import learning
    import ops_controller

    _empty(LEARN_DIR)
    service = learning.LearningService(learning.LearningStore(str(LEARN_DIR)))
    previous = ops_controller.LEARNING
    ops_controller.LEARNING = service
    yield service
    ops_controller.LEARNING = previous


@pytest.fixture()
def audit_dir():
    """An empty audit directory for the current test."""
    _empty(AUDIT_DIR)
    yield AUDIT_DIR


@pytest.fixture()
def srv():
    import agent_server

    return agent_server
