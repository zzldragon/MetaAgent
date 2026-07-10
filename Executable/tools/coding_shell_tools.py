"""Coding-agent SHELL tool — run commands to verify changes (for the tester sub-agent).

run_shell is `@tool(risk="high")` so the generated agent HITL-gates it. Split out of
coding_agent_tools.py so an orchestrator can give a "tester" sub-agent ONLY the
ability to run tests / linters / builds. Conventions: `from tool_registry import
tool`; helpers are LAMBDAS; never raise (return `[ERROR] ...`); output capped; stdlib only.
"""
from tool_registry import tool

import subprocess

_MAX_CHARS = 30000
_clip = lambda s: (s if len(s) <= _MAX_CHARS
                   else s[:_MAX_CHARS] + f"\n... [truncated {len(s) - _MAX_CHARS} more chars] ...")


@tool(risk="high")
def run_shell(command: str, cwd: str = "", timeout: int = 60) -> str:
    """Run a shell command and return its combined stdout+stderr plus exit code.
    Use it to run tests, linters, formatters, builds, package installs, or git —
    i.e. to VERIFY changes. The command runs in the platform's default shell.

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
