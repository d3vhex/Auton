#!/usr/bin/env python3
"""MCP server: filesystem, search, patch, git and shell tools for a local agent.

Tools exposed over stdio (the MCP standard transport):

  Workspace      list_directory, read_file, write_file, edit_file, apply_patch
  Search         grep, glob
  Shell          bash, job, run_tests
  Versioning     git_status, git_diff
  Housekeeping   fs, todo_write
  Kubernetes     k8s_get, k8s_describe, k8s_logs, k8s_apply, k8s_scale,
                 k8s_rollout, k8s_delete          (k8s_tools.py)
  n8n            n8n_workflows, n8n_activate, n8n_executions, n8n_webhook,
                 n8n_create_agent_pipeline         (n8n_tools.py)

Design notes
------------
* All filesystem paths are resolved through `_resolve()` and confined to
  ``AGENT_WORKSPACE`` (default ``./workspace``). ``bash`` is the one deliberately
  unjailed door: it runs a real shell with the server's permissions, using the
  workspace as its working directory.
* Successful tools return plain text (cheap on the model's context). Failures
  return compact JSON: ``{"ok": false, "error": "...", "hint": "..."}`` — a hint
  is what lets a small model recover instead of looping.
* Every read/search/shell result is truncation-aware, so a runaway build or a
  5 MB log cannot silently eat the context window.
* Optional bwrap sandbox: set ``AGENT_SANDBOX=bwrap`` (recommended for the host
  CLI, off by default inside the pod where the container is already the boundary).

Deps:  pip install -r requirements.txt
"""
from __future__ import annotations

import difflib
import fnmatch
import hashlib
import json
import os
import pathlib
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import uuid

from mcp.server.mcpserver import MCPServer

import k8s_tools
import n8n_tools

WORKSPACE = pathlib.Path(
    os.environ.get("AGENT_WORKSPACE", pathlib.Path.cwd() / "workspace")
).resolve()
WORKSPACE.mkdir(parents=True, exist_ok=True)

MAX_READ_CHARS = int(os.environ.get("AGENT_MAX_READ_CHARS", "20000"))
MAX_OUTPUT_CHARS = int(os.environ.get("AGENT_MAX_OUTPUT_CHARS", "8000"))
MAX_DIFF_CHARS = int(os.environ.get("AGENT_MAX_DIFF_CHARS", "12000"))
MAX_SEARCH_BYTES = 2_000_000  # skip files larger than this when searching
MAX_SEARCH_RESULTS = 500
SANDBOX = os.environ.get("AGENT_SANDBOX", "off").lower()
SANDBOX_NET = os.environ.get("AGENT_SANDBOX_NET", "on").lower() != "off"

# Directories we never descend into. Extra ignores come from .gitignore.
SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules", ".agent",
             ".mypy_cache", ".pytest_cache", ".ruff_cache"}

STATE_DIR = WORKSPACE / ".agent"
JOBS_DIR = STATE_DIR / "jobs"
JOBS: dict[str, dict] = {}

mcp = MCPServer("local-coding-agent")

# Kubernetes / n8n control tools live in their own modules; they read their
# settings (namespace, API URL, key file) from the environment at import time.
k8s_tools.register(mcp)
n8n_tools.register(mcp)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

class WorkspaceError(Exception):
    """Raised when a path would escape the workspace root."""


def _err(message: str, hint: str = "") -> str:
    """Compact JSON error. Small models recover much better from a hint."""
    payload = {"ok": False, "error": message}
    if hint:
        payload["hint"] = hint
    return json.dumps(payload)


# --- test-file guard ------------------------------------------------------- #
# A 7B model that sees "AssertionError: assert -1 == 5" often decides the test
# is wrong and rewrites the assertion instead of reading the implementation.
# The prompt says not to; prose is not enough, so the edit tools refuse it.
# Set AGENT_ALLOW_TEST_EDITS=1 when a task really does change tests.
ALLOW_TEST_EDITS = os.environ.get("AGENT_ALLOW_TEST_EDITS", "0").strip().lower() in (
    "1", "true", "yes", "on")

_TEST_FILE_RE = re.compile(r"^(test_.*\.py|.*_test\.py|conftest\.py)$")
_ASSERT_LINE_RE = re.compile(r"^\s*(assert\b|self\.assert)")


def _is_test_file(path: pathlib.Path) -> bool:
    return bool(_TEST_FILE_RE.match(path.name))


def _assertion_lines(text: str) -> "collections.Counter[str]":
    from collections import Counter

    return Counter(line.strip() for line in text.splitlines()
                   if _ASSERT_LINE_RE.match(line))


def _test_edit_guard(path: pathlib.Path, old: str, new: str) -> str:
    """Refuse to remove or rewrite assertions in an existing test file."""
    if ALLOW_TEST_EDITS or old == new or not _is_test_file(path):
        return ""
    removed = _assertion_lines(old) - _assertion_lines(new)
    if not removed:
        return ""
    examples = "; ".join(list(removed)[:3])
    return _err(
        f"refusing to remove/change assertion(s) in test file {path.name}; "
        "nothing was written",
        f"Removed: {examples}. HARD RULE 1: fix the implementation, not the "
        "test. Read the source file and fix the bug. If the task explicitly "
        "asks you to change tests, that requires AGENT_ALLOW_TEST_EDITS=1.",
    )


