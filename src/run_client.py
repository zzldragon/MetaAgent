#!/usr/bin/env python
"""Launch the MetaAgent PySide6 client.

Use this instead of `python -m client` if your Python runs in "safe path" mode
(portable/embeddable installs set sys.flags.safe_path via a pythonXX._pth file,
which stops Python from adding the current directory to sys.path — so `-m client`
fails with "No module named client"). This script puts the repo root on sys.path
explicitly, then launches the GUI.

    python run_client.py --url ws://127.0.0.1:8765 --connect
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from client.qt_app import main   # noqa: E402  (after sys.path setup)

if __name__ == "__main__":
    sys.exit(main())
