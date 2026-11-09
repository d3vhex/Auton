"""Unit tests for the MCP tool server (imported directly, no MCP transport)."""
from __future__ import annotations

import json
import re
import shutil
import time

import pytest

from conftest import WORKSPACE


def _err(out: str) -> dict:
    """Assert the tool returned its structured failure form."""
    data = json.loads(out)
    assert data.get("ok") is False, out
    return data


# --------------------------------------------------------------------------- #
# Workspace / read / write
# --------------------------------------------------------------------------- #

def test_write_read_roundtrip(ws, srv):
    out = srv.write_file("pkg/a.txt", "one\ntwo\nthree\n")
    assert "Wrote" in out
    read = srv.read_file("pkg/a.txt")
    assert "lines 1-3 of 3" in read
    assert "     1| one" in read
    assert "     3| three" in read


def test_read_range_and_cap(ws, srv):
    srv.write_file("big.txt", "\n".join(f"line{i}" for i in range(1, 101)))
    out = srv.read_file("big.txt", start_line=10, end_line=20)
    assert "lines 10-20 of 100" in out
    assert "    10| line10" in out
    assert "    21| line21" not in out

    capped = srv.read_file("big.txt", max_chars=200)
    assert "resume with start_line=" in capped


def test_read_missing_and_binary(ws, srv):
    _err(srv.read_file("nope.txt"))
    (WORKSPACE / "bin.dat").write_bytes(b"ab\x00cd")
    _err(srv.read_file("bin.dat"))


def test_path_escape_is_refused(ws, srv):
    _err(srv.read_file("../escape.txt"))
    _err(srv.read_file("/etc/passwd"))
    _err(srv.write_file("../escape.txt", "x"))
    _err(srv.list_directory(".."))
    assert not (WORKSPACE.parent / "escape.txt").exists()


def test_list_directory_depth_and_ignore(ws, srv):
    srv.write_file("top.txt", "x")
    srv.write_file("d/nested/deep.txt", "x")
    srv.write_file(".venv/lib.py", "x")
    out = srv.list_directory(".", depth=2)
    assert "file  top.txt" in out
    assert "dir   d/" in out
    assert "nested/" in out
    assert ".venv" not in out


# --------------------------------------------------------------------------- #
# Editing
# --------------------------------------------------------------------------- #

def test_edit_requires_unique_match(ws, srv):
    srv.write_file("e.py", "x = 1\ny = 1\n")
    err = _err(srv.edit_file("e.py", "= 1", "= 2"))
    assert "2 times" in err["error"]
    assert "replace_all" in err["hint"]
    assert srv.read_file("e.py").count("= 1") == 2  # untouched


def test_edit_applies_and_returns_diff(ws, srv):
    srv.write_file("e.py", "x = 1\ny = 1\n")
    out = srv.edit_file("e.py", "x = 1", "x = 9")
    assert "Replaced 1 occurrence" in out
    assert "-x = 1" in out and "+x = 9" in out
    assert "x = 9" in srv.read_file("e.py")


def test_edit_file_quote_style_fallback(ws, srv):
    srv.write_file("v.py", 'VERSION = "0.1.0"\n')
    out = srv.edit_file("v.py", "VERSION = '0.1.0'", "VERSION = '0.2.0'")
    assert "quote-normalised" in out
    assert "0.2.0" in srv.read_file("v.py")


def test_edit_file_quote_fallback_still_requires_unique(ws, srv):
    srv.write_file("v.py", 'A = "1"\nA = "1"\n')
    err = _err(srv.edit_file("v.py", "A = '1'", "A = '2'"))
    assert "2 times" in err["error"]
    assert "quote style" in err["error"]


def test_edit_replace_all(ws, srv):
    srv.write_file("e.py", "a = 1\nb = 1\n")
    out = srv.edit_file("e.py", "= 1", "= 2", replace_all=True)
    assert "Replaced 2 occurrence" in out
    assert srv.read_file("e.py").count("= 2") == 2


