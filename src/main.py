"""MetaAgent — a Qt (PySide6) app for building AI agents.

Run:
    pip install -r requirements.txt
    python main.py

First run: Settings → API Key / Model, paste your LLM API key (SiliconFlow by
default), stored in config.json next to this file.

The whole app is Qt (PySide6) and runs in one process: the welcome launcher, the
visual canvas designer, and the coding-agent Tool Generator. Opening a project
or the tool generator is instant.
"""

import os
import sys

# Allow `python MetaAgent/main.py` from anywhere (and from embeddable Python,
# which doesn't auto-add the script directory to sys.path).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Optional external library folder for in-process Debug Run: a compiled (frozen)
# designer can only import libs bundled at build time, so a Debug Run of a tool-using
# agent may fail with e.g. "No module named requests". Drop a `pylibs/` folder next to
# the exe (or this file) and `pip install --target pylibs <pkg>` — no rebuild needed.
# Appended (not inserted) so bundled modules keep resolving first (zero startup cost).
_ext_libs = os.path.join(
    os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
    else os.path.dirname(os.path.abspath(__file__)),
    "pylibs")
if os.path.isdir(_ext_libs) and _ext_libs not in sys.path:
    sys.path.append(_ext_libs)

from canvas_qt.welcome import run  # noqa: E402


def main() -> None:
    run()


if __name__ == "__main__":
    main()
