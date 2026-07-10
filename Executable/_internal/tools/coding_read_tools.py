"""Coding-agent READ tools — read-only inspection & search (for the explorer sub-agent).

read_file / list_dir / glob_search / grep_search. All safe (no side effects), so
none is high-risk. Split out of coding_agent_tools.py so an orchestrator can give a
read-only "explorer" sub-agent ONLY these. Conventions: `from tool_registry import
tool`; helpers are LAMBDAS (a top-level def becomes a tool); never raise (return
`[ERROR] ...`); output capped; stdlib only.
"""
from tool_registry import tool

import fnmatch
import os
import re

_MAX_CHARS = 30000
_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", ".tox",
              ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build",
              ".idea", ".vscode", ".next", "target", ".gradle"}
_BINARY_EXT = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".pdf", ".zip",
               ".gz", ".tar", ".7z", ".rar", ".exe", ".dll", ".so", ".dylib",
               ".pyc", ".class", ".o", ".a", ".bin", ".mp3", ".mp4", ".mov",
               ".avi", ".woff", ".woff2", ".ttf", ".otf", ".jar", ".wasm"}

_clip = lambda s: (s if len(s) <= _MAX_CHARS
                   else s[:_MAX_CHARS] + f"\n... [truncated {len(s) - _MAX_CHARS} more chars] ...")
_is_binary = lambda p: os.path.splitext(p)[1].lower() in _BINARY_EXT
_rel = lambda p: os.path.relpath(p).replace("\\", "/")
_skipped = lambda p: any(part in _SKIP_DIRS
                         for part in p.replace("\\", "/").split("/"))


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
