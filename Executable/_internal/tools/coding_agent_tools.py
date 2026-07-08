"""Coding-agent toolset — a Cursor / Codex-style filesystem + search + shell kit.

Gives an agent the primitives a software engineer needs: read / write / surgically
edit files, list directories, find files by glob, grep file contents by regex, run
shell commands, and move / delete paths. Everything operates relative to the
process working directory (the "workspace") or on absolute paths.

Conventions (per MetaAgent's tool contract, see tools/data_analysis_tools.py):
  * ``from tool_registry import tool``; every top-level ``def`` becomes a tool, so
    all helpers are LAMBDAS.
  * Mutating / side-effecting tools are ``@tool(risk="high")`` so the generated
    agent HITL-gates them when human-in-the-loop is enabled.
  * Tools never raise — they return a human-readable string, and errors come back
    as ``"[ERROR] ..."`` so the agent can read the problem and recover.
  * Output is capped (``_MAX_CHARS``) so one huge file / command can't blow the
    model's context window.
  * Only the Python standard library is used, so the generated agent stays
    dependency-light and the tools run anywhere.
"""
from tool_registry import tool

import fnmatch
import os
import re
import shutil
import subprocess

# ── module constants (NOT tools — only top-level `def`s are registered) ──────
_MAX_CHARS = 30000                       # per-call output cap (protects context)
_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", ".tox",
              ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build",
              ".idea", ".vscode", ".next", "target", ".gradle"}
_BINARY_EXT = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".pdf", ".zip",
               ".gz", ".tar", ".7z", ".rar", ".exe", ".dll", ".so", ".dylib",
               ".pyc", ".class", ".o", ".a", ".bin", ".mp3", ".mp4", ".mov",
               ".avi", ".woff", ".woff2", ".ttf", ".otf", ".jar", ".wasm"}

# ── helpers — LAMBDAS ONLY (a top-level def here would become a junk tool) ────
_clip = lambda s: (s if len(s) <= _MAX_CHARS
                   else s[:_MAX_CHARS] + f"\n... [truncated {len(s) - _MAX_CHARS} more chars] ...")
_is_binary = lambda p: os.path.splitext(p)[1].lower() in _BINARY_EXT
_rel = lambda p: os.path.relpath(p).replace("\\", "/")
_skipped = lambda p: any(part in _SKIP_DIRS
                         for part in p.replace("\\", "/").split("/"))


# ── read / inspect ──────────────────────────────────────────────────────────
@tool
def read_file(path: str, start_line: int = 0, end_line: int = 0) -> str:
    """Read a UTF-8 text file and return its EXACT contents (so you can copy a
    substring verbatim into edit_file). Prefer reading a file before editing it.

    Args:
        path: file path, relative to the working directory or absolute.
        start_line: 1-based first line to return (0 = from the start).
        end_line: 1-based last line to return (0 = to the end of the file).
    """
    try:
        if not os.path.isfile(path):
            return f"[ERROR] Not a file: {path}"
        if _is_binary(path):
            return f"[ERROR] '{os.path.splitext(path)[1]}' looks binary; not reading as text."
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        total = len(lines)
        s, e = max(int(start_line), 0), int(end_line)
        if s or e:
            s0 = s - 1 if s > 0 else 0
            e0 = e if e > 0 else total
            sel = lines[s0:e0]
            header = f"# {_rel(path)}  (lines {s0 + 1}-{s0 + len(sel)} of {total})\n"
            return _clip(header + "".join(sel))
        return _clip("".join(lines))
    except Exception as ex:
        return f"[ERROR] read_file failed: {ex}"


