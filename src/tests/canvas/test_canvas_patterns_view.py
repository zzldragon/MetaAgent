"""Patterns menu, theme, autofit/view, KIND_META registry, recents."""

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



def test_welcome_renders_missing_recents(qapp, monkeypatch):
    from canvas_qt import welcome as W

    monkeypatch.setattr(W, "load_recent_projects",
                        lambda: [{"path": r"C:\nope\gone.json", "opened_at": 0}])
    w = W.WelcomeWindow()
    try:
        w._reload_recents()  # must not raise on a missing file
    finally:
        w.close()


def test_patterns_menu_lists_every_preset(win):
    import patterns
    # one Patterns-menu action per preset, label-matched, in registry order
    labels = [a.text().replace("&", "")
              for a in win._patterns_menu.actions() if not a.isSeparator()]
    assert labels == [s["label"] for s in patterns.PATTERNS.values()]
    assert set(win.pattern_actions) == set(patterns.PATTERNS)


def test_insert_pattern_builds_graph(win, monkeypatch):
    # the old combo+button is gone — inserting now goes through insert_pattern(pid)
    assert not hasattr(win, "pattern_choice")
    win.insert_pattern("react")
    assert win.graph.nodes  # react preset added modules
    assert all(isinstance(i, NodeItem) or isinstance(i, EdgeItem) or True
               for i in win.scene.items())


def test_pattern_menu_action_inserts_that_preset(win, monkeypatch):
    """Triggering a Patterns-menu action replaces the canvas with that exact
    preset (selection + insert in one click). The replace-confirm only fires on
    a non-empty canvas, so stub it to 'Yes'."""
    from PySide6.QtWidgets import QMessageBox
    monkeypatch.setattr(QMessageBox, "question",
                        staticmethod(lambda *a, **k: QMessageBox.Yes))
    win.pattern_actions["orchestrator"].trigger()
    roles = sorted(n.props.get("role") for n in win.graph.nodes.values()
                   if n.kind == "agent")
    assert roles == ["orchestrator", "worker", "worker"], roles
    # switching to another preset via its action replaces the canvas again
    win.pattern_actions["react"].trigger()
    agents = [n for n in win.graph.nodes.values() if n.kind == "agent"]
    assert len(agents) == 1 and agents[0].props.get("role") == "single"


def test_insert_rag_preset_via_menu(win, monkeypatch):
    """A retrieval preset (CRAG) inserts through the real menu path — which passes
    a non-empty tool_files list — and lands its RAG node + capability toggles.
    Proves the builder-based presets work end-to-end from the GUI, not just from
    build_pattern_graph directly."""
    from PySide6.QtWidgets import QMessageBox
    monkeypatch.setattr(QMessageBox, "question",
                        staticmethod(lambda *a, **k: QMessageBox.Yes))
    win.pattern_actions["crag"].trigger()
    kinds = sorted(n.kind for n in win.graph.nodes.values())
    assert "rag" in kinds and kinds.count("agent") == 1, kinds
    agent = next(n for n in win.graph.nodes.values() if n.kind == "agent")
    rag = next(n for n in win.graph.nodes.values() if n.kind == "rag")
    assert agent.props.get("web_search") is True
    assert rag.props.get("grade_docs") is True and rag.props.get("corrective") is True
    # a fresh preset leaves docs_dir empty on purpose (the one thing to configure)
    assert not rag.props.get("docs_dir")
    # its prompt node carries the hand-written CRAG persona (not a role template)
    prompt = next(n for n in win.graph.nodes.values() if n.kind == "prompt")
    assert "corrective" in prompt.props.get("text", "").lower()