def test_edit_missing_anchor_has_hint(ws, srv):
    srv.write_file("e.py", "def foo():\n    return 1\n")
    err = _err(srv.edit_file("e.py", "def foo():\n    return 2", "x"))
    assert "not found" in err["error"]
    assert "line" in err["hint"]


def test_edit_empty_anchor(ws, srv):
    srv.write_file("e.py", "x\n")
    _err(srv.edit_file("e.py", "", "y"))


def test_apply_patch_multiple_blocks(ws, srv):
    srv.write_file("p.py", "a = 1\nb = 2\nc = 3\n")
    patch = (
        "<<<<<<< SEARCH\n"
        "a = 1\n"
        "=======\n"
        "a = 10\n"
        ">>>>>>> REPLACE\n"
        "<<<<<<< SEARCH\n"
        "c = 3\n"
        "=======\n"
        "c = 30\n"
        ">>>>>>> REPLACE\n"
    )
    out = srv.apply_patch("p.py", patch)
    assert "Applied 2 block(s)" in out
    content = srv.read_file("p.py")
    assert "a = 10" in content and "c = 30" in content and "b = 2" in content


def test_apply_patch_is_atomic(ws, srv):
    srv.write_file("p.py", "a = 1\n")
    patch = (
        "<<<<<<< SEARCH\n"
        "a = 1\n"
        "=======\n"
        "a = 2\n"
        ">>>>>>> REPLACE\n"
        "<<<<<<< SEARCH\n"
        "does not exist\n"
        "=======\n"
        "nope\n"
        ">>>>>>> REPLACE\n"
    )
    err = _err(srv.apply_patch("p.py", patch))
    assert "Block 2 failed" in err["error"]
    assert "a = 1" in srv.read_file("p.py")  # block 1 was rolled back


def test_apply_patch_whitespace_fallback(ws, srv):
    srv.write_file("p.py", "def f():\n    return 1\n")
    patch = (
        "<<<<<<< SEARCH\n"
        "def f():   \n"
        "    return 1\n"
        "=======\n"
        "def f():\n"
        "    return 2\n"
        ">>>>>>> REPLACE\n"
    )
    out = srv.apply_patch("p.py", patch)
    assert "whitespace-normalised" in out
    assert "return 2" in srv.read_file("p.py")


def test_apply_patch_bad_format(ws, srv):
    srv.write_file("p.py", "a\n")
    _err(srv.apply_patch("p.py", "just text"))
    _err(srv.apply_patch("missing.py", "just text"))


# --------------------------------------------------------------------------- #
# Search
# --------------------------------------------------------------------------- #

def test_grep_include_exclude_and_context(ws, srv):
    srv.write_file("src/a.py", "alpha\nneedle here\nomega\n")
    srv.write_file("src/b.txt", "needle here\n")
    out = srv.grep("needle", include="*.py")
    assert "src/a.py:2" in out
    assert "b.txt" not in out

    excluded = srv.grep("needle", exclude="*.txt")
    assert "a.py" in excluded and "b.txt" not in excluded

    with_context = srv.grep("needle", include="*.py", context=1)
    assert "alpha" in with_context and "omega" in with_context


def test_grep_case_and_regex(ws, srv):
    srv.write_file("c.txt", "Hello World\nhello world\n")
    sensitive = srv.grep("Hello", case_sensitive=True)
    assert "c.txt:1" in sensitive and "c.txt:2" not in sensitive
    assert "No matches" in srv.grep("nope", case_sensitive=True)
    assert "c.txt:1" in srv.grep("h.llo", regex=True, case_sensitive=False)
    assert "c.txt:2" in srv.grep("h.llo", regex=True, case_sensitive=True)


def test_grep_respects_gitignore(ws, srv):
    srv.write_file(".gitignore", "secret.txt\n")
    srv.write_file("secret.txt", "needle\n")
    srv.write_file("visible.txt", "needle\n")
    out = srv.grep("needle")
    assert "visible.txt" in out
    assert "secret.txt" not in out


