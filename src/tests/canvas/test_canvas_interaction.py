"""Rubber-band multi-select, group drag, pan."""

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



# ── canvas: rubber-band multi-select + group drag ────────────────────────────
from PySide6.QtCore import QEvent, QPointF, QRectF      # noqa: E402
from PySide6.QtGui import QMouseEvent, QPainterPath      # noqa: E402
from PySide6.QtWidgets import QGraphicsView              # noqa: E402
from canvas_qt.designer import NODE_W, NODE_H            # noqa: E402


def _vp(view, sx, sy):
    """scene (sx,sy) -> a viewport QPointF (what mouse events carry)."""
    return QPointF(view.mapFromScene(QPointF(sx, sy)))


def _press(view, vp, btn=Qt.LeftButton):
    view.mousePressEvent(QMouseEvent(QEvent.MouseButtonPress, vp, btn, btn, Qt.NoModifier))


def _move(view, vp, btn=Qt.LeftButton):
    view.mouseMoveEvent(QMouseEvent(QEvent.MouseMove, vp, Qt.NoButton, btn, Qt.NoModifier))


def _release(view, vp, btn=Qt.LeftButton):
    view.mouseReleaseEvent(QMouseEvent(QEvent.MouseButtonRelease, vp, btn, Qt.NoButton, Qt.NoModifier))


def _spread3(win, qapp):
    """Three nodes at known, separated scene positions; window shown so the view
    has a real viewport for coordinate mapping; auto-fit off + fit once."""
    win.resize(1100, 850); win.show(); qapp.processEvents()
    for k in ("agent", "llm", "tool"):
        win.add_node(k)
    a, b, c = list(win.scene.node_items.values())
    a.setPos(0, 0); b.setPos(400, 0); c.setPos(0, 400)   # setPos writes back; do NOT rebuild
    win.set_autofit(False); win.fit_view(); qapp.processEvents()
    return a, b, c


def test_view_is_rubberband_mode(win):
    assert win.view.dragMode() == QGraphicsView.RubberBandDrag


def test_area_selection_selects_only_enclosed_nodes(win):
    """The mechanism the rubber band uses (scene.setSelectionArea over a rect):
    a marquee around one node selects exactly that node."""
    win.add_node("agent"); win.add_node("llm")
    a, b = list(win.scene.node_items.values())
    a.setPos(0, 0); b.setPos(600, 0)
    path = QPainterPath(); path.addRect(QRectF(-20, -20, NODE_W + 40, NODE_H + 40))
    win.scene.setSelectionArea(path, Qt.ReplaceSelection, Qt.IntersectsItemShape)
    sel = [i for i in win.scene.node_items.values() if i.isSelected()]
    assert sel == [a], [i.node.kind for i in sel]


def test_rubberband_drag_selects_covered_nodes(win, qapp):
    """A left-drag over empty space draws a marquee and selects every node it
    covers — and only those."""
    a, b, c = _spread3(win, qapp)
    v = win.view
    _press(v, _vp(v, -60, -60))
    _move(v, _vp(v, 200, 200)); _move(v, _vp(v, 420 + NODE_W, 420 + NODE_H))
    _release(v, _vp(v, 420 + NODE_W, 420 + NODE_H)); qapp.processEvents()
    assert {i for i in (a, b, c) if i.isSelected()} == {a, b, c}
    win.scene.clearSelection()
    _press(v, _vp(v, -60, -60))
    _move(v, _vp(v, NODE_W + 20, NODE_H + 20))
    _release(v, _vp(v, NODE_W + 20, NODE_H + 20)); qapp.processEvents()
    assert {i for i in (a, b, c) if i.isSelected()} == {a}


def test_group_drag_moves_all_selected(win, qapp):
    """With several nodes selected, dragging one moves the whole group, and every
    selected node's position is written back to the model (unselected stays put).

    Driven via QTest on the viewport (not the direct event-handler helpers): moving
    items needs Qt's real scene mouse-grabber + "move the whole selection" machinery,
    which only runs when events flow through the event system to the viewport."""
    from PySide6.QtTest import QTest
    a, b, c = _spread3(win, qapp)
    win.scene.clearSelection()                     # add_node auto-selects the last node
    a.setSelected(True); b.setSelected(True)       # c left unselected
    cpos = (c.node.x, c.node.y)
    vpt = win.view.viewport()

    def at(sx, sy):
        return _vp(win.view, sx, sy).toPoint()
    QTest.mousePress(vpt, Qt.LeftButton, Qt.NoModifier, at(NODE_W / 2, NODE_H / 2))
    QTest.mouseMove(vpt, at(NODE_W / 2 + 40, NODE_H / 2 + 25))
    QTest.mouseMove(vpt, at(NODE_W / 2 + 80, NODE_H / 2 + 50))
    QTest.mouseRelease(vpt, Qt.LeftButton, Qt.NoModifier, at(NODE_W / 2 + 80, NODE_H / 2 + 50))
    qapp.processEvents()
    assert abs(a.node.x - 80) <= 6 and abs(a.node.y - 50) <= 6, (a.node.x, a.node.y)
    assert abs(b.node.x - 480) <= 6 and abs(b.node.y - 50) <= 6, (b.node.x, b.node.y)
    assert (c.node.x, c.node.y) == cpos, "unselected node must not move"


def test_middle_button_pans_not_selects(win, qapp):
    """Middle-drag pans (turns auto-fit off) and never selects."""
    a, b, c = _spread3(win, qapp)
    win.scene.clearSelection()                      # add_node may leave a node selected
    win.set_autofit(True)
    v = win.view
    _press(v, QPointF(500, 400), Qt.MiddleButton)
    _move(v, QPointF(440, 360), Qt.MiddleButton)
    _release(v, QPointF(440, 360), Qt.MiddleButton); qapp.processEvents()
    assert win._autofit is False
    assert not any(i.isSelected() for i in (a, b, c))


def test_left_drag_on_port_links_not_rubberband(win, qapp):
    """Left-press on a node's output port starts a link drag, not a marquee."""
    a, b, c = _spread3(win, qapp)
    win.scene.clearSelection()                      # add_node may leave a node selected
    v = win.view
    p = a.out_port()
    _press(v, _vp(v, p.x(), p.y()))
    assert v._link_src == a.node.id and not any(i.isSelected() for i in (a, b, c))
    _release(v, _vp(v, p.x(), p.y()))
    assert v._link_src is None


def test_click_empty_clears_selection(win, qapp):
    a, b, c = _spread3(win, qapp)
    a.setSelected(True)
    v = win.view
    empty = _vp(v, 250, 250)                        # between the nodes -> empty space
    _press(v, empty); _release(v, empty); qapp.processEvents()
    assert not a.isSelected()