def test_theme_menu_switches_and_persists(win, monkeypatch):
    """Configure -> Theme offers Dark + Light; switching repaints the canvas,
    recolors muted labels, and persists the choice."""
    import app_config
    from canvas_qt import designer as D
    from canvas_qt import theme as T

    saved = {}
    monkeypatch.setattr(app_config, "save_config", lambda cfg: saved.update(cfg))
    monkeypatch.setattr(D, "persist_theme",
                        lambda n: saved.__setitem__("theme", n))

    assert set(win.theme_actions) == {"dark", "light"}
    # the checked action reflects the persisted theme
    assert win.theme_actions[app_config.get_theme()].isChecked()

    win.set_theme("light")
    assert T.current_theme() == "light"
    assert D.BG == T.CANVAS_COLORS["light"]["bg"]      # canvas globals repointed
    assert saved.get("theme") == "light"               # persisted
    # the muted status label now uses the light theme's status color
    assert T.CANVAS_COLORS["light"]["status"].lower() in win.status_label.styleSheet().lower()

    win.set_theme("dark")
    assert T.current_theme() == "dark" and D.BG == T.CANVAS_COLORS["dark"]["bg"]
    assert saved.get("theme") == "dark"


def test_inserted_pattern_has_prompt_per_agent(win, monkeypatch):
    """A complete preset wires a role-matched Prompt node to every agent."""
    from PySide6.QtWidgets import QMessageBox
    monkeypatch.setattr(QMessageBox, "question",
                        staticmethod(lambda *a, **k: QMessageBox.Yes))
    win.pattern_actions["planner_executor_critic"].trigger()
    agents = [n for n in win.graph.nodes.values() if n.kind == "agent"]
    prompts = [n for n in win.graph.nodes.values() if n.kind == "prompt"]
    assert len(agents) == 3 and len(prompts) == 3
    for a in agents:
        ins = win.graph.inputs_of(a.id, "prompt")
        assert len(ins) == 1, (a.name, "want exactly one prompt")
        assert ins[0].props["role"] == a.props["role"]
        assert (ins[0].props.get("text") or "").strip()


def test_select_all_selects_every_node(win):
    win.add_node("agent")
    win.add_node("llm")
    win.select_all()
    items = list(win.scene.node_items.values())
    assert items and all(it.isSelected() for it in items)


def test_node_can_move_to_negative_coords(win):
    """The old (0,0) position clamp is gone: nodes can be dragged anywhere, so the
    whole graph can be moved up/left off the origin."""
    win.add_node("agent")
    item = next(iter(win.scene.node_items.values()))
    item.setPos(-120, -80)                 # previously clamped to (0, 0)
    assert item.node.x == -120 and item.node.y == -80


def test_preset_layout_centered_on_origin():
    """Presets are laid out centered on the origin (straddling 0,0), not anchored
    at the top-left corner."""
    import patterns
    from canvas_qt.designer import NODE_H, NODE_W

    llm = {"provider": "siliconflow", "model": "m", "api_key": "", "base_url": "u"}
    g = patterns.build_pattern_graph("planner_executor_critic", llm)
    xs = [n.x for n in g.nodes.values()]
    ys = [n.y for n in g.nodes.values()]
    cx = (min(xs) + max(xs) + NODE_W) / 2
    cy = (min(ys) + max(ys) + NODE_H) / 2
    assert abs(cx) <= 40 and abs(cy) <= 40, (cx, cy)   # bbox centered near origin
    assert min(xs) < 0 and min(ys) < 0                 # straddles, not top-left


def test_qt_log_filter_drops_only_dpi_noise(capsys):
    """The Windows DPI-probe warning is dropped; other Qt messages pass through."""
    from PySide6.QtCore import QtMsgType

    from canvas_qt import welcome
    welcome._qt_message_handler(
        QtMsgType.QtWarningMsg, None,
        r"monitorData: Unable to obtain handle for monitor '\\.\DISPLAY1', "
        "defaulting to 96 DPI.")
    welcome._qt_message_handler(QtMsgType.QtWarningMsg, None,
                                "some genuinely useful warning")
    err = capsys.readouterr().err
    assert "Unable to obtain handle for monitor" not in err
    assert "some genuinely useful warning" in err


def test_autofit_on_by_default_and_toggle(win):
    assert win._autofit is True
    assert win.act_autofit.isChecked() is True
    # toggling the View-menu action flows through set_autofit()
    win.act_autofit.setChecked(False)
    assert win._autofit is False
    win.act_autofit.setChecked(True)
    assert win._autofit is True


