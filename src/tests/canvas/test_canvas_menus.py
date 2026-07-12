"""Generate menu, GUI node, trace panel and misc dialogs."""

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



# ── Generate menu + GUI node ─────────────────────────────────────────────────
def test_canvas_has_generate_menu(win):
    """Generate actions moved from palette buttons to a Generate menu."""
    titles = [a.text().replace("&", "") for a in win.menubar.actions()]
    assert any(t == "Generate" for t in titles), titles
    actions = [a.text().replace("&", "") for a in win.findChildren(QAction)]
    for want in ("Generate Code", "Run GUI Agent", "Debug Run (live overlay)",
                 "Compile (PyInstaller)", "Open Output Folder",
                 "Dump System Prompts..."):
        assert any(want in a for a in actions), (want, actions)
    # load-bearing action handles (debug toggle / run) and the GC-safe menu ref
    assert hasattr(win, "act_run") and hasattr(win, "act_debug")
    assert win.act_debug.text() == "&Debug Run (live overlay)"
    assert hasattr(win, "_gen_menu")
    # the old palette controls are gone
    assert not hasattr(win, "agent_name") and not hasattr(win, "gui_check")


def test_graph_menu_replaces_palette_buttons(win):
    """Save/Load/Merge moved from palette buttons to a Graph menu; 'Add graph
    from...' is renamed 'Merge graph from...'; the 'Fit to view' button is gone."""
    from PySide6.QtWidgets import QPushButton

    titles = [a.text().replace("&", "") for a in win.menubar.actions()]
    assert "Graph" in titles, titles
    assert hasattr(win, "_graph_menu")
    items = [a.text().replace("&", "") for a in win._graph_menu.actions()
             if not a.isSeparator()]
    assert items == ["Save...", "Load...", "Merge graph from...",
                     "Edit Shared State...", "Define Types...",
                     "Storage / Persistence..."], items

    # the palette no longer has Save/Load/Merge/Fit buttons
    btn_labels = [b.text().replace("&", "") for b in win.findChildren(QPushButton)]
    for gone in ("Fit to view", "Save...", "Load...", "Add graph from...",
                 "Merge graph from..."):
        assert gone not in btn_labels, (gone, btn_labels)
    # no stale "Add graph from..." action survives the rename
    assert all("Add graph" not in a.text() for a in win.findChildren(QAction))
    # one-shot fit still available via the View menu
    view_items = [a.text().replace("&", "") for a in win._view_menu.actions()]
    assert "Fit to view now" in view_items


