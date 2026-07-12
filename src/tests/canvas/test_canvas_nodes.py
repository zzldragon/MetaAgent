"""Built-in tools, per-role silhouettes, HITL dialogs."""

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



# ── canvas: built-in tools advertised on agent-like nodes ────────────────────
from canvas_qt.dialogs import AgentDialog, builtin_tools_for   # noqa: E402


def test_builtin_tools_listed_for_agent_kinds(win):
    """An agent advertises all four built-ins; a node with none (llm) is empty."""
    win.add_node("agent"); win.add_node("llm")
    items = {i.node.kind: i for i in win.scene.node_items.values()}
    names = [t["name"] for t in builtin_tools_for(items["agent"].node)]
    assert names == ["route_to", "spawn_subagent", "write_todos", "run_python",
                     "web_search", "read_offload"]
    assert all(t.get("desc") and t.get("short") for t in
               builtin_tools_for(items["agent"].node)), "name+description+short present"
    assert builtin_tools_for(items["llm"].node) == []


def test_builtin_active_reflects_config(win):
    """The active flag tracks the node's role/flags (what it provides right now)."""
    win.add_node("agent")
    ag = next(iter(win.scene.node_items.values())).node

    def active():
        return {t["name"] for t in builtin_tools_for(ag) if t["active"]}
    assert active() == set()                             # plain single agent: none active
    ag.props["code_exec"] = True
    assert "run_python" in active()
    ag.props["enable_todos"] = True
    assert {"run_python", "write_todos"} <= active()
    ag.props["role"] = "orchestrator"
    assert "spawn_subagent" in active()
    ag.props["role"] = "planner"; ag.props["route_self"] = True
    assert "route_to" in active()


def test_agent_dialog_role_gating(win):
    """After the collapsible-group regroup: routing/plan fields are enabled ONLY
    for a planner (greyed otherwise); code-exec sub-fields + max-revises follow
    their checkboxes; and apply() still round-trips every prop."""
    win.add_node("agent")
    node = next(iter(win.scene.node_items.values())).node
    node.props["role"] = "worker"
    dlg = AgentDialog(win, node)
    assert not dlg.route_self.isEnabled() and not dlg.structured_plan.isEnabled()
    assert "color: #888" in dlg.route_self.styleSheet()    # disabled -> greyed text
    assert "Planner role only" in dlg.route_self.toolTip()
    dlg.role.setCurrentText("planner")                 # fires _refresh_enabled
    assert dlg.route_self.isEnabled() and dlg.structured_plan.isEnabled()
    assert not dlg.quick_response.isEnabled()          # route_self still off
    dlg.route_self.setChecked(True)
    assert dlg.quick_response.isEnabled()
    assert not dlg.code_exec_timeout.isEnabled() and not dlg.max_regen.isEnabled()
    dlg.code_exec.setChecked(True); dlg.groundedness_check.setChecked(True)
    assert dlg.code_exec_timeout.isEnabled() and dlg.max_regen.isEnabled()
    dlg.web_search.setChecked(True); dlg.max_regen.setText("2")
    assert dlg.apply() is None
    assert node.props["role"] == "planner" and node.props["route_self"] is True
    assert node.props["web_search"] and node.props["groundedness_check"]
    assert node.props["max_regen"] == 2


def test_config_dialog_is_resizable(win):
    """Config windows can be resized / maximized — Qt dialogs otherwise get a
    minimal title bar (no maximize button) and no resize grip, so they feel fixed."""
    from canvas_qt.dialogs import make_dialog_resizable
    win.add_node("agent")
    node = next(iter(win.scene.node_items.values())).node
    dlg = AgentDialog(win, node)
    make_dialog_resizable(dlg)
    assert dlg.isSizeGripEnabled()
    assert dlg.windowFlags() & Qt.WindowMaximizeButtonHint
    assert dlg.windowFlags() & Qt.WindowMinimizeButtonHint


def test_builtin_node_taller_and_tooltip_describes(win):
    """Agent-like nodes reserve room for the strip (taller bounding box) and the
    tooltip carries every built-in's name + full description."""
    win.add_node("agent"); win.add_node("llm")
    items = {i.node.kind: i for i in win.scene.node_items.values()}
    assert items["agent"].boundingRect().height() > items["llm"].boundingRect().height()
    tip = items["agent"]._tooltip_text()
    assert "Built-in tools" in tip
    for t in builtin_tools_for(items["agent"].node):
        assert t["name"] in tip and t["desc"][:14] in tip


