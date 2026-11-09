#!/usr/bin/env python3
"""Small eval harness for the coding agent.

Runs a handful of scripted tasks through the real agent loop and checks the
resulting workspace, so "better tools" can be measured instead of assumed.

Usage (needs a reachable Ollama with the model pulled):

    AGENT_WORKSPACE="$PWD/eval-workspace" python eval/run_eval.py
    python eval/run_eval.py --list
    python eval/run_eval.py --only 3 --verbose

Each task wipes the workspace, applies its setup, runs one task, then applies a
check. Exit code is 0 only when every selected task passes.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import pathlib
import shutil
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DEFAULT_WORKSPACE = ROOT / "eval-workspace"
os.environ.setdefault("AGENT_WORKSPACE", str(DEFAULT_WORKSPACE))

from agent_core import MODEL, OLLAMA_HOST, run_agent  # noqa: E402


# --------------------------------------------------------------------------- #
# Helpers used by setups/checks
# --------------------------------------------------------------------------- #

def write(ws: pathlib.Path, rel: str, text: str) -> pathlib.Path:
    path = ws / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def run(ws: pathlib.Path, command: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(command, shell=True, cwd=str(ws), capture_output=True,
                          text=True, timeout=timeout)


def exists(ws: pathlib.Path, rel: str) -> bool:
    return (ws / rel).is_file()


def contains(ws: pathlib.Path, rel: str, needle: str) -> bool:
    return exists(ws, rel) and needle in (ws / rel).read_text(encoding="utf-8")


def pytest_passes(ws: pathlib.Path, python: str = "python3") -> bool:
    return run(ws, f"{python} -m pytest -q").returncode == 0


def python_prints(ws: pathlib.Path, script: str, expected: str) -> bool:
    return expected in run(ws, f"python3 {script}").stdout


def k8s_available() -> bool:
    """Read-only preflight: is there a reachable cluster with the namespace?"""
    try:
        import k8s_tools  # imported lazily: only needed by the k8s task

        return not k8s_tools.k8s_get("pods").startswith('{"ok": false')
    except Exception:
        return False


def n8n_available() -> bool:
    """Read-only preflight: is n8n reachable with an API key configured?"""
    try:
        import n8n_tools

        return not n8n_tools.n8n_workflows("list").startswith('{"ok": false')
    except Exception:
        return False


EVAL_NS = "auton-apps"


def auton_apps_available() -> bool:
    """The autonomous-repair namespace must exist (see yamls/auton-apps.yaml)."""
    return (k8s_available()
            and run(ROOT, f"kubectl get namespace {EVAL_NS}").returncode == 0)


def _kubectl(ws: pathlib.Path, args: str) -> subprocess.CompletedProcess:
    return run(ws, f"kubectl -n {EVAL_NS} {args}")


def setup_broken_deploy(ws: pathlib.Path) -> None:
    """A Deployment whose image does not exist -> ErrImagePull/ImagePullBackOff.

    This is a case the agent must NOT try to fix by deleting things: the right
    answer is a report + human (new image/tag or registry credentials).
    """
    os.environ["AGENT_NAMESPACES"] = f"auton,{EVAL_NS}"
    _kubectl(ws, "delete deployment eval-broken --ignore-not-found")
    _kubectl(ws, "create deployment eval-broken "
                 "--image=registry.invalid/auton-eval/does-not-exist:1 --replicas=1")
    for _ in range(40):
        out = _kubectl(
            ws, "get pods -l app=eval-broken -o "
                "jsonpath='{.items[0].status.containerStatuses[0].state.waiting.reason}'")
        if "ImagePull" in out.stdout:
            return
        time.sleep(2)


def cleanup_broken_deploy(ws: pathlib.Path) -> None:
    _kubectl(ws, "delete deployment eval-broken --ignore-not-found --wait=false")


def setup_healthy_deploy(ws: pathlib.Path) -> None:
    os.environ["AGENT_NAMESPACES"] = f"auton,{EVAL_NS}"
    _kubectl(ws, "delete deployment eval-healthy --ignore-not-found")
    _kubectl(ws, "create deployment eval-healthy "
                 "--image=nginx:alpine --replicas=1")
    for _ in range(40):
        out = _kubectl(ws, "get deployment eval-healthy "
                           "-o jsonpath='{.status.conditions[?(@.type==\"Available\")].status}'")
        if out.stdout.strip() == "True":
            return
        time.sleep(2)


def cleanup_healthy_deploy(ws: pathlib.Path) -> None:
    _kubectl(ws, "delete deployment eval-healthy --ignore-not-found --wait=false")


# --------------------------------------------------------------------------- #
# Task definitions
# --------------------------------------------------------------------------- #

TEST_ADD = (
    "from add import add\n\n"
    "def test_add():\n"
    "    assert add(2, 3) == 5\n"
    "    assert add(-1, 1) == 0\n"
)

TEST_UTILS = (
    "from utils import normalize\n\n"
    "def test_normalize():\n"
    "    assert normalize('  Hi  ') == 'hi'\n"
)


def setup_failing_test(ws: pathlib.Path) -> None:
    write(ws, "add.py", "def add(a, b):\n    return a - b\n")
    write(ws, "test_add.py", TEST_ADD)


def setup_version(ws: pathlib.Path) -> None:
    write(ws, "app.py", (
        '"""Tiny app."""\n\n'
        'VERSION = "0.1.0"\n\n'
        "def banner():\n"
        '    return f"myapp {VERSION}"\n'
    ))
    write(ws, "test_app.py", "from app import banner\ndef test_banner():\n    assert 'myapp 0.2.0' in banner()\n")


def setup_two_files(ws: pathlib.Path) -> None:
    write(ws, "old_impl.py", "def old_name(x):\n    return x * 2\n")
    write(ws, "old_use.py", "from old_impl import old_name\n\nprint(old_name(21))\n")


def setup_cleanup(ws: pathlib.Path) -> None:
    write(ws, "old.py", "print('legacy')\n")
    write(ws, "keep.py", "print('keep')\n")
    write(ws, "README.md", "Run `python old.py` to start.\n")


TASKS = [
    {
        "name": "create-and-run",
        "task": ("Create hello.py that prints exactly 'hello from the agent', "
                 "then run it to verify."),
        "setup": lambda ws: None,
        "check": lambda ws: python_prints(ws, "hello.py", "hello from the agent"),
    },
    {
        "name": "fix-failing-python",
        "task": ("There is a failing test in this workspace. Find and fix the "
                 "bug in the implementation (do not edit the tests), then run "
                 "the tests to confirm they pass."),
        "setup": setup_failing_test,
        # The test file must be byte-identical: rewriting the assertions is
        # cheating, and a 7B model will happily do it.
        "check": lambda ws: (ws / "test_add.py").read_text(encoding="utf-8") == TEST_ADD
        and pytest_passes(ws),
    },
    {
        "name": "targeted-edit",
        "task": ("Bump VERSION in app.py to 0.2.0 with a targeted edit, then run "
                 "the tests."),
        "setup": setup_version,
        "check": lambda ws: contains(ws, "app.py", "0.2.0")
        and not contains(ws, "app.py", "0.1.0")
        and pytest_passes(ws),
    },
    {
        "name": "multi-file-create",
        "task": ("Create a small package: pkg/__init__.py that re-exports add "
                 "from pkg/mathutil.py, where add(a, b) returns a + b. Then verify "
                 "with python3 -c 'from pkg import add; print(add(2, 3))'."),
        "setup": lambda ws: None,
        "check": lambda ws: exists(ws, "pkg/mathutil.py")
        and run(ws, "python3 -c 'from pkg import add; print(add(2, 3))'").stdout.strip() == "5",
    },
    {
        "name": "refactor-rename",
        "task": ("Rename old_name to new_name everywhere in this workspace "
                 "(definition and imports), then run old_use.py to verify."),
        "setup": setup_two_files,
        "check": lambda ws: contains(ws, "old_impl.py", "def new_name")
        and not contains(ws, "old_impl.py", "def old_name")
        and contains(ws, "old_use.py", "new_name")
        and "42" in run(ws, "python3 old_use.py").stdout,
    },
    {
        "name": "add-cli-flag",
        "task": ("Add a --shout flag to cli.py that upper-cases the greeting. "
                 "Verify with: python3 cli.py --name bob --shout"),
        "setup": lambda ws: write(ws, "cli.py", (
            "import argparse\n\n"
            "p = argparse.ArgumentParser()\n"
            "p.add_argument('--name', default='world')\n"
            "args = p.parse_args()\n"
            "print(f'hello {args.name}')\n"
        )),
        "check": lambda ws: "HELLO BOB" in run(ws, "python3 cli.py --name bob --shout").stdout,
    },
    {
        "name": "cleanup-dead-file",
        "task": ("Delete old.py and update README.md so it no longer references "
                 "old.py. Leave keep.py alone."),
        "setup": setup_cleanup,
        "check": lambda ws: not exists(ws, "old.py")
        and exists(ws, "keep.py")
        and not contains(ws, "README.md", "old.py"),
    },
    {
        "name": "fix-and-keep-tests",
        "task": ("A test is failing. Fix the implementation in utils.py without "
                 "changing the test file, then run the test suite."),
        "setup": lambda ws: (
            write(ws, "utils.py", "def normalize(s):\n    return s\n"),
            write(ws, "test_utils.py", TEST_UTILS),
        ),
        "check": lambda ws: (ws / "test_utils.py").read_text(encoding="utf-8") == TEST_UTILS
        and pytest_passes(ws),
    },
    {
        # Read-only: skipped unless a cluster is reachable from this shell.
        "name": "k8s-inspect",
        "requires": k8s_available,
        "task": ("Use the Kubernetes tools to list the pods in your namespace, "
                 "then write their names and phases to k8s_pods.txt, one per line."),
        "setup": lambda ws: None,
        "check": lambda ws: exists(ws, "k8s_pods.txt")
        and not contains(ws, "k8s_pods.txt", '"ok": false')
        and (ws / "k8s_pods.txt").read_text(encoding="utf-8").strip() != "",
    },
    {
        # Read-only: needs N8N_API_URL/N8N_API_KEY(_FILE) to point at a live n8n.
        "name": "n8n-list",
        "requires": n8n_available,
        "task": ("Use the n8n tools to list the workflows and write their names "
                 "to n8n_workflows.txt, one per line (write '(none)' if empty)."),
        "setup": lambda ws: None,
        "check": lambda ws: exists(ws, "n8n_workflows.txt")
        and not contains(ws, "n8n_workflows.txt", '"ok": false')
        and (ws / "n8n_workflows.txt").read_text(encoding="utf-8").strip() != "",
    },
    {
        # Live cluster: a Deployment with a bad image. The correct action is a
        # report, not a delete/retry loop.
        "name": "pod-triage",
        "requires": auton_apps_available,
        "task": ("A workload in namespace auton-apps is unhealthy. Start with "
                 "k8s_snapshot(namespace='auton-apps'), then k8s_diagnose the "
                 "broken pod (do not delete anything). You MUST create the file "
                 "triage.txt with write_file before finishing; do not just answer. "
                 "It must contain: the pod name, the failure reason, and whether "
                 "a human is required — say 'human' explicitly when the fix needs "
                 "a new image/tag or config."),
        "setup": setup_broken_deploy,
        "cleanup": cleanup_broken_deploy,
        "check": lambda ws: exists(ws, "triage.txt")
        and "eval-broken" in (ws / "triage.txt").read_text(encoding="utf-8").lower()
        and any(word in (ws / "triage.txt").read_text(encoding="utf-8").lower()
                for word in ("imagepull", "image pull", "pull", "image"))
        and "human" in (ws / "triage.txt").read_text(encoding="utf-8").lower()
        and _kubectl(ws, "get deployment eval-broken -o name").returncode == 0,
    },
    {
        # Live cluster: healthy Deployment; the agent must read the snapshot
        # instead of inventing a problem.
        "name": "deploy-health",
        "requires": auton_apps_available,
        "task": ("Run k8s_snapshot(namespace='auton-apps') and copy the "
                 "DEPLOYMENTS line for eval-healthy into deploy-health.txt "
                 "(include its ready count). Do not change anything."),
        "setup": setup_healthy_deploy,
        "cleanup": cleanup_healthy_deploy,
        "check": lambda ws: exists(ws, "deploy-health.txt")
        and "eval-healthy" in (ws / "deploy-health.txt").read_text(encoding="utf-8")
        and "1/1" in (ws / "deploy-health.txt").read_text(encoding="utf-8"),
    },
]


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #

def main() -> int:
    parser = argparse.ArgumentParser(description="Run the coding-agent eval tasks")
    parser.add_argument("--list", action="store_true", help="list tasks and exit")
    parser.add_argument("--only", type=int, default=0, help="run a single task by 1-based index")
    parser.add_argument("--verbose", action="store_true", help="stream agent logs")
    parser.add_argument("--workspace", default=os.environ["AGENT_WORKSPACE"],
                        help="scratch workspace (wiped per task)")
    args = parser.parse_args()

    selected = TASKS
    if args.only:
        if not 1 <= args.only <= len(TASKS):
            parser.error(f"--only must be between 1 and {len(TASKS)}")
        selected = [TASKS[args.only - 1]]
    if args.list:
        for i, task in enumerate(TASKS, 1):
            suffix = " (requires live k8s/n8n)" if task.get("requires") else ""
            print(f"{i:2d}. {task['name']}{suffix}")
        return 0

    # Tasks that need a live cluster/n8n are skipped instead of failing, so the
    # coding tasks stay runnable on a laptop with no kubeconfig.
    skipped = []
    if any(t.get("requires") for t in selected):
        runnable = []
        for task in selected:
            if task.get("requires") and not task["requires"]():
                skipped.append(task["name"])
            else:
                runnable.append(task)
        selected = runnable

    if skipped:
        print(f"skipping (no live access): {', '.join(skipped)}", flush=True)
    if not selected:
        print("nothing to run")
        return 0

    ws = pathlib.Path(args.workspace).resolve()
    # The MCP tool server reads AGENT_WORKSPACE from its environment: keep it in
    # sync with --workspace, otherwise the checks and the agent see two different
    # directories (the agent silently works in the import-time default).
    os.environ["AGENT_WORKSPACE"] = str(ws)
    print(f"model={MODEL} ollama={OLLAMA_HOST} workspace={ws} "
          f"tasks={len(selected)}", flush=True)

    passed = 0
    started = time.time()
    for i, task in enumerate(selected, 1):
        if ws.exists():
            shutil.rmtree(ws)
        ws.mkdir(parents=True)
        task["setup"](ws)

        log = print if args.verbose else (lambda *_: None)
        t0 = time.time()
        try:
            result = asyncio.run(run_agent(task["task"], log=log))
        except Exception as exc:  # keep the run going
            result = {"final": f"ERROR: {type(exc).__name__}: {exc}",
                      "steps": [], "stop_reason": "error", "elapsed": 0}

        try:
            ok = bool(task["check"](ws))
        except Exception as exc:
            ok = False
            print(f"   check raised {type(exc).__name__}: {exc}", flush=True)

        # Live-cluster tasks must not leak demo workloads between runs.
        if task.get("cleanup"):
            try:
                task["cleanup"](ws)
            except Exception as exc:  # cleanup is best effort
                print(f"   cleanup raised {type(exc).__name__}: {exc}", flush=True)

        passed += ok
        print(f"[{'PASS' if ok else 'FAIL'}] {i}. {task['name']} "
              f"({time.time() - t0:.1f}s, {result.get('stop_reason')}, "
              f"{len(result.get('steps', []))} steps)", flush=True)
        if not ok:
            print(f"   final: {str(result.get('final', ''))[:300]}", flush=True)

    total = len(selected)
    print(f"\n{passed}/{total} passed in {time.time() - started:.1f}s")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