def _resolve(rel: str, *, must_exist: bool = False) -> pathlib.Path:
    """Resolve a workspace-relative path, refusing anything that escapes it."""
    raw = (rel or ".").strip() or "."
    p = (WORKSPACE / raw).resolve()
    if p != WORKSPACE and WORKSPACE not in p.parents:
        raise WorkspaceError(
            f"Path '{rel}' escapes the workspace ({WORKSPACE}). "
            "Use a path inside the workspace."
        )
    if must_exist and not p.exists():
        raise FileNotFoundError(rel)
    return p


def _rel(p: pathlib.Path) -> str:
    return p.relative_to(WORKSPACE).as_posix()


def _clip(text: str, limit: int, note: str = "chars elided") -> str:
    """Keep the head and tail of a long string, with an explicit marker."""
    if limit <= 0 or len(text) <= limit:
        return text
    head = int(limit * 0.65)
    tail = limit - head
    return f"{text[:head]}\n…[{len(text) - limit} {note}]…\n{text[-tail:]}"


def _read_text(p: pathlib.Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace")


def _binary(p: pathlib.Path) -> bool:
    try:
        return b"\x00" in p.read_bytes()[:8192]
    except OSError:
        return False


def _atomic_write(p: pathlib.Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)


def _diff(old: str, new: str, path: str) -> str:
    lines = difflib.unified_diff(
        old.splitlines(), new.splitlines(),
        fromfile=f"a/{path}", tofile=f"b/{path}", lineterm="",
    )
    return _clip("\n".join(lines), MAX_DIFF_CHARS, "diff chars elided")


def _ignore_patterns() -> list[str]:
    """Default ignores plus the workspace's .gitignore / .git/info/exclude."""
    patterns = [f"{d}/" for d in SKIP_DIRS]
    patterns += ["*.pyc", "*.pyo"]
    for name in (".gitignore", ".git/info/exclude"):
        f = WORKSPACE / name
        try:
            if f.is_file():
                patterns.extend(f.read_text(encoding="utf-8", errors="replace").splitlines())
        except OSError:
            pass
    return patterns


def _matcher():
    """A gitignore matcher, or a tiny fnmatch fallback if pathspec is absent."""
    patterns = [p for p in _ignore_patterns() if p.strip() and not p.startswith("#")]
    try:
        import pathspec  # type: ignore

        gitignore_spec = getattr(pathspec, "GitIgnoreSpec", None)
        if gitignore_spec is not None:
            return gitignore_spec.from_lines(patterns)
        return pathspec.PathSpec.from_lines("gitwildmatch", patterns)
    except Exception:
        return None


def _ignored(p: pathlib.Path, spec) -> bool:
    try:
        rel = p.relative_to(WORKSPACE).as_posix()
    except ValueError:
        return True
    if any(part in SKIP_DIRS for part in p.relative_to(WORKSPACE).parts):
        return True
    if spec is not None:
        return spec.match_file(rel + ("/" if p.is_dir() else ""))
    return any(fnmatch.fnmatch(rel, pat.rstrip("/")) for pat in _ignore_patterns())


def _child_env() -> dict[str, str]:
    """A small, allowlisted environment for shell commands.

    Avoid leaking cluster secrets (tokens, kubeconfigs) into a shell that the
    model controls. HOME points at the workspace so tools write there.
    """
    keep = {"PATH", "LANG", "LC_ALL", "TERM", "TMPDIR", "SHELL", "USER", "LOGNAME"}
    env = {k: v for k, v in os.environ.items() if k in keep}
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    env["HOME"] = str(WORKSPACE)
    env["TERM"] = "dumb"
    return env


def _sandbox_argv(command: str, cwd: pathlib.Path) -> tuple[list[str], pathlib.Path]:
    """Return argv for a shell command, optionally wrapped in bubblewrap."""
    if SANDBOX == "bwrap" and shutil.which("bwrap"):
        argv = [
            "bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
            "--tmpfs", "/tmp",
            "--bind", str(WORKSPACE), str(WORKSPACE),
            "--chdir", str(cwd), "--die-with-parent",
        ]
        if not SANDBOX_NET:
            argv.append("--unshare-net")
        argv += ["--", "/bin/sh", "-c", command]
        return argv, cwd
    return ["/bin/sh", "-c", command], cwd


def _run_shell(command: str, cwd: pathlib.Path, timeout: int,
               max_chars: int = MAX_OUTPUT_CHARS) -> str:
    argv, real_cwd = _sandbox_argv(command, cwd)
    try:
        proc = subprocess.run(
            argv, cwd=str(real_cwd), env=_child_env(),
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        return _clip(
            f"$ {command}\n[timeout after {timeout}s]\n{out}",
            max_chars, "chars elided",
        ) + "\nHint: use bash(..., background=true) for long-running commands."
    except OSError as exc:
        return _err(f"Could not run command: {exc}", "Check the cwd and that /bin/sh exists.")
    body = (
        f"$ {command}\n[exit {proc.returncode}]\n"
        f"--- stdout ---\n{proc.stdout or ''}\n"
        f"--- stderr ---\n{proc.stderr or ''}"
    )
    return _clip(body, max_chars)


def _first_line_hint(text: str, needle: str) -> str:
    """Explain where the anchor went wrong, with the file's actual line.

    Small models retry the same near-miss (single vs double quotes, a renamed
    variable) forever unless the error shows the real text. Naming the closest
    line is what breaks that cycle.
    """
    first = next((ln for ln in needle.splitlines() if ln.strip()), "")
    if not first.strip():
        return ""
    hits = [
        i for i, line in enumerate(text.splitlines(), 1)
        if first.strip() in line
    ]
    if hits:
        return ("First line of old_string appears at line(s): "
                + ", ".join(map(str, hits[:5])) + ".")
    candidates = [ln for ln in text.splitlines() if ln.strip()]
    close = difflib.get_close_matches(first.strip(), candidates, n=1, cutoff=0.5)
    if close:
        line_no = next(
            (i for i, ln in enumerate(text.splitlines(), 1) if ln == close[0]), 0
        )
        return (f"No exact match. Closest line in the file (line {line_no}): "
                f"{close[0]!r} — copy it verbatim.")
    return ""


# --------------------------------------------------------------------------- #
# Workspace tools
# --------------------------------------------------------------------------- #

@mcp.tool()
def list_directory(path: str = ".", depth: int = 1, max_entries: int = 200) -> str:
    """List files and directories under `path` (workspace-relative).

    Recurses up to `depth` levels and skips ignored paths (.git, .venv,
    __pycache__, node_modules, .gitignore matches). Use glob() to find files by
    name instead of guessing directories.
    """
    try:
        root = _resolve(path)
    except WorkspaceError as exc:
        return _err(str(exc), "Paths must stay inside the workspace.")
    if not root.is_dir():
        return _err(f"Not a directory: {path}", "Call list_directory on its parent or use glob().")

    spec = _matcher()
    depth = max(1, min(int(depth), 5))
    # Models frequently send 0 meaning "no limit"; treating it as "at most one
    # entry" made the call look broken (and invited retry loops).
    max_entries = int(max_entries)
    if max_entries <= 0:
        max_entries = 200
    max_entries = min(max_entries, 1000)
    lines: list[str] = []
    truncated = False

    def walk(d: pathlib.Path, level: int) -> None:
        nonlocal truncated
        try:
            entries = sorted(d.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
        except OSError:
            return
        for entry in entries:
            if len(lines) >= max_entries:
                truncated = True
                return
            if _ignored(entry, spec):
                continue
            indent = "  " * (level - 1)
            if entry.is_dir():
                lines.append(f"{indent}dir   {entry.name}/")
                if level < depth:
                    walk(entry, level + 1)
            else:
                try:
                    size = entry.stat().st_size
                except OSError:
                    size = 0
                lines.append(f"{indent}file  {entry.name}  ({size} bytes)")

    walk(root, 1)
    if not lines:
        return f"(empty directory: {path})"
    if truncated:
        lines.append(f"…(stopped at {max_entries} entries; narrow the path or raise max_entries)")
    return "\n".join(lines)


@mcp.tool()
def read_file(path: str, start_line: int = 1, end_line: int = 0,
              max_chars: int = MAX_READ_CHARS) -> str:
    """Read a text file with line numbers, optionally a line range.

    Output looks like `    12| code`. Prefer a range over whole-file reads for
    large files: the output is capped at `max_chars` and tells you where to
    resume. `end_line=0` means end of file.
    """
    try:
        f = _resolve(path)
    except WorkspaceError as exc:
        return _err(str(exc), "Paths must stay inside the workspace.")
    if not f.is_file():
        return _err(f"No such file: {path}", "Use glob() or list_directory() to find it.")
    if _binary(f):
        return _err(f"{path} looks binary (contains NUL bytes).", "Only text files are readable.")

    lines = _read_text(f).splitlines()
    total = len(lines)
    if total == 0:
        return f"{path} is empty (0 lines)."
    start = max(1, int(start_line))
    end = int(end_line) if int(end_line) > 0 else total
    end = min(end, total)
    if start > end:
        return _err(f"start_line={start} is past end_line={end} (file has {total} lines).")

    out: list[str] = []
    used = 0
    last = start - 1
    for i in range(start, end + 1):
        row = f"{i:6d}| {lines[i - 1]}"
        if used + len(row) + 1 > int(max_chars):
            break
        out.append(row)
        used += len(row) + 1
        last = i
    if not out:  # max_chars smaller than a single row
        out = [f"{start:6d}| {lines[start - 1]}"[: max(1, int(max_chars))]]
        last = start

    header = [f"{path} — lines {start}-{last} of {total}"]
    if last < end:
        header.append(f"(output capped; resume with start_line={last + 1})")
    footer = []
    if last < total:
        footer.append(f"[next: start_line={last + 1}, end_line={end}]")
    return "\n".join(header + out + footer)


@mcp.tool()
def write_file(path: str, content: str) -> str:
    """Create or fully overwrite a text file (workspace-relative).

    Use this for new files. For edits to an existing file prefer apply_patch()
    or edit_file() so you do not accidentally drop surrounding code.
    """
    try:
        f = _resolve(path)
    except WorkspaceError as exc:
        return _err(str(exc), "Paths must stay inside the workspace.")
    if f.is_dir():
        return _err(f"{path} is a directory.", "Choose a file path.")
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
    if f.is_file():
        old = _read_text(f)
        if old == content:
            return f"Unchanged: {path} ({len(content)} bytes, sha256 {digest})"
        guard = _test_edit_guard(f, old, content)
        if guard:
            return guard
    _atomic_write(f, content)
    return (f"Wrote {len(content)} bytes ({content.count(chr(10)) + 1} lines) "
            f"to {path}\nsha256: {digest}")


@mcp.tool()
def edit_file(path: str, old_string: str, new_string: str,
              replace_all: bool = False) -> str:
    """Replace old_string with new_string in a file, returning the diff.

    By default old_string must occur **exactly once** — this prevents silently
    editing several places. Set replace_all=true to change every occurrence.
    Fails without writing anything if the anchor is missing.
    """
    if old_string == "":
        return _err("old_string must not be empty.",
                    "Use write_file() for a new file, or apply_patch() for insertions.")
    try:
        f = _resolve(path)
    except WorkspaceError as exc:
        return _err(str(exc), "Paths must stay inside the workspace.")
    if not f.is_file():
        return _err(f"No such file: {path}", "Use glob() to find the right path.")
    text = _read_text(f)
    count = text.count(old_string)
    if count == 0:
        try:
            start, end, note = _find_span(text, old_string)
        except ValueError as exc:
            hint = (_first_line_hint(text, old_string)
                    or "Copy the anchor exactly from read_file() output.")
            return _err(f"old_string not found in {path} ({exc}); nothing changed.", hint)
        new_text = text[:start] + new_string + text[end:]
        guard = _test_edit_guard(f, text, new_text)
        if guard:
            return guard
        _atomic_write(f, new_text)
        return (f"Replaced 1 occurrence(s) in {path}{note}.\n"
                f"{_diff(text, new_text, path)}")
    if count > 1 and not replace_all:
        starts = [i for i in range(len(text)) if text.startswith(old_string, i)]
        lines = [text.count("\n", 0, pos) + 1 for pos in starts[:8]]
        return _err(
            f"old_string occurs {count} times in {path}; nothing changed.",
            f"Matches begin at line(s) {', '.join(map(str, lines))}. Add surrounding "
            "context to make it unique, or set replace_all=true.",
        )

    new_text = text.replace(old_string, new_string, -1 if replace_all else 1)
    guard = _test_edit_guard(f, text, new_text)
    if guard:
        return guard
    _atomic_write(f, new_text)
    replaced = count if replace_all else 1
    return f"Replaced {replaced} occurrence(s) in {path}.\n{_diff(text, new_text, path)}"


def _parse_patch(patch: str) -> list[tuple[str, str]]:
    """Parse Aider-style SEARCH/REPLACE blocks into (search, replace) pairs."""
    lines = patch.splitlines()
    blocks: list[tuple[str, str]] = []
    i = 0
    while i < len(lines):
        if not lines[i].lstrip().startswith("<<<<<<<"):
            i += 1
            continue
        i += 1
        search: list[str] = []
        while i < len(lines) and not lines[i].lstrip().startswith("======="):
            search.append(lines[i])
            i += 1
        if i >= len(lines):
            raise ValueError("missing '=======' separator")
        i += 1
        replace: list[str] = []
        while i < len(lines) and not lines[i].lstrip().startswith(">>>>>>>"):
            replace.append(lines[i])
            i += 1
        if i >= len(lines):
            raise ValueError("missing '>>>>>>> REPLACE' terminator")
        i += 1
        blocks.append(("\n".join(search), "\n".join(replace)))
    if not blocks:
        raise ValueError("no SEARCH/REPLACE blocks found")
    return blocks


def _span_bounds(hay: list[str], i: int, n: int) -> tuple[int, int]:
    start = sum(len(ln) + 1 for ln in hay[:i])
    end = start + sum(len(ln) + 1 for ln in hay[i:i + n]) - 1
    return start, end


def _find_span(text: str, search: str) -> tuple[int, int, str]:
    """Locate `search` in `text`. Returns (start, end, note).

    Exact match first, then two tolerant passes: trailing whitespace, then
    quote style. Small models routinely write 'single quotes' where the file
    uses "double quotes"; rejecting that is what makes them loop forever.
    """
    hits = []
    start = text.find(search)
    while start != -1:
        hits.append(start)
        start = text.find(search, start + 1)
    if len(hits) == 1:
        return hits[0], hits[0] + len(search), ""
    if len(hits) > 1:
        raise ValueError(f"matches {len(hits)} times; add more context to make it unique")

    hay = text.splitlines()
    needle = search.splitlines()
    if needle:
        for note, ignored, transform in (
            (" (whitespace-normalised)", "trailing whitespace", lambda s: s.rstrip()),
            (" (quote-normalised)", "quote style", lambda s: s.replace("'", '"').rstrip()),
        ):
            wanted = [transform(ln) for ln in needle]
            spans = [
                i for i in range(0, len(hay) - len(needle) + 1)
                if [transform(ln) for ln in hay[i:i + len(needle)]] == wanted
            ]
            if len(spans) == 1:
                s, e = _span_bounds(hay, spans[0], len(needle))
                return s, e, note
            if len(spans) > 1:
                raise ValueError(f"matches {len(spans)} times ignoring {ignored}")
    raise ValueError("not found")


@mcp.tool()
def apply_patch(path: str, patch: str) -> str:
    """Apply one or more SEARCH/REPLACE edit blocks to a file, atomically.

    Format (each block):

        <<<<<<< SEARCH
        exact existing lines
        =======
        replacement lines
        >>>>>>> REPLACE

    Blocks are applied in order; if any block does not match exactly once,
    nothing is written. Matching falls back to ignoring trailing whitespace.
    Returns the resulting diff. This is the preferred edit tool.
    """
    try:
        f = _resolve(path)
    except WorkspaceError as exc:
        return _err(str(exc), "Paths must stay inside the workspace.")
    if not f.is_file():
        return _err(f"No such file: {path}", "Use write_file() to create it first.")

    try:
        blocks = _parse_patch(patch)
    except ValueError as exc:
        return _err(
            f"Invalid patch: {exc}",
            "Use one or more blocks of <<<<<<< SEARCH / ======= / >>>>>>> REPLACE.",
        )

    text = _read_text(f)
    working = text
    notes: list[str] = []
    for n, (search, replace) in enumerate(blocks, 1):
        try:
            start, end, note = _find_span(working, search)
        except ValueError as exc:
            return _err(
                f"Block {n} failed: {exc}. Nothing was written.",
                _first_line_hint(working, search) or "Read the file again and copy exact lines.",
            )
        working = working[:start] + replace + working[end:]
        notes.append(f"block {n}: ok{note}")

    if working == text:
        return f"No changes: {path} already matches the patch."
    guard = _test_edit_guard(f, text, working)
    if guard:
        return guard
    _atomic_write(f, working)
    return (f"Applied {len(blocks)} block(s) to {path}\n"
            + "\n".join(notes) + "\n" + _diff(text, working, path))


# --------------------------------------------------------------------------- #
# Search tools
# --------------------------------------------------------------------------- #

def _rg() -> str | None:
    return shutil.which("rg")


def _default_globs() -> list[str]:
    return [f"!{d}/**" for d in sorted(SKIP_DIRS)]


def _grep_python(pattern: str, root: pathlib.Path, *, regex: bool,
                 case_sensitive: bool, include: str, exclude: str,
                 context: int, max_results: int) -> str:
    spec = _matcher()
    rx = None
    if regex:
        rx = re.compile(pattern, 0 if case_sensitive else re.IGNORECASE)
    needle = pattern if case_sensitive else pattern.lower()
    out: list[str] = []
    files = [root] if root.is_file() else sorted(root.rglob("*"))
    for fp in files:
        if len(out) >= max_results:
            break
        if not fp.is_file() or _ignored(fp, spec):
            continue
        rel = _rel(fp)
        if include and not fnmatch.fnmatch(rel, include) and not fnmatch.fnmatch(fp.name, include):
            continue
        if exclude and (fnmatch.fnmatch(rel, exclude) or fnmatch.fnmatch(fp.name, exclude)):
            continue
        try:
            if fp.stat().st_size > MAX_SEARCH_BYTES:
                continue
            rows = fp.read_text(encoding="utf-8", errors="strict").splitlines()
        except (UnicodeDecodeError, OSError):
            continue
        for i, line in enumerate(rows):
            hit = bool(rx.search(line)) if rx else ((needle in line.lower()) if not case_sensitive else (needle in line))
            if not hit:
                continue
            for c in range(max(0, i - context), min(len(rows), i + context + 1)):
                prefix = ":" if c == i else "-"
                out.append(f"{rel}{prefix}{c + 1}{prefix} {rows[c]}")
            if len(out) >= max_results:
                break
    return "\n".join(out)


@mcp.tool()
def grep(pattern: str, path: str = ".", include: str = "", exclude: str = "",
         regex: bool = False, case_sensitive: bool = True, context: int = 0,
         max_results: int = 50) -> str:
    """Search file contents for a substring or regular expression.

    Returns `path:line: text` (context lines use `path-line- text`). Uses
    ripgrep when available and respects .gitignore either way. `include` /
    `exclude` are globs (e.g. "*.py"). `regex=false` does a plain substring
    match. Scope the `path` when a repo is large.
    """
    try:
        root = _resolve(path)
    except WorkspaceError as exc:
        return _err(str(exc), "Paths must stay inside the workspace.")
    if not root.exists():
        return _err(f"No such path: {path}")
    if regex:
        try:
            re.compile(pattern)
        except re.error as exc:
            return _err(f"Invalid regex: {exc}", "Escape special characters or set regex=false.")
    max_results = max(1, min(int(max_results), MAX_SEARCH_RESULTS))
    context = max(0, min(int(context), 10))

    rg = _rg()
    if rg:
        args = [rg, "--line-number", "--no-heading", "--color", "never", "--hidden"]
        if not case_sensitive:
            args.append("-i")
        if not regex:
            args.append("-F")
        if context:
            args += ["-C", str(context)]
        for g in _default_globs():
            args += ["-g", g]
        if include:
            args += ["-g", include]
        if exclude:
            args += ["-g", f"!{exclude}"]
        args += ["-e", pattern, "--", str(root)]
        try:
            proc = subprocess.run(args, cwd=str(WORKSPACE), env=_child_env(),
                                  capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return _err(f"ripgrep failed: {exc}", "Retry with a narrower path.")
        if proc.returncode not in (0, 1):
            return _err((proc.stderr or "ripgrep error").strip()[:1000])
        rows = proc.stdout.splitlines()
    else:
        rows = _grep_python(pattern, root if root.is_dir() else root,
                            regex=regex, case_sensitive=case_sensitive,
                            include=include, exclude=exclude,
                            context=context, max_results=max_results).splitlines()

    if not rows:
        return f"No matches for {pattern!r} in {path}."
    trimmed = rows[:max_results + (max_results * context * 2)]
    text = "\n".join(trimmed)
    if len(rows) > len(trimmed):
        text += f"\n…(stopped at {max_results} matches)"
    return _clip(text, MAX_OUTPUT_CHARS)


@mcp.tool()
def glob(pattern: str, path: str = ".") -> str:
    """Find files by name/extension using a glob (e.g. "**/*.py", "src/*.ts").

    Returns workspace-relative paths. Respects .gitignore and the usual
    ignored directories. Use this before list_directory() when you know the
    file name pattern.
    """
    try:
        base = _resolve(path)
    except WorkspaceError as exc:
        return _err(str(exc), "Paths must stay inside the workspace.")
    if base.is_file():
        base = base.parent
    if not base.is_dir():
        return _err(f"No such directory: {path}")
    spec = _matcher()
    results: list[str] = []
    try:
        for fp in base.glob(pattern):
            if len(results) >= MAX_SEARCH_RESULTS:
                break
            if fp.is_file() and not _ignored(fp, spec):
                results.append(_rel(fp))
    except (ValueError, OSError) as exc:
        return _err(f"Invalid glob: {exc}", "Patterns are relative, e.g. '**/*.py'.")
    if not results:
        return f"No files match {pattern!r} under {path}."
    return "\n".join(sorted(results))


# --------------------------------------------------------------------------- #
# Shell tools
# --------------------------------------------------------------------------- #

@mcp.tool()
def bash(command: str, cwd: str = ".", timeout: int = 120,
         max_output_chars: int = MAX_OUTPUT_CHARS, background: bool = False) -> str:
    """Run a shell command in the workspace and return its output.

    This is the agent's terminal: it runs with the server's permissions and
    cwd inside the workspace. Output is truncated head+tail. Set
    background=true for servers/watchers; you then get a job_id and poll it
    with job(job_id).
    """
    try:
        workdir = _resolve(cwd)
    except WorkspaceError as exc:
        return _err(str(exc), "cwd must be inside the workspace.")
    if not workdir.is_dir():
        return _err(f"cwd is not a directory: {cwd}", "Use list_directory() to check.")
    timeout = max(1, min(int(timeout), 3600))

    if background:
        JOBS_DIR.mkdir(parents=True, exist_ok=True)
        job_id = f"job-{int(time.time())}-{uuid.uuid4().hex[:6]}"
        log_path = JOBS_DIR / f"{job_id}.log"
        log = open(log_path, "wb")
        argv, real_cwd = _sandbox_argv(command, workdir)
        try:
            proc = subprocess.Popen(
                argv, cwd=str(real_cwd), env=_child_env(), stdin=subprocess.DEVNULL,
                stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
            )
        except OSError as exc:
            log.close()
            return _err(f"Could not start background command: {exc}")
        JOBS[job_id] = {"proc": proc, "log": log_path, "handle": log,
                        "command": command, "started": time.time()}
        return (f"Started in background: {job_id}\n"
                f"$ {command}\nPoll with job(job_id=\"{job_id}\").")

    return _run_shell(command, workdir, timeout, max(1000, int(max_output_chars)))


@mcp.tool()
def job(job_id: str = "", action: str = "output", tail_chars: int = MAX_OUTPUT_CHARS) -> str:
    """Inspect or stop a background job started by bash(background=true).

    action="output" (default) returns status and the latest output;
    action="kill" terminates the process group; action="list" lists jobs.
    """
    action = (action or "output").lower()
    if action == "list":
        if not JOBS:
            return "(no background jobs)"
        rows = []
        for jid, meta in JOBS.items():
            code = meta["proc"].poll()
            state = "running" if code is None else f"exit {code}"
            rows.append(f"{jid}  {state}  {meta['command'][:80]}")
        return "\n".join(rows)

    meta = JOBS.get(job_id)
    if not meta:
        return _err(f"Unknown job_id: {job_id}", "Call job(action=\"list\") to see jobs.")

    if action == "kill":
        proc = meta["proc"]
        if proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except OSError:
                proc.kill()
        meta["handle"].close()
        return f"Killed {job_id}."

    tail_chars = max(500, int(tail_chars))
    try:
        with open(meta["log"], "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - tail_chars))
            data = fh.read().decode("utf-8", "replace")
    except OSError as exc:
        return _err(f"Could not read job log: {exc}")
    code = meta["proc"].poll()
    state = "running" if code is None else f"exit {code}"
    return _clip(f"[{job_id}] {state}\n$ {meta['command']}\n{data}", tail_chars)


@mcp.tool()
def run_tests(command: str = "", timeout: int = 300) -> str:
    """Run the project's test suite and return a compact pass/fail summary.

    Auto-detects pytest / npm / cargo / go / make when `command` is empty.
    Output is trimmed to the most relevant tail (first failure is near the end
    for pytest). Use this to verify changes instead of eyeballing them.
    """
    cmd = command.strip()
    if not cmd:
        cmd, kind = _detect_test_command()
        if not cmd:
            return _err(
                "Could not detect a test runner.",
                "Pass an explicit command, e.g. run_tests(command=\"python -m pytest -q\").",
            )
    else:
        kind = "custom"
    timeout = max(1, min(int(timeout), 3600))
    try:
        workdir = _resolve(".")
    except WorkspaceError as exc:  # pragma: no cover - workspace is always valid
        return _err(str(exc))

    argv, real_cwd = _sandbox_argv(cmd, workdir)
    try:
        proc = subprocess.run(argv, cwd=str(real_cwd), env=_child_env(),
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return _err(f"Test run timed out after {timeout}s.",
                    "Raise timeout or run the suite with bash(background=true).")
    except OSError as exc:
        return _err(f"Could not run tests: {exc}")

    out = ((proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")).strip()
    verdict = "PASS" if proc.returncode == 0 else "FAIL"
    limit = 6000 if proc.returncode != 0 else 2000
    tail = "\n".join(out.splitlines()[-80:])
    hint = ""
    if proc.returncode != 0:
        # Repeated here, at the moment it matters: a 7B model that sees red
        # output tends to "fix" the assertion instead of the code.
        hint = ("\n[hint] This is a real failure. Change the implementation "
                "(or environment), not the test: editing/skipping assertions "
                "is considered a failed task.")
    return _clip(
        f"[{verdict}] runner={kind} exit={proc.returncode}\n"
        f"$ {cmd}\n--- output tail ---\n{tail or '(no output)'}{hint}",
        limit,
    )


def _detect_test_command() -> tuple[str, str]:
    ws = WORKSPACE
    py_signals = [
        ws / "pyproject.toml", ws / "setup.cfg", ws / "pytest.ini", ws / "tox.ini",
        ws / "tests", ws / "test",
    ]
    if any(p.exists() for p in py_signals) or list(ws.glob("test_*.py")) or list(ws.glob("*_test.py")):
        return f"{shlex.quote(sys.executable)} -m pytest -q --maxfail=1", "pytest"
    if (ws / "package.json").is_file():
        return "npm test --silent", "npm"
    if (ws / "Cargo.toml").is_file():
        return "cargo test -q", "cargo"
    if (ws / "go.mod").is_file():
        return "go test ./...", "go"
    if (ws / "Makefile").is_file() and "test:" in _read_text(ws / "Makefile"):
        return "make test", "make"
    return "", ""


# --------------------------------------------------------------------------- #
# Git tools
# --------------------------------------------------------------------------- #

def _git(args: list[str], limit: int = MAX_OUTPUT_CHARS) -> str:
    if not (WORKSPACE / ".git").exists():
        return _err("Not a git repository.", "Run `git init` first if you want version control.")
    try:
        proc = subprocess.run(
            ["git", "-C", str(WORKSPACE), "--no-pager", *args],
            env=_child_env(), capture_output=True, text=True, timeout=60,
        )
    except FileNotFoundError:
        return _err("git is not installed in this image.")
    except subprocess.TimeoutExpired:
        return _err("git command timed out.")
    if proc.returncode != 0:
        return _err((proc.stderr or "git failed").strip()[:1000])
    return _clip(proc.stdout.strip(), limit)


@mcp.tool()
def git_status() -> str:
    """Show short git status (branch + changed files)."""
    out = _git(["status", "--porcelain=v1", "-b"])
    if out.startswith("{"):
        return out
    if not out.strip():
        return "(clean)"
    return out


@mcp.tool()
def git_diff(path: str = "", staged: bool = False) -> str:
    """Show the current git diff (optionally staged, optionally one path).

    The diff is the densest summary of what changed — use it to verify edits
    instead of re-reading whole files.
    """
    args = ["diff", "--no-color", "--no-ext-diff"]
    if staged:
        args.append("--staged")
    if path:
        try:
            _resolve(path)
        except WorkspaceError as exc:
            return _err(str(exc))
        args += ["--", path]
    out = _git(args, limit=MAX_DIFF_CHARS)
    return "(no changes)" if out == "" else out


# --------------------------------------------------------------------------- #
# Housekeeping
# --------------------------------------------------------------------------- #

@mcp.tool()
def fs(path: str, op: str = "stat", dest: str = "", recursive: bool = False) -> str:
    """Filesystem operations: stat | mkdir | move | delete.

    - stat: type, size, mtime
    - mkdir: create directory (and parents)
    - move: rename/move `path` to `dest` (dest is workspace-relative)
    - delete: remove a file, or a directory when recursive=true (empty dirs
      otherwise). The workspace root and .git are protected.
    """
    op = (op or "stat").lower()
    try:
        target = _resolve(path)
    except WorkspaceError as exc:
        return _err(str(exc), "Paths must stay inside the workspace.")
    if target == WORKSPACE and op in ("delete", "move"):
        return _err("Refusing to modify the workspace root.")

    if op == "stat":
        if not target.exists():
            return _err(f"No such path: {path}")
        st = target.stat()
        kind = "dir" if target.is_dir() else "file"
        mtime = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime))
        return f"{kind}  {path}\n  size: {st.st_size} bytes\n  mtime: {mtime}\n  mode: {oct(st.st_mode)[-3:]}"

    if op == "mkdir":
        target.mkdir(parents=True, exist_ok=True)
        return f"Created directory {path}."

    if op == "move":
        if not dest.strip():
            return _err("dest is required for op=move.")
        try:
            new_path = _resolve(dest)
        except WorkspaceError as exc:
            return _err(str(exc), "dest must stay inside the workspace.")
        if not target.exists():
            return _err(f"No such path: {path}")
        if new_path.exists():
            return _err(f"Destination already exists: {dest}", "Delete it or pick another name.")
        new_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(target), str(new_path))
        return f"Moved {path} -> {dest}."

    if op == "delete":
        if not target.exists() and not target.is_symlink():
            return _err(f"No such path: {path}")
        if target == WORKSPACE / ".git":
            return _err("Refusing to delete .git.")
        if target.is_dir() and not target.is_symlink():
            if recursive:
                shutil.rmtree(target)
            else:
                try:
                    target.rmdir()
                except OSError:
                    return _err(f"Directory not empty: {path}",
                                "Set recursive=true or delete the contents first.")
        else:
            target.unlink()
        return f"Deleted {path}."

    return _err(f"Unknown op: {op}", "Use one of: stat, mkdir, move, delete.")


@mcp.tool()
def todo_write(todos: list[dict] | None = None) -> str:
    """Write the task checklist (also returns it). Call with no argument (or []) to read the current one.

    Each item: {"content": "what to do", "status": "pending|in_progress|completed"}.
    Use this to keep a multi-step task on track.
    """
    path = STATE_DIR / "todos.json"
    if not todos:
        if path.is_file():
            try:
                todos = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                todos = []
        if not todos:
            return "(no todos)"
    # Small models emit a plain list of strings (or a dict with "task"/"text")
    # instead of the documented [{"content": ...}] shape. Accept both rather
    # than rejecting the call and letting the model loop on it.
    normalized: list[dict] = []
    for item in todos:
        if isinstance(item, str):
            normalized.append({"content": item, "status": "pending"})
            continue
        if not isinstance(item, dict):
            normalized.append({"content": str(item), "status": "pending"})
            continue
        item = dict(item)
        if not item.get("content"):
            item["content"] = (item.get("task") or item.get("text")
                               or item.get("title") or "")
        item["status"] = str(item.get("status", "pending")).lower()
        normalized.append(item)
    todos = normalized
    marks = {"completed": "x", "in_progress": "~", "pending": " "}
    rendered = []
    for i, item in enumerate(todos, 1):
        status = str(item.get("status", "pending")).lower()
        rendered.append(f"[{marks.get(status, ' ')}] {i}. {item.get('content', '')}")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(todos, indent=2), encoding="utf-8")
    return "\n".join(rendered)


@mcp.tool()
def run_command(command: str, timeout: int = 120) -> str:
    """DEPRECATED alias for bash(); use bash() instead."""
    return bash(command, timeout=timeout)


if __name__ == "__main__":
    mcp.run()  # stdio transport
