"""Code generation through the window."""

from __future__ import annotations

import os
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402,F401
from PySide6.QtGui import QAction  # noqa: E402,F401
from PySide6.QtWidgets import QApplication  # noqa: E402,F401

from canvas_qt import dialogs as D  # noqa: E402,F401
from canvas_qt.designer import CanvasWindow, EdgeItem, NodeItem  # noqa: E402,F401
from graph_model import Graph  # noqa: E402,F401
# --- shared cross-section imports (from the former monolith) ---
from PySide6.QtCore import QEvent, QPointF, QRectF  # noqa: E402,F401
from PySide6.QtGui import QMouseEvent, QPainterPath, QKeyEvent  # noqa: E402,F401
from PySide6.QtWidgets import QGraphicsView  # noqa: E402,F401
from canvas_qt.dialogs import AgentDialog, builtin_tools_for  # noqa: E402,F401
from canvas_qt.designer import NODE_W, NODE_H  # noqa: E402,F401
import canvas_qt.designer as _dz  # noqa: E402,F401



# ── generation through the window ────────────────────────────────────────────
def test_generate_minimal_pipeline(win, tmp_path, monkeypatch):
    import graph_codegen

    win.add_node("agent")
    agent = next(n for n in win.graph.nodes.values() if n.kind == "agent")
    win.add_linked(agent, "llm")
    llm = next(n for n in win.graph.nodes.values() if n.kind == "llm")
    llm.props.update(provider="openai", model="gpt-4o", base_url="")

    captured = {}

    def fake_generate(graph, name, **kwargs):   # on_generate passes code_style=
        captured["name"] = name
        captured["nodes"] = len(graph.nodes)
        return str(tmp_path)

    monkeypatch.setattr(graph_codegen, "generate_from_graph", fake_generate)
    monkeypatch.setattr(graph_codegen, "analyze",
                        lambda g: {"errors": [], "warnings": []})
    # Generate Code now prompts for the name instead of reading a text field.
    monkeypatch.setattr("PySide6.QtWidgets.QInputDialog.getText",
                        staticmethod(lambda *a, **k: ("smoke_pipeline", True)))
    monkeypatch.setattr("PySide6.QtWidgets.QMessageBox.information",
                        staticmethod(lambda *a, **k: None))
    monkeypatch.setattr("PySide6.QtWidgets.QMessageBox.warning",
                        staticmethod(lambda *a, **k: None))

    win.on_generate()
    assert captured["name"] == "smoke_pipeline"
    assert captured["nodes"] == 2
    assert win._agent_name == "smoke_pipeline"   # remembered for next time


def test_generate_prompt_abort_paths(win, monkeypatch):
    """Cancel, blank-OK and whitespace-OK all abort without generating, and
    leave the remembered agent name unchanged."""
    import graph_codegen

    win.add_node("agent")
    called = {"v": False}
    monkeypatch.setattr(graph_codegen, "analyze",
                        lambda g: {"errors": [], "warnings": []})
    monkeypatch.setattr(graph_codegen, "generate_from_graph",
                        lambda *a, **k: called.update(v=True))
    win._agent_name = "keep_me"
    for ret in (("", False), ("", True), ("   ", True)):
        called["v"] = False
        monkeypatch.setattr("PySide6.QtWidgets.QInputDialog.getText",
                            staticmethod(lambda *a, _r=ret, **k: _r))
        win.on_generate()
        assert called["v"] is False, ret
        assert win._agent_name == "keep_me", ret


def test_generate_blocked_on_invalid_graph(win, monkeypatch):
    """A graph with errors warns and never reaches the name prompt."""
    import graph_codegen

    monkeypatch.setattr(graph_codegen, "analyze",
                        lambda g: {"errors": ["broken"], "warnings": []})
    prompted = {"v": False}
    monkeypatch.setattr("PySide6.QtWidgets.QInputDialog.getText",
                        staticmethod(lambda *a, **k: prompted.update(v=True) or ("x", True)))
    monkeypatch.setattr("PySide6.QtWidgets.QMessageBox.warning",
                        staticmethod(lambda *a, **k: None))
    win.on_generate()
    assert prompted["v"] is False
