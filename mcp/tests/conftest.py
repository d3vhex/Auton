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


@pytest.fixture()
def audit_dir():
    """An empty audit directory for the current test."""
    _empty(AUDIT_DIR)
    yield AUDIT_DIR


@pytest.fixture()
def srv():
    import agent_server

    return agent_server