@tool
def list_dir(path: str = ".") -> str:
    """List the files and sub-directories directly inside a directory (one level).
    Directories are shown with a trailing '/'. Use this to orient yourself in an
    unfamiliar project before searching."""
    try:
        if not os.path.isdir(path):
            return f"[ERROR] Not a directory: {path}"
        names = sorted(os.listdir(path), key=str.lower)
        rows = []
        for name in names:
            full = os.path.join(path, name)
            if os.path.isdir(full):
                rows.append(f"  {name}/")
            else:
                try:
                    size = os.path.getsize(full)
                except OSError:
                    size = 0
                rows.append(f"  {name}  ({size} B)")
        body = "\n".join(rows) if rows else "  (empty)"
        return f"{_rel(path)}/  — {len(names)} entr{'y' if len(names) == 1 else 'ies'}\n{body}"
    except Exception as ex:
        return f"[ERROR] list_dir failed: {ex}"


@tool
def glob_search(pattern: str, path: str = ".", max_results: int = 200) -> str:
    """Find files whose path matches a glob PATTERN, recursively (build/vendor
    directories like .git and node_modules are skipped).

    Args:
        pattern: a glob such as '*.py', 'src/**/*.ts', or 'test_*.py'.
        path: directory to search under (default: current directory).
        max_results: cap on how many paths to return.
    """
    try:
        import glob as _glob
        base = path or "."
        found = set(_glob.glob(os.path.join(base, "**", pattern), recursive=True))
        found |= set(_glob.glob(os.path.join(base, pattern), recursive=True))
        hits = sorted(m for m in found if os.path.isfile(m) and not _skipped(m))
        shown = hits[:max(int(max_results), 1)]
        if not shown:
            return f"No files match '{pattern}' under {base}."
        extra = f" (showing {len(shown)})" if len(hits) > len(shown) else ""
        return (f"{len(hits)} match(es) for '{pattern}'{extra}\n"
                + "\n".join("  " + _rel(m) for m in shown))
    except Exception as ex:
        return f"[ERROR] glob_search failed: {ex}"


@tool
def grep_search(pattern: str, path: str = ".", include: str = "",
                ignore_case: bool = False, max_results: int = 100) -> str:
    """Search file CONTENTS for a regular expression and return 'path:line: text'
    matches. Use this to locate a symbol, string, or definition across a project.

    Args:
        pattern: a Python regular expression (e.g. 'def \\w+_handler').
        path: directory (or a single file) to search (default: current dir).
        include: optional filename glob filter, e.g. '*.py' or '*.{ts,tsx}'.
        ignore_case: case-insensitive match when True.
        max_results: cap on the number of matching lines returned.
    """
    try:
        rx = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as ex:
        return f"[ERROR] invalid regex: {ex}"
    try:
        cap = max(int(max_results), 1)
        results, targets = [], []
        if os.path.isfile(path):
            targets = [path]
        else:
            for root, dirs, files in os.walk(path):
                dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
                for fn in files:
                    if include and not fnmatch.fnmatch(fn, include):
                        continue
                    targets.append(os.path.join(root, fn))
        for fp in targets:
            if _is_binary(fp):
                continue
            try:
                with open(fp, "r", encoding="utf-8", errors="ignore") as f:
                    for i, line in enumerate(f, 1):
                        if rx.search(line):
                            results.append(f"{_rel(fp)}:{i}: {line.rstrip()[:300]}")
                            if len(results) >= cap:
                                return f"{len(results)}+ matches (capped at {cap}):\n" + "\n".join(results)
            except (OSError, UnicodeError):
                continue
        return (f"{len(results)} match(es):\n" + "\n".join(results)) if results \
            else f"No matches for /{pattern}/ under {path}."
    except Exception as ex:
        return f"[ERROR] grep_search failed: {ex}"


# ── mutate (side-effecting → high risk so HITL can gate) ─────────────────────
@tool(risk="high")
def write_file(path: str, content: str) -> str:
    """Create a new file, or OVERWRITE an existing one, with content (missing
    parent directories are created). Use this for brand-new files; prefer
    edit_file for changing part of an existing file."""
    try:
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        existed = os.path.isfile(path)
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(content)
        verb = "Overwrote" if existed else "Created"
        return f"{verb} {_rel(path)} ({len(content)} chars, {content.count(chr(10)) + 1} lines)."
    except Exception as ex:
        return f"[ERROR] write_file failed: {ex}"


