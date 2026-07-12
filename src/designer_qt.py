"""Launch the Qt (PySide6) visual agent designer.

A thin entry point over canvas_qt.designer. Graphs are plain JSON / .mta bundles,
so they stay interchangeable across versions.

    python designer_qt.py                 # blank canvas
    python designer_qt.py path/to.json    # open a saved graph / .mta bundle
"""

from __future__ import annotations

import os
import sys

# Ensure the project root is importable (canvas_qt + codegen/graph_codegen/...),
# even under an embeddable Python that doesn't auto-add the script directory.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from canvas_qt.designer import run  # noqa: E402

if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else None)
