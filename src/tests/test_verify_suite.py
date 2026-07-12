"""Pytest driver for the legacy `_verify_*.py` verification scripts.

Each script is a self-contained, assert-based check (offline / stubbed LLMs).
Rather than rewrite ~22 scripts, we run each as its own subprocess from the
project root and assert it exits 0. Subprocess isolation matches how the scripts
were designed to run (their own __main__, sys.path, chdir, and — for the GUI
smoke tests — a fresh wx.App per process), and turns each into a separate,
named pytest case so failures are pinpointed.

Run just these:   pytest test/test_verify_suite.py
Run one:          pytest "test/test_verify_suite.py::test_verify_script[_verify_router.py]"
"""

import glob
import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPTS = sorted(glob.glob(os.path.join(HERE, "_verify_*.py")))

# Per-script wall-clock cap. Generous: a couple of scripts generate agents,
# bind a local websocket, or launch a stdio MCP subprocess.
TIMEOUT_S = 240


@pytest.mark.parametrize("script", SCRIPTS,
                         ids=[os.path.basename(s) for s in SCRIPTS])
def test_verify_script(script):
    name = os.path.basename(script)
    try:
        r = subprocess.run([sys.executable, script], cwd=ROOT,
                           capture_output=True, text=True, timeout=TIMEOUT_S)
    except subprocess.TimeoutExpired:
        pytest.fail(f"{name} timed out after {TIMEOUT_S}s")
    if r.returncode != 0:
        pytest.fail(
            f"{name} failed (rc={r.returncode})\n"
            f"--- stdout (tail) ---\n{(r.stdout or '')[-2500:]}\n"
            f"--- stderr (tail) ---\n{(r.stderr or '')[-2500:]}")