@tool(risk="high")
def edit_file(path: str, old_string: str, new_string: str,
              replace_all: bool = False) -> str:
    """Make a surgical edit by replacing an EXACT substring in a file — like
    applying a small patch. old_string must match the file byte-for-byte
    (including indentation) and be UNIQUE, unless replace_all=True.

    Tip: read_file first and copy old_string verbatim, with a few lines of
    surrounding context so it is unique. To insert, use a nearby anchor as
    old_string and include it again in new_string.

    Args:
        path: file to edit.
        old_string: exact text to find (empty is rejected).
        new_string: text to put in its place.
        replace_all: replace every occurrence instead of requiring uniqueness.
    """
    try:
        if not os.path.isfile(path):
            return f"[ERROR] Not a file: {path}"
        if old_string == "":
            return "[ERROR] old_string is empty. Use write_file to create a file."
        if old_string == new_string:
            return "[ERROR] old_string and new_string are identical; nothing to do."
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
        count = text.count(old_string)
        if count == 0:
            return ("[ERROR] old_string not found. Read the file and copy the exact "
                    "text, whitespace included.")
        if count > 1 and not replace_all:
            return (f"[ERROR] old_string matches {count} places. Add surrounding "
                    "context to make it unique, or pass replace_all=True.")
        new_text = text.replace(old_string, new_string, -1 if replace_all else 1)
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(new_text)
        return f"Edited {_rel(path)}: replaced {count if replace_all else 1} occurrence(s)."
    except Exception as ex:
        return f"[ERROR] edit_file failed: {ex}"


@tool
def make_dir(path: str) -> str:
    """Create a directory, including any missing parent directories (no error if
    it already exists)."""
    try:
        os.makedirs(path, exist_ok=True)
        return f"Directory ready: {_rel(path)}"
    except Exception as ex:
        return f"[ERROR] make_dir failed: {ex}"


@tool(risk="high")
def move_path(src: str, dst: str) -> str:
    """Move or rename a file or directory (parent directories of dst are created).
    Use this to rename files or reorganize a project."""
    try:
        if not os.path.exists(src):
            return f"[ERROR] Source does not exist: {src}"
        os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
        shutil.move(src, dst)
        return f"Moved {_rel(src)} -> {_rel(dst)}."
    except Exception as ex:
        return f"[ERROR] move_path failed: {ex}"


@tool(risk="high")
def delete_path(path: str) -> str:
    """Delete a file, or an entire directory tree. This is irreversible — confirm
    the path is correct before calling."""
    try:
        if os.path.isdir(path):
            shutil.rmtree(path)
            return f"Deleted directory {_rel(path)}."
        if os.path.isfile(path):
            os.remove(path)
            return f"Deleted file {_rel(path)}."
        return f"[ERROR] Nothing to delete at {path}."
    except Exception as ex:
        return f"[ERROR] delete_path failed: {ex}"


@tool(risk="high")
def run_shell(command: str, cwd: str = "", timeout: int = 60) -> str:
    """Run a shell command and return its combined stdout+stderr plus exit code.
    Use it to run tests, linters, formatters, builds, package installs, or git —
    i.e. to VERIFY your changes. The command runs in the platform's default shell.

    Args:
        command: the command line to execute (e.g. 'python -m pytest -q').
        cwd: directory to run in (default: current working directory).
        timeout: seconds before the command is killed (default 60).
    """
    try:
        proc = subprocess.run(command, shell=True, cwd=(cwd or None),
                              capture_output=True, text=True, timeout=int(timeout))
        out = ((proc.stdout or "") + (proc.stderr or "")).strip() or "(no output)"
        return _clip(f"$ {command}\n(exit {proc.returncode})\n{out}")
    except subprocess.TimeoutExpired:
        return f"[ERROR] command timed out after {timeout}s: {command}"
    except Exception as ex:
        return f"[ERROR] run_shell failed: {ex}"