# ── canvas: per-role node silhouettes ────────────────────────────────────────
def test_node_shapes_by_role_and_paint(win):
    """Each node kind maps to a role-based silhouette (shape encodes role, colour
    the exact kind); shape_path is a valid closed path for every shape, and one
    node of every kind paints without error (incl. a selected node's ring)."""
    from PySide6.QtCore import QRectF
    from PySide6.QtGui import QImage, QPainter
    from canvas_qt.designer import KIND_SHAPE, shape_path
    from graph_model import NODE_KINDS

    valid = {"rect", "diamond", "cylinder", "document", "hexagon",
             "parallelogram", "octagon", "trapezoid", "stadium"}
    # every kind has a shape from the known set; several silhouettes are in use
    assert set(KIND_SHAPE) == set(NODE_KINDS), set(KIND_SHAPE) ^ set(NODE_KINDS)
    assert set(KIND_SHAPE.values()) <= valid
    assert len(set(KIND_SHAPE.values())) >= 6, "shapes should be genuinely varied"
    # the control/branch kinds share the diamond; rag is the lone cylinder
    assert KIND_SHAPE["router"] == KIND_SHAPE["condition"] == "diamond"
    assert KIND_SHAPE["rag"] == "cylinder"
    # shape_path is a non-trivial closed path for each shape
    for s in valid:
        path = shape_path(s, QRectF(0, 0, 168, 64))
        assert not path.isEmpty() and path.elementCount() >= 3, s

    # paint one node of every kind (with one selected → ring) without raising
    for kind in NODE_KINDS:
        win.add_node(kind)
    next(iter(win.scene.node_items.values())).setSelected(True)
    img = QImage(1200, 900, QImage.Format_ARGB32)
    img.fill(0)
    pnt = QPainter(img)
    win.scene.render(pnt, QRectF(0, 0, 1200, 900), win.scene.itemsBoundingRect())
    pnt.end()
    nonblank = sum(1 for y in range(0, 900, 30) for x in range(0, 1200, 30)
                   if img.pixelColor(x, y).alpha() > 0)
    assert nonblank > 5, "canvas painted blank — a node shape failed to render"


# ── canvas: HITL tool-confirm / review dialogs ───────────────────────────────
def test_tool_confirm_dialog_edit_and_resizable(qapp):
    """The high-risk tool-confirm dialog shows the full args, edits the primary
    (longest-string) arg in place, supports deny + remember, and is resizable."""
    from canvas_qt.dialogs import ToolConfirmDialog
    d = ToolConfirmDialog(None, "run_python",
                          {"code": "print(1)\nprint(2)", "timeout": 30})
    assert d._pkey == "code"                       # longest string arg is editable
    assert d.isSizeGripEnabled()
    d.text.setPlainText("print(42)")
    d._decision = "edit"
    o = d.outcome()
    assert o["decision"] == "allow" and o["args"]["code"] == "print(42)"
    assert o["args"]["timeout"] == 30              # other args preserved
    d.remember.setChecked(True)
    d._decision = "deny"
    assert d.outcome() == {"decision": "deny", "remember": True}
    # no string arg → edit the whole JSON
    d2 = ToolConfirmDialog(None, "t", {"n": 5})
    assert d2._pkey is None
    d2.text.setPlainText('{"n": 9}')
    d2._decision = "edit"
    assert d2.outcome()["args"] == {"n": 9}


def test_review_dialog_resizable(qapp):
    from canvas_qt.dialogs import ReviewDialog
    assert ReviewDialog(None, "prompt", "content").isSizeGripEnabled()


def test_review_dialog_route_mode_has_branch_buttons(qapp):
    """In a Debug Run of a route-mode HITL, the review dialog must show one BUTTON per
    branch (not approve/edit/reject) and return the chosen branch as its decision."""
    from PySide6.QtWidgets import QPushButton
    from canvas_qt.dialogs import ReviewDialog
    dlg = ReviewDialog(None, "pick", "the draft", choices=["send", "reviser", "escalate"])
    labels = {b.text() for b in dlg.findChildren(QPushButton)}
    assert labels == {"send", "reviser", "escalate"}, labels   # branch buttons, no approve/reject
    dlg._finish("reviser")                                     # simulate clicking 'reviser'
    assert dlg.result() == {"decision": "reviser", "content": "the draft", "feedback": ""}
    # gate mode (no choices) still shows Approve/Edit + Reject
    g = ReviewDialog(None, "pick", "x")
    assert {b.text() for b in g.findChildren(QPushButton)} == {"Approve / Edit", "Reject"}


def test_hitl_links_are_flow_not_uses(win):
    """A link touching a HITL node is FLOW (blue #1565C0), never a gray 'uses'
    resource edge — a HITL is a review gate / human-driven branch, not a resource."""
    from graph_model import Graph
    a = Graph()
    n_a = a.new_node("agent", 0, 0); n_a.name = "A"
    n_h = a.new_node("hitl", 200, 0); n_h.name = "gate"
    n_b = a.new_node("agent", 400, -40); n_b.name = "B"
    n_c = a.new_node("agent", 400, 40); n_c.name = "C"
    a.add_edge(n_a.id, n_h.id)                    # agent -> HITL
    a.add_edge(n_h.id, n_b.id); a.add_edge(n_h.id, n_c.id)   # HITL -> branches (route)
    win.scene.graph = a
    win.scene.rebuild()
    edges = [it for it in win.scene.items() if isinstance(it, EdgeItem)]
    assert len(edges) == 3, len(edges)
    for e in edges:
        assert e._style()[0] == "#1565C0", (e.src.node.kind, e.dst.node.kind, e._style())