def test_grep_invalid_regex(ws, srv):
    _err(srv.grep("[", regex=True))


def test_glob(ws, srv):
    srv.write_file("pkg/mod/a.py", "")
    srv.write_file("pkg/mod/b.txt", "")
    out = srv.glob("**/*.py")
    assert "pkg/mod/a.py" in out
    assert "b.txt" not in out
    assert "No files match" in srv.glob("**/*.rs")


# --------------------------------------------------------------------------- #
# Shell
# --------------------------------------------------------------------------- #

def test_bash_runs_and_reports_exit(ws, srv):
    out = srv.bash("echo hello")
    assert "hello" in out
    assert "[exit 0]" in out
    assert "[exit 3]" in srv.bash("exit 3")


def test_bash_cwd_is_jailed(ws, srv):
    _err(srv.bash("pwd", cwd=".."))
    _err(srv.bash("pwd", cwd="/tmp"))


def test_bash_output_is_truncated(ws, srv):
    out = srv.bash("python3 -c 'print(\"x\" * 20000)'", max_output_chars=1000)
    assert "chars elided" in out


def test_bash_timeout_points_at_background(ws, srv):
    out = srv.bash("sleep 5", timeout=1)
    assert "timeout after 1s" in out
    assert "background=true" in out


def test_background_job_lifecycle(ws, srv):
    out = srv.bash("echo started; sleep 30", background=True)
    match = re.search(r"job-\d+-[0-9a-f]+", out)
    assert match, out
    job_id = match.group(0)
    time.sleep(0.3)

    status = srv.job(job_id)
    assert "running" in status
    assert "started" in status
    assert job_id in srv.job(action="list")

    assert "Killed" in srv.job(job_id, action="kill")
    _err(srv.job("job-does-not-exist"))


def test_run_tests_custom_pass_and_fail(ws, srv):
    passed = srv.run_tests("echo all good")
    assert "[PASS]" in passed and "all good" in passed
    assert "[hint]" not in passed
    failed = srv.run_tests("exit 3")
    assert "[FAIL]" in failed and "exit=3" in failed
    # Failures must tell the model to fix the code, not the assertions.
    assert "[hint]" in failed and "not the test" in failed


def test_run_tests_detection(ws, srv):
    assert srv._detect_test_command() == ("", "")
    _err(srv.run_tests())  # nothing to detect, nothing to run

    (WORKSPACE / "tests").mkdir()
    command, kind = srv._detect_test_command()
    assert kind == "pytest" and "pytest" in command


# --------------------------------------------------------------------------- #
# Git
# --------------------------------------------------------------------------- #

@pytest.mark.skipif(not shutil.which("git"), reason="git not installed")
def test_git_status_and_diff(ws, srv):
    srv.bash("git init -q && git config user.email t@example.com && "
             "git config user.name tester")
    srv.write_file("g.txt", "one\n")
    assert "?? g.txt" in srv.git_status()
    srv.bash("git add . && git commit -qm init")
    srv.write_file("g.txt", "two\n")
    diff = srv.git_diff()
    assert "-one" in diff and "+two" in diff
    assert "(no changes)" in srv.git_diff(staged=True)


# --------------------------------------------------------------------------- #
# Housekeeping
# --------------------------------------------------------------------------- #

def test_fs_operations(ws, srv):
    assert "Created" in srv.fs("d/e", "mkdir")
    srv.write_file("d/e/f.txt", "x")
    assert "file" in srv.fs("d/e/f.txt", "stat")
    assert "Moved" in srv.fs("d/e/f.txt", "move", dest="d/g.txt")
    assert "Deleted" in srv.fs("d/g.txt", "delete")

    srv.write_file("d/e/keep.txt", "x")
    _err(srv.fs("d/e", "delete"))
    assert "Deleted" in srv.fs("d/e", "delete", recursive=True)

    _err(srv.fs(".", "delete"))
    _err(srv.fs("nope", "delete"))
    _err(srv.fs("x", "bogus"))


