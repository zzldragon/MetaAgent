"""Link (edge) contracts and branch editing from edges."""

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



# ── canvas: link (edge) contracts ────────────────────────────────────────────
def _two_agents(win, s="A", d="B"):
    win.add_node("agent"); win.add_node("agent")
    items = list(win.scene.node_items.values())
    items[0].node.name, items[1].node.name = s, d
    win.graph.add_edge(items[0].node.id, items[1].node.id)
    win.scene.rebuild()
    return items[0].node, items[1].node


def test_edge_contract_dialog_roundtrip(win):
    """The agent→agent link dialog writes a structured contract to edge.props,
    and clearing all fields removes it."""
    from canvas_qt.dialogs import EdgeContractDialog
    a, b = _two_agents(win)
    edge = win.graph.edges[0]
    dlg = EdgeContractDialog(win, edge, win.graph)
    dlg._fields = [{"name": "steps", "type": "list", "description": "the plan"}]
    assert dlg.apply() is None
    assert edge.props["contract"] == [{"name": "steps", "type": "list",
                                       "description": "the plan"}]
    dlg._fields = []                                   # clearing removes the key
    dlg.apply()
    assert "contract" not in edge.props


def test_edge_condition_branch_from_edge(win):
    """Configuring a condition→X link sets that branch's predicate on the
    condition NODE (the single source of truth), not on the edge."""
    from canvas_qt.dialogs import EdgeConditionBranchDialog
    win.add_node("condition"); win.add_node("agent")
    items = list(win.scene.node_items.values())
    cond, tgt = items[0].node, items[1].node
    tgt.name = "target"
    win.graph.add_edge(cond.id, tgt.id)
    edge = win.graph.edges[0]
    dlg = EdgeConditionBranchDialog(win, edge, win.graph)
    dlg.expr.setText("score < 0.5")
    assert dlg.apply() is None
    assert cond.props["branches"] == [{"to": "target", "expr": "score < 0.5"}]
    assert "contract" not in edge.props            # branch lives on the node, not edge


def test_edge_while_branch_from_edge(win):
    """Marking a while→X link as the loop body sets the while node's `body`;
    marking the exit needs a distinct body link to exist."""
    from canvas_qt.dialogs import EdgeWhileBranchDialog
    win.add_node("while"); win.add_node("agent"); win.add_node("agent")
    items = list(win.scene.node_items.values())
    wh, body, exit_ = items[0].node, items[1].node, items[2].node
    body.name, exit_.name = "work", "done"
    win.graph.add_edge(wh.id, body.id)
    win.graph.add_edge(wh.id, exit_.id)
    body_edge = next(e for e in win.graph.edges if e.dst == body.id)
    dlg = EdgeWhileBranchDialog(win, body_edge, win.graph)
    dlg.role.setCurrentText("loop body")
    assert dlg.apply() is None
    assert wh.props["body"] == "work"
    # marking the OTHER edge as the exit keeps body on 'work'
    exit_edge = next(e for e in win.graph.edges if e.dst == exit_.id)
    dlg2 = EdgeWhileBranchDialog(win, exit_edge, win.graph)
    dlg2.role.setCurrentText("exit")
    assert dlg2.apply() is None
    assert wh.props["body"] == "work"


def test_edge_labels_and_edge_at_scene(win):
    """Configured links draw a label (contract fields / branch / loop-exit) and
    are hit-testable via edge_at_scene."""
    from canvas_qt.designer import EdgeItem
    a, b = _two_agents(win)
    win.graph.edges[0].props["contract"] = [
        {"name": "plan", "type": "list", "description": "x"}]
    win.scene.rebuild()
    ei = next(i for i in win.scene.items() if isinstance(i, EdgeItem))
    label = ei._edge_label()
    assert label and label[0].startswith("◇ plan")
    assert "Contract" in ei.toolTip() and "plan (list)" in ei.toolTip()
    mid = ei.path().pointAtPercent(0.5)
    assert win.scene.edge_at_scene(mid) is ei


