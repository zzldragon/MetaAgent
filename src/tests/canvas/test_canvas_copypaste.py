"""Ctrl+C / Ctrl+V copy & paste of configured nodes."""

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



# ── canvas: Ctrl+C / Ctrl+V copy & paste of configured nodes ─────────────────
from PySide6.QtGui import QKeyEvent                       # noqa: E402
import canvas_qt.designer as _dz                          # noqa: E402


def test_copy_paste_duplicates_node_config(win):
    """Ctrl+C then Ctrl+V duplicates a node with its full config, a fresh id, a
    unique name and an offset position; the copy is independent (deep-copied)."""
    win.add_node("agent")
    ag = next(iter(win.scene.node_items.values()))
    ag.node.name = "Planner"
    ag.node.props["code_exec"] = True
    ag.node.props["role"] = "orchestrator"
    win.scene.clearSelection(); ag.setSelected(True)
    win.copy_selection()
    n0 = len(win.graph.nodes)
    win.paste_clipboard()
    assert len(win.graph.nodes) == n0 + 1
    pasted = max((n for n in win.graph.nodes.values() if n.id != ag.node.id),
                 key=lambda n: n.x)
    assert pasted.id != ag.node.id
    assert pasted.name == "Planner_copy"
    assert pasted.props["code_exec"] is True and pasted.props["role"] == "orchestrator"
    assert (pasted.x, pasted.y) == (ag.node.x + 28, ag.node.y + 28)
    pasted.props["code_exec"] = False                     # mutate the copy
    assert ag.node.props["code_exec"] is True, "props must be deep-copied"
    assert win.scene.node_items[pasted.id].isSelected(), "pasted node is selected"
    win.paste_clipboard()                                 # cascade + dedup
    assert "Planner_copy2" in {n.name for n in win.graph.nodes.values()}


def test_paste_preserves_internal_links(win):
    """Copying several nodes keeps the links BETWEEN them on paste."""
    win.add_node("agent"); win.add_node("llm")
    items = {i.node.kind: i for i in win.scene.node_items.values()}
    assert win.graph.add_edge(items["llm"].node.id, items["agent"].node.id) is None
    win.scene.clearSelection()
    items["agent"].setSelected(True); items["llm"].setSelected(True)
    win.copy_selection()
    e0 = len(win.graph.edges)
    win.paste_clipboard()
    assert len(win.graph.nodes) == 4
    assert len(win.graph.edges) == e0 + 1, "the llm→agent link is re-created for the copies"


def test_copy_paste_keybindings(win):
    """Ctrl+C / Ctrl+V routed through the view's key handler drive copy & paste."""
    win.add_node("tool")
    t = next(iter(win.scene.node_items.values()))
    win.scene.clearSelection(); t.setSelected(True)
    win.view.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_C, Qt.ControlModifier))
    assert _dz._NODE_CLIPBOARD and _dz._NODE_CLIPBOARD["nodes"][0]["kind"] == "tool"
    n0 = len(win.graph.nodes)
    win.view.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_V, Qt.ControlModifier))
    assert len(win.graph.nodes) == n0 + 1


def test_copy_empty_and_paste_empty_are_noops(win):
    _dz._NODE_CLIPBOARD = None
    win.add_node("tool")
    win.scene.clearSelection()
    win.copy_selection()                                  # nothing selected
    assert _dz._NODE_CLIPBOARD is None
    before = len(win.graph.nodes)
    win.paste_clipboard()                                 # empty clipboard
    assert len(win.graph.nodes) == before