def test_palette_canvas_divider_is_a_draggable_splitter(win):
    """The palette|canvas divider is a horizontal QSplitter (draggable handle),
    not a fixed-width wall; neither pane can be collapsed to nothing."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QScrollArea, QSplitter

    sp = win.splitter
    assert isinstance(sp, QSplitter)
    assert sp.orientation() == Qt.Horizontal
    assert sp.count() == 2 and not sp.childrenCollapsible()
    palette = sp.widget(0)
    assert isinstance(palette, QScrollArea)
    # resizable: the old setFixedWidth(250) is gone (min set, max unbounded)
    assert palette.minimumWidth() == 170
    assert palette.maximumWidth() > 1000
    # the handle is draggable -> pane sizes are settable
    sp.setSizes([320, 760])
    assert len(sp.sizes()) == 2 and sum(sp.sizes()) > 0


def test_generate_action_triggers_slot(win, monkeypatch):
    """Fire the 'as Single File (portable)' generate QAction (triggered.connect)."""
    import graph_codegen
    reached = {"v": False}
    monkeypatch.setattr(graph_codegen, "analyze",
                        lambda g: {"errors": [], "warnings": []})
    monkeypatch.setattr(graph_codegen, "generate_from_graph",
                        lambda *a, **k: reached.update(v=True) or "/tmp/x")
    monkeypatch.setattr("PySide6.QtWidgets.QInputDialog.getText",
                        staticmethod(lambda *a, **k: ("named", True)))
    monkeypatch.setattr("PySide6.QtWidgets.QMessageBox.information",
                        staticmethod(lambda *a, **k: None))
    monkeypatch.setattr("PySide6.QtWidgets.QMessageBox.warning",
                        staticmethod(lambda *a, **k: None))
    act = next(a for a in win.findChildren(QAction)
               if a.text().replace("&", "") == "as Single File (portable)")
    act.trigger()
    assert reached["v"] is True


def test_debug_run_toggle_lifecycle(win, monkeypatch):
    """Debug Run flips act_debug text + _debug_running; _debug_done resets them,
    and the compile-tagged result must NOT clobber the debug lifecycle."""
    import graph_codegen
    monkeypatch.setattr(graph_codegen, "generate_from_graph",
                        lambda *a, **k: "/tmp/dbg")
    monkeypatch.setattr("PySide6.QtWidgets.QInputDialog.getText",
                        staticmethod(lambda *a, **k: ("a task", True)))
    monkeypatch.setattr(win, "_debug_worker", lambda *a, **k: None)  # don't spawn
    win.on_debug_run()
    assert win._debug_running is True
    assert win.act_debug.text() == "Stop Debug Run"
    # compile result is a tagged tuple — must leave the debug lifecycle alone
    monkeypatch.setattr("PySide6.QtWidgets.QMessageBox.information",
                        staticmethod(lambda *a, **k: None))
    win._debug_done(("compile", True), "built ok")
    assert win._debug_running is True and win.act_debug.text() == "Stop Debug Run"
    # a real debug finish resets the toggle
    win._debug_done("done", None)
    assert win._debug_running is False
    assert win.act_debug.text() == "&Debug Run (live overlay)"


def test_trace_panel_describes_state_and_condition_events():
    """The timeline's one-line description covers state writes (the delta) and
    condition branches (expr → chosen target)."""
    from canvas_qt.trace_panel import _describe
    assert _describe({"kind": "state", "agent": "work",
                      "updates": {"attempts": 2, "score": 0.8}}
                     ) == "set attempts=2, score=0.8"
    assert _describe({"kind": "state", "updates": {}}) == "state updated"
    assert _describe({"kind": "condition", "agent": "gate",
                      "choice": "publish", "expr": "score >= 0.7"}
                     ) == "score >= 0.7 → publish"
    assert _describe({"kind": "condition", "choice": "rework", "expr": "else"}
                     ) == "else → rework"


def test_trace_panel_state_block_renders_live_snapshot():
    """The run-summary appends a 'Shared state' block from the overlay's live
    full snapshot; graphs with no state read unchanged."""
    from types import SimpleNamespace

    from canvas_qt.trace_panel import TracePanel
    block = TracePanel._state_block(
        SimpleNamespace(state={"attempts": 2, "score": 0.8}))
    assert "Shared state:" in block
    assert "attempts = 2" in block and "score = 0.8" in block
    # no declared state -> empty block, summary unchanged
    assert TracePanel._state_block(SimpleNamespace(state={})) == ""
    assert TracePanel._state_block(SimpleNamespace(state=None)) == ""


def test_agent_dialog_quick_response_roundtrips(qapp):
    """The AgentDialog quick_response checkbox round-trips into node.props."""
    g = Graph()
    n = g.new_node("agent", 0, 0)
    n.name = "planner"
    n.props["role"] = "planner"
    dlg = D.AgentDialog(None, n)
    assert dlg.quick_response.isChecked() is False     # default off
    dlg.quick_response.setChecked(True)
    assert dlg.apply() is None
    assert n.props["quick_response"] is True


def test_state_field_description_is_multiline(qapp):
    """The shared-state field Description is a roomy multi-line editor and
    round-trips multi-line text through result()."""
    from PySide6.QtWidgets import QPlainTextEdit
    fd = D._StateFieldDialog(None, "Add")
    assert isinstance(fd.description, QPlainTextEdit)   # not a cramped QLineEdit
    assert fd.description.minimumHeight() >= 72         # ~3 lines tall
    fd.name.setText("draft")
    fd.description.setPlainText("the current draft being refined\nacross rounds")
    r = fd.result()
    assert r is not None and r["name"] == "draft"
    assert r["description"] == "the current draft being refined\nacross rounds"
    # an edited existing field re-opens with its description preloaded
    fd2 = D._StateFieldDialog(None, "Edit", field=r)
    assert fd2.description.toPlainText() == r["description"]


# ── Estimation menu: prompt for the LLM key/URL when it's missing ─────────────
def test_estimation_prompts_for_api_key_when_missing(win, monkeypatch):
    """Estimation's LLM layer needs an API key; if none is set, _run_estimation
    must open the Settings dialog so the user can enter the key / base URL."""
    import estimation
    import canvas_qt.welcome as W
    monkeypatch.setattr(estimation, "llm_available", lambda: False)
    opened = {"n": 0}

    class _FakeSettings:
        def __init__(self, parent=None):
            opened["n"] += 1

        def exec(self):
            from PySide6.QtWidgets import QDialog
            return QDialog.Rejected            # user declines -> save() not called

    monkeypatch.setattr(W, "SettingsDialog", _FakeSettings)
    win._ensure_llm_configured()
    assert opened["n"] == 1, "should open Settings when no API key is set"


def test_estimation_no_prompt_when_key_present(win, monkeypatch):
    """With a key configured, estimation must NOT interrupt with the Settings
    dialog."""
    import estimation
    import canvas_qt.welcome as W
    monkeypatch.setattr(estimation, "llm_available", lambda: True)
    opened = {"n": 0}

    class _FakeSettings:
        def __init__(self, *a, **k):
            opened["n"] += 1

    monkeypatch.setattr(W, "SettingsDialog", _FakeSettings)
    win._ensure_llm_configured()
    assert opened["n"] == 0, "must not prompt when a key is already configured"


def test_configure_menu_has_llm_settings(win, monkeypatch):
    """The Configure menu exposes an action to change the LLM API key / model / URL,
    which opens the shared SettingsDialog and saves on accept."""
    labels = [a.text().replace("&", "") for a in win._config_menu.actions()]
    assert any("API" in t or "LLM" in t for t in labels), labels

    import canvas_qt.welcome as W
    seen = {"open": 0, "saved": 0}

    class _FakeSettings:
        def __init__(self, parent=None):
            seen["open"] += 1

        def exec(self):
            from PySide6.QtWidgets import QDialog
            return QDialog.Accepted

        def save(self):
            seen["saved"] += 1

    monkeypatch.setattr(W, "SettingsDialog", _FakeSettings)
    win.on_edit_llm_settings()
    assert seen["open"] == 1 and seen["saved"] == 1


def test_debug_run_gates_on_key_but_gui_launch_does_not(win, tmp_path):
    """Debug Run needs the designer's key, but the key is injected IN-MEMORY at run
    time — never written into the generated config.json (no leak into the build
    artifact). A MIXED config (some LLMs keyed, some not) still triggers the gate.
    Launching the GUI does NOT nag — that's the end-user's key, set in the app."""
    import json
    import types
    # MIXED: 'a' already keyed, 'b' empty -> a debug run still needs a key for 'b'
    cfg = {"llms": {"a": [{"api_key": "sk-set"}], "b": [{"api_key": ""}]}}
    p = tmp_path / "config.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    win._debug_api_key = "nvapi-xyz"                 # session-cached -> no modal prompt
    assert win._ensure_debug_key(str(tmp_path)) is True
    # config.json on disk is UNCHANGED — the key never touches the build artifact
    assert json.loads(p.read_text(encoding="utf-8")) == cfg

    # injection is in-memory: fills ONLY keyless entries, preserves explicit keys,
    # and clears the client cache so the new key takes effect
    mod = types.SimpleNamespace(
        CONFIG={"llms": {"a": [{"api_key": "sk-set"}], "b": [{"api_key": ""}]}},
        _clients={"stale": 1})
    win._inject_debug_key(mod)
    assert mod.CONFIG["llms"]["a"][0]["api_key"] == "sk-set"     # preserved
    assert mod.CONFIG["llms"]["b"][0]["api_key"] == "nvapi-xyz"  # filled
    assert mod._clients == {}

    # a fully-keyed config needs no key at all (no prompt, returns True)
    win._debug_api_key = ""
    p.write_text(json.dumps({"llms": {"a": [{"api_key": "k"}]}}), encoding="utf-8")
    assert win._ensure_debug_key(str(tmp_path)) is True
    # the GUI-launch nag is gone (end-users set their own key in the app)
    assert not hasattr(win, "_warn_empty_keys")


def test_debug_key_dialog_requires_nonempty_key(qapp, monkeypatch):
    """OK is disabled until a non-empty key is present, so accepting can't silently
    bypass the gate with a blank key."""
    from PySide6.QtWidgets import QDialogButtonBox
    import app_config
    monkeypatch.setattr(app_config, "load_config", lambda: {"api_key": ""})
    from canvas_qt.dialogs import DebugKeyDialog
    dlg = DebugKeyDialog()
    ok = dlg._bb.button(QDialogButtonBox.Ok)
    assert not ok.isEnabled()                 # empty on open
    dlg._key.setText("nvapi-1")
    assert ok.isEnabled()                     # non-empty enables OK
    dlg._key.setText("   ")
    assert not ok.isEnabled()                 # whitespace-only disables again


def test_configure_is_a_gear_in_the_menubar_corner(win):
    """Configure is presented as a right-aligned gear button (not a text menu),
    and the gear pops up the same _config_menu with LLM settings + Theme."""
    from PySide6.QtWidgets import QToolButton

    gear = win.menubar.cornerWidget(Qt.TopRightCorner)
    assert isinstance(gear, QToolButton) and gear is win._config_gear
    assert gear.text() == "⚙" and gear.toolTip()
    assert gear.menu() is win._config_menu       # clicking it opens Configure
    # Configure is no longer a top-level text menu on the bar…
    titles = [a.text().replace("&", "") for a in win.menubar.actions()]
    assert "Configure" not in titles, titles
    # …but its actions (LLM settings + Theme) are all still reachable via the gear
    labels = [a.text().replace("&", "") for a in win._config_menu.actions()]
    assert any("API" in t or "LLM" in t for t in labels), labels
    assert any(t == "Theme" for t in labels), labels


def test_canvas_renders_in_chinese(qapp, monkeypatch):
    """With language=zh, the designer's menu bar + node palette render in
    Simplified Chinese (i18n extends to the canvas, not just the welcome window)."""
    from PySide6.QtWidgets import QPushButton
    import canvas_qt.designer as dz
    from canvas_qt import i18n
    monkeypatch.setattr(dz, "get_language", lambda: "zh")   # override the en pin
    i18n.set_language("zh")
    try:
        w = dz.CanvasWindow()
        menus = [a.text() for a in w.menubar.actions()]
        assert any("图" in m for m in menus) and any("生成" in m for m in menus), menus
        btns = {b.text() for b in w.findChildren(QPushButton)}
        assert {"智能体", "大模型", "工具"} <= btns, sorted(btns)[:10]
        w._clean_snapshot = w._snapshot(); w.close()
    finally:
        i18n.set_language("en")