def test_copy_paste_preserves_edge_contract(win):
    """Copying a group of nodes carries the contract on the internal link too
    (edges now hold props, not just src/dst)."""
    import graph_model as gm
    a, b = _two_agents(win)
    win.graph.edges[0].props["contract"] = [
        {"name": "plan", "type": "list", "description": "steps"}]
    win.scene.rebuild()
    win.scene.clearSelection()
    for it in win.scene.node_items.values():
        it.setSelected(True)
    win.copy_selection()
    win.paste_clipboard()
    copies = {n.id for n in win.graph.nodes.values() if n.name.endswith("_copy")}
    assert len(copies) == 2
    pasted_edge = next(e for e in win.graph.edges
                       if e.src in copies and e.dst in copies)
    assert gm.contract_fields(pasted_edge) == [
        {"name": "plan", "type": "list", "description": "steps"}]


def test_edge_dispatch_resource_link_has_no_contract(win, monkeypatch):
    """A (non-llm) resource→agent link has no contract; the dispatcher shows an
    info box and returns None (no dialog, no crash). NOTE: llm→agent is the
    exception — it opens the fallback-priority dialog (see the next test)."""
    from canvas_qt import dialogs as CD
    from PySide6.QtWidgets import QMessageBox
    win.add_node("agent"); win.add_node("tool")
    items = list(win.scene.node_items.values())
    agent = next(n.node for n in items if n.node.kind == "agent")
    tool = next(n.node for n in items if n.node.kind == "tool")
    win.graph.add_edge(tool.id, agent.id)
    seen = {}
    monkeypatch.setattr(QMessageBox, "information",
                        staticmethod(lambda *a, **k: seen.setdefault("info", True)))
    assert CD.open_edge_config_dialog(win, win.graph.edges[0], win.graph) is None
    assert seen.get("info")


def test_edge_llm_link_priority_dialog(win):
    """An llm→agent link opens EdgeLlmPriorityDialog; setting a lower priority
    promotes that link, and a rebuild re-packs every link to contiguous 1..N."""
    from canvas_qt.dialogs import EdgeLlmPriorityDialog
    win.add_node("agent"); win.add_node("llm"); win.add_node("llm")
    items = list(win.scene.node_items.values())
    agent = next(n.node for n in items if n.node.kind == "agent")
    llms = [n.node for n in items if n.node.kind == "llm"]
    win.graph.add_edge(llms[0].id, agent.id)
    win.graph.add_edge(llms[1].id, agent.id)
    win.scene.rebuild()                                  # renumbers: link0=#1, link1=#2
    e0, e1 = win.graph.llm_edges_of(agent.id)
    assert (e0.props["priority"], e1.props["priority"]) == (1, 2)
    # promote the 2nd link to primary via its dialog (writes slot-0.5), then rebuild
    dlg = EdgeLlmPriorityDialog(win, e1, win.graph)
    dlg.spin.setValue(1)
    assert dlg.apply() is None
    win.scene.rebuild()
    ordered = win.graph.llm_edges_of(agent.id)
    assert ordered[0] is e1 and ordered[0].props["priority"] == 1
    assert ordered[1] is e0 and ordered[1].props["priority"] == 2


def test_edge_dispatch_router_link_has_no_contract(win, monkeypatch):
    """A router→agent link carries no data contract (a router forwards text, it
    doesn't reshape it) — the dispatcher shows an info box, no dialog."""
    from canvas_qt import dialogs as CD
    from PySide6.QtWidgets import QMessageBox
    win.add_node("router"); win.add_node("agent")
    items = list(win.scene.node_items.values())
    r = next(n.node for n in items if n.node.kind == "router")
    a = next(n.node for n in items if n.node.kind == "agent")
    win.graph.add_edge(r.id, a.id)
    seen = {}
    monkeypatch.setattr(QMessageBox, "information",
                        staticmethod(lambda *a, **k: seen.setdefault("info", True)))
    assert CD.open_edge_config_dialog(win, win.graph.edges[0], win.graph) is None
    assert seen.get("info")