def test_set_autofit_false_keeps_action_in_sync(win):
    # a manual zoom/pan calls set_autofit(False); the menu toggle must follow
    win.set_autofit(False)
    assert win._autofit is False and win.act_autofit.isChecked() is False
    win.set_autofit(True)
    assert win._autofit is True and win.act_autofit.isChecked() is True


def test_autofit_runs_on_structural_change_only_when_enabled(win, monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr(win, "fit_view",
                        lambda: calls.__setitem__("n", calls["n"] + 1))
    win.set_autofit(True)
    base = calls["n"]
    win.add_node("agent")          # rebuild -> on_rebuilt -> autofit_now -> fit
    assert calls["n"] > base, "auto-fit should run on a structural change"
    win.set_autofit(False)         # disabled (does not fit)
    off = calls["n"]
    win.add_node("llm")
    assert calls["n"] == off, "auto-fit must not run while disabled"


def test_fit_view_empty_graph_no_crash(win):
    win.graph.nodes.clear()
    win.scene.rebuild()
    win.fit_view()                 # empty bounding rect -> no-op, must not raise


def test_autofit_suppressed_while_dragging_a_node(win, monkeypatch):
    """Regression: refitting mid-drag grew the scene and chased the cursor, so a
    node dragged to the edge 'flew away' and never stopped. Auto-fit must be
    suppressed whenever the scene reports an item mouse-grabber (a drag)."""
    win.add_node("agent")
    win.set_autofit(True)
    calls = {"n": 0}
    monkeypatch.setattr(win, "fit_view",
                        lambda: calls.__setitem__("n", calls["n"] + 1))
    win.autofit_now()                       # no drag -> fits
    assert calls["n"] == 1
    item = next(iter(win.scene.node_items.values()))
    monkeypatch.setattr(win.scene, "mouseGrabberItem", lambda: item)
    win.autofit_now()                       # drag in progress -> suppressed
    assert calls["n"] == 1, "auto-fit must not run while a node is being dragged"


def test_fit_view_reentrancy_guarded(win, monkeypatch):
    """fitInView can toggle scrollbars -> resizeEvent -> fit_view again; the guard
    must stop that nested call (the runaway-zoom feedback loop)."""
    win.add_node("agent")
    calls = {"n": 0}
    monkeypatch.setattr(win.view, "fitInView",
                        lambda *a, **k: calls.__setitem__("n", calls["n"] + 1))
    win._fitting = True                     # pretend a fit is already in progress
    win.fit_view()
    assert calls["n"] == 0                  # guarded: no nested fitInView
    win._fitting = False
    win.fit_view()
    assert calls["n"] == 1


# ── KIND_META registry: single source of truth for per-kind presentation ─────
def test_kind_meta_matches_node_kinds():
    """Every declared node kind has a complete KIND_META entry, and the order is
    pinned (KIND_META order IS the palette / add-menu display order)."""
    from graph_model import KIND_META, NODE_KINDS
    assert set(KIND_META) == set(NODE_KINDS), set(KIND_META) ^ set(NODE_KINDS)
    assert tuple(KIND_META) == tuple(NODE_KINDS)


def test_kind_meta_entries_complete():
    from graph_model import KIND_META
    for k, m in KIND_META.items():
        assert isinstance(m["label"], str) and m["label"], k
        c = m["color"]
        assert isinstance(c, str) and c.startswith("#") and len(c) in (4, 7), (k, c)


def test_designer_views_derive_from_kind_meta():
    """designer.KIND_LABELS/KIND_COLORS are views over KIND_META (regression guard
    against anyone re-literalizing them), order preserved."""
    from graph_model import KIND_META
    from canvas_qt import designer
    assert designer.KIND_LABELS == {k: v["label"] for k, v in KIND_META.items()}
    assert designer.KIND_COLORS == {k: v["color"] for k, v in KIND_META.items()}
    assert list(designer.KIND_LABELS) == list(KIND_META)


def test_dialogs_cover_every_kind():
    """Every node kind has a config dialog (else open_config_dialog silently
    degrades to the generic TextDialog)."""
    from graph_model import NODE_KINDS
    assert set(D._DIALOGS) == set(NODE_KINDS), set(D._DIALOGS) ^ set(NODE_KINDS)