def test_todo_write_roundtrip(ws, srv):
    out = srv.todo_write([
        {"content": "first", "status": "completed"},
        {"content": "second", "status": "in_progress"},
    ])
    assert "[x] 1. first" in out
    assert "[~] 2. second" in out
    assert "[x] 1. first" in srv.todo_write([])

    srv.fs(".agent/todos.json", "delete")
    assert "(no todos)" in srv.todo_write([])


def test_todo_write_tolerates_strings_and_aliases(ws, srv):
    """Small models send ["a", "b"] or {"task": "..."} instead of {"content": ...}."""
    out = srv.todo_write(["read the code", {"task": "fix the bug", "status": "in_progress"}])
    assert "[ ] 1. read the code" in out
    assert "[~] 2. fix the bug" in out
    assert "read the code" in srv.todo_write([])


def test_todo_write_without_arguments_reads_the_list(ws, srv):
    """A model may call todo_write({}) — it must not fail schema validation."""
    srv.todo_write([{"content": "only item"}])
    assert "only item" in srv.todo_write()
    srv.fs(".agent/todos.json", "delete")
    assert "(no todos)" in srv.todo_write()


# --------------------------------------------------------------------------- #
# Test-file guard (HARD RULE 1)
# --------------------------------------------------------------------------- #

TEST_SRC = "from add import add\n\ndef test_add():\n    assert add(2, 3) == 5\n"


def test_new_test_file_is_allowed(ws, srv):
    out = srv.write_file("test_new.py", TEST_SRC)
    assert "Wrote" in out


def test_edit_file_refuses_to_rewrite_assertions(ws, srv):
    srv.write_file("test_add.py", TEST_SRC)
    out = _err(srv.edit_file("test_add.py", "assert add(2, 3) == 5",
                             "assert add(2, 3) == 6"))
    assert "refusing" in out["error"] and "HARD RULE 1" in out["hint"]
    assert (WORKSPACE / "test_add.py").read_text() == TEST_SRC


def test_write_file_refuses_to_drop_assertions(ws, srv):
    srv.write_file("test_add.py", TEST_SRC)
    out = _err(srv.write_file("test_add.py", "from add import add\n"))
    assert "refusing" in out["error"]
    assert "assert add(2, 3) == 5" in (WORKSPACE / "test_add.py").read_text()


def test_apply_patch_refuses_to_delete_an_assertion(ws, srv):
    srv.write_file("test_add.py", TEST_SRC)
    patch = ("<<<<<<< SEARCH\n"
             "    assert add(2, 3) == 5\n"
             "=======\n"
             ">>>>>>> REPLACE\n")
    out = _err(srv.apply_patch("test_add.py", patch))
    assert "refusing" in out["error"]
    assert "assert add(2, 3) == 5" in (WORKSPACE / "test_add.py").read_text()


def test_test_file_edits_that_keep_assertions_are_allowed(ws, srv):
    srv.write_file("test_add.py", TEST_SRC)
    out = srv.edit_file("test_add.py", "def test_add():",
                        "def test_add_works():")
    assert "Replaced" in out
    # A new assertion/test is additive, not a rewrite.
    out = srv.edit_file("test_add.py", "    assert add(2, 3) == 5\n",
                        "    assert add(2, 3) == 5\n    assert add(0, 0) == 0\n")
    assert "Replaced" in out


def test_guard_can_be_disabled(ws, srv, monkeypatch):
    srv.write_file("test_add.py", TEST_SRC)
    monkeypatch.setattr(srv, "ALLOW_TEST_EDITS", True)
    out = srv.edit_file("test_add.py", "assert add(2, 3) == 5",
                        "assert add(2, 3) == 6")
    assert "Replaced" in out