def test_edge_while_exit_does_not_invert_body(win):
    """Marking the current loop-body link as 'exit' is refused rather than
    silently promoting another link to body (which would invert the loop)."""
    from canvas_qt.dialogs import EdgeWhileBranchDialog
    win.add_node("while"); win.add_node("agent"); win.add_node("agent")
    items = list(win.scene.node_items.values())
    wh, work, done = items[0].node, items[1].node, items[2].node
    work.name, done.name = "work", "done"
    win.graph.add_edge(wh.id, work.id)
    win.graph.add_edge(wh.id, done.id)
    wh.props["body"] = "work"
    body_edge = next(e for e in win.graph.edges if e.dst == work.id)
    dlg = EdgeWhileBranchDialog(win, body_edge, win.graph)
    dlg.role.setCurrentText("exit")
    msg = dlg.apply()
    assert msg and "loop body" in msg                  # refused with guidance
    assert wh.props["body"] == "work"                  # unchanged — no inversion


def test_edge_branch_dialog_guards_duplicate_names(win, monkeypatch):
    """Branches are keyed by destination name; the dispatcher refuses to edit a
    branch when two successors of the same node share a name (ambiguous)."""
    from canvas_qt import dialogs as CD
    from PySide6.QtWidgets import QMessageBox
    win.add_node("condition"); win.add_node("agent"); win.add_node("agent")
    items = list(win.scene.node_items.values())
    cond = next(n.node for n in items if n.node.kind == "condition")
    a, b = [n.node for n in items if n.node.kind == "agent"]
    a.name = b.name = "dup"
    win.graph.add_edge(cond.id, a.id)
    win.graph.add_edge(cond.id, b.id)
    seen = {}
    monkeypatch.setattr(QMessageBox, "warning",
                        staticmethod(lambda *a, **k: seen.setdefault("warn", True)))
    edge = next(e for e in win.graph.edges if e.src == cond.id)
    assert CD.open_edge_config_dialog(win, edge, win.graph) is None
    assert seen.get("warn")


def test_merge_graph_preserves_edge_contract(win):
    """Merging a graph carries agent→agent link contracts (edge props), like it
    already carries node props and shared state."""
    import graph_model as gm
    incoming = gm.Graph()
    x = incoming.new_node("agent", 0, 0); x.name = "X"
    y = incoming.new_node("agent", 200, 0); y.name = "Y"
    incoming.add_edge(x.id, y.id)
    incoming.edges[0].props["contract"] = [
        {"name": "z", "type": "int", "description": "d"}]
    win._merge_graph(incoming)
    merged = [e for e in win.graph.edges
              if win.graph.nodes[e.src].kind == "agent"
              and win.graph.nodes[e.dst].kind == "agent"]
    assert merged and gm.contract_fields(merged[-1]) == [
        {"name": "z", "type": "int", "description": "d"}]


def test_contract_field_dialog_offers_custom_types(win):
    """An agent→agent contract field can be typed with a custom type / list[Type]
    from the graph's type_defs (P5), not just native scalars — and it survives."""
    from canvas_qt.dialogs import _ContractFieldDialog, EdgeContractDialog
    import graph_model as gm
    a, b = _two_agents(win)
    win.graph.type_defs = {"Finding": {"schema": {"type": "object",
                                                  "properties": {"id": {"type": "string"}}},
                                       "merge": "merge_deep"}}
    # the field dialog lists the custom type + list[Type]
    fd = _ContractFieldDialog(win, "Add", type_defs=win.graph.type_defs)
    opts = [fd.type.itemText(i) for i in range(fd.type.count())]
    assert "Finding" in opts and "list[Finding]" in opts, opts
    # a contract with a custom-typed field keeps that type (not coerced to str)
    edge = win.graph.edges[0]
    edge.props["contract"] = [{"name": "items", "type": "list[Finding]",
                               "description": "the findings"}]
    flds = {f["name"]: f["type"] for f in gm.contract_fields(edge, win.graph.type_defs)}
    assert flds["items"] == "list[Finding]", flds
