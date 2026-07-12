"""Editing model: add/move/connect/delete via the window."""

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



# ── editing model ────────────────────────────────────────────────────────────
def test_add_nodes_and_link(win):
    win.add_node("agent")
    win.add_node("llm")
    assert len(win.graph.nodes) == 2
    # one NodeItem per node on the scene
    items = [i for i in win.scene.items() if isinstance(i, NodeItem)]
    assert len(items) == 2

    agent_id = next(i for i, n in win.graph.nodes.items() if n.kind == "agent")
    llm_id = next(i for i, n in win.graph.nodes.items() if n.kind == "llm")
    err = win.graph.add_edge(llm_id, agent_id)
    assert err is None
    win.scene.rebuild()
    assert any(isinstance(i, EdgeItem) for i in win.scene.items())


def test_move_writes_back_position(win):
    win.add_node("agent")
    nid = next(iter(win.graph.nodes))
    item = win.scene.node_items[nid]
    item.setPos(321, 234)
    assert (win.graph.nodes[nid].x, win.graph.nodes[nid].y) == (321, 234)


def test_delete_node_and_edge(win):
    win.add_node("agent")
    win.add_node("tool")
    agent_id = next(i for i, n in win.graph.nodes.items() if n.kind == "agent")
    tool_id = next(i for i, n in win.graph.nodes.items() if n.kind == "tool")
    win.graph.add_edge(tool_id, agent_id)
    win.scene.rebuild()

    edge = win.graph.edges[0]
    win.delete_edge(edge)
    assert not win.graph.edges

    win.delete_node(win.graph.nodes[tool_id])
    assert tool_id not in win.graph.nodes


def test_add_linked_resource(win):
    win.add_node("agent")
    agent = next(n for n in win.graph.nodes.values() if n.kind == "agent")
    win.add_linked(agent, "llm")
    # the new llm is linked into the agent
    llm = next(n for n in win.graph.nodes.values() if n.kind == "llm")
    assert any(e.src == llm.id and e.dst == agent.id for e in win.graph.edges)
