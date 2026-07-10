"""Coding-agent EDIT tools — create / edit / move / delete files (for the editor sub-agent).

write_file / edit_file / move_path / delete_path are `@tool(risk="high")` so the
generated agent HITL-gates them (the GUI shows a diff + asks permission); make_dir
is harmless. Split out of coding_agent_tools.py so an orchestrator can give a
write-capable "editor" sub-agent ONLY these. Conventions: `from tool_registry import
tool`; helpers are LAMBDAS; never raise (return `[ERROR] ...`); stdlib only.
"""
from tool_registry import tool

import os
import shutil

_rel = lambda p: os.path.relpath(p).replace("\\", "/")


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
