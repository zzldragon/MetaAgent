"""Tool Generator (in-canvas launch + the Qt window)."""

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



def _node(kind):
    g = Graph()
    return g.new_node(kind, 10, 10)



# ── Tool Generator moved into the canvas designer ────────────────────────────
@pytest.fixture
def reset_tool_gen():
    """Hand back the tool_generator module with a clean app-global Tool Generator
    singleton before and after the test. closeEvent clears the global and restores
    the shared confirm handler synchronously, so closing the window is enough."""
    from canvas_qt import tool_generator as TG

    TG._TOOL_GEN_WINDOW = None
    yield TG
    win = TG._TOOL_GEN_WINDOW
    if win is not None:
        win.close()
    TG._TOOL_GEN_WINDOW = None


def test_canvas_has_tools_menu_for_generator(win):
    """The canvas designer exposes a Tools → Tool Generator menu action."""
    assert hasattr(win, "act_tool_gen")
    assert win.act_tool_gen.text() == "&Tool Generator..."
    assert win.act_tool_gen in win._tools_menu.actions()
    # robust introspection without the QAction.menu() ownership quirk
    assert any("Tool Generator" in a.text() for a in win.findChildren(QAction))


def test_canvas_tools_menu_survives_show_and_gc(win, qapp):
    """Regression: the Tools submenu's QMenu was being garbage-collected on
    show() (QLayout.setMenuBar on a plain QWidget), silently emptying the menu.
    Accessing it after show()+GC must not raise 'C++ object already deleted'."""
    import gc

    win.show()
    qapp.processEvents()
    gc.collect()
    qapp.processEvents()
    assert [a.text() for a in win._tools_menu.actions()] == [
        "&Tool Generator...", "&Designer Agent..."]


def test_canvas_menu_opens_tool_generator_singleton(win, reset_tool_gen):
    TG = reset_tool_gen
    # Fire the QAction itself (exercises the triggered.connect wiring), not the slot.
    win.act_tool_gen.trigger()
    first = TG._TOOL_GEN_WINDOW
    assert isinstance(first, TG.ToolGeneratorWindow)
    # Triggering again focuses the same window rather than opening a second.
    win.act_tool_gen.trigger()
    assert TG._TOOL_GEN_WINDOW is first


def test_tool_generator_shows_tokens_and_context(qapp, reset_tool_gen):
    """Token usage + context size live in a PERMANENT status-bar widget (always
    visible, not clobbered by transient 'Thinking…'/'Ready.' messages). Context
    shows a bare token count with no capacity, and a /capacity (%) ratio with one."""
    from canvas_qt.tool_generator import ToolGeneratorWindow
    w = ToolGeneratorWindow()
    try:
        w.agent.extract_tools_from_last_reply = lambda: []   # isolate from history
        w.agent.usage = {"input_tokens": 1200, "output_tokens": 340}
        w.agent.last_context_tokens = 5200
        w.agent.context_capacity = 0                         # no capacity configured
        w._on_reply("ok")
        meta = w._meta_label.text()
        assert "Session: 1200 in + 340 out" in meta, meta
        assert "Context" in meta and "tokens" not in w.statusBar().currentMessage()
        # the permanent readout survives a transient status message
        w.statusBar().showMessage("Thinking...")
        assert "Session:" in w._meta_label.text()
        w.agent.context_capacity = 128000                    # capacity set -> ratio
        w._update_meta()
        assert "/" in w._meta_label.text() and "%" in w._meta_label.text()
    finally:
        w.close()


def test_tool_generator_session_menu(qapp, reset_tool_gen):
    """The Tool Generator has a Session menu listing recent sessions; New Session
    and recovering a session both refresh the transcript via _replay_history."""
    from canvas_qt.tool_generator import ToolGeneratorWindow
    w = ToolGeneratorWindow()
    try:
        calls = {"new": 0, "load": None}
        w.agent.list_sessions = lambda: [
            {"id": "s1", "title": "first chat", "updated": "2026-06-20 10:00",
             "turns": 4, "active": True},
            {"id": "s2", "title": "older chat", "updated": "2026-06-19 09:00",
             "turns": 2, "active": False}]
        w.agent.new_session = lambda: calls.__setitem__("new", calls["new"] + 1) or "s3"
        w.agent.load_session = lambda sid: (calls.__setitem__("load", sid) or True)
        w.agent.history = []
        w._rebuild_session_menu()
        labels = [a.text() for a in w._session_menu.actions() if not a.isSeparator()]
        assert any("New Session" in t for t in labels), labels
        assert any("Clear" in t and "History" in t for t in labels), labels
        assert any("first chat" in t for t in labels), labels
        assert any("older chat" in t for t in labels), labels
        w.on_new_session()
        assert calls["new"] == 1
        w.on_load_session("s2")
        assert calls["load"] == "s2"
    finally:
        w.close()


def test_tool_generator_clear_history(qapp, reset_tool_gen, monkeypatch):
    """Session → Clear History wipes ALL sessions via the agent and refreshes the UI."""
    from canvas_qt.tool_generator import ToolGeneratorWindow
    from PySide6.QtWidgets import QMessageBox
    w = ToolGeneratorWindow()
    try:
        calls = {"cleared": 0}
        w.agent.clear_all_sessions = lambda: (calls.__setitem__("cleared", 1) or 3)
        # confirm the destructive prompt
        monkeypatch.setattr(QMessageBox, "question",
                            staticmethod(lambda *a, **k: QMessageBox.Yes))
        w.on_clear_history()
        assert calls["cleared"] == 1
        # declining the prompt must NOT clear
        calls["cleared"] = 0
        monkeypatch.setattr(QMessageBox, "question",
                            staticmethod(lambda *a, **k: QMessageBox.No))
        w.on_clear_history()
        assert calls["cleared"] == 0
    finally:
        w.close()


def test_tool_generator_closeEvent_resets_singleton_and_handler(reset_tool_gen):
    """closeEvent must clear the singleton synchronously and restore the shared
    confirm handler, so a reopen in the same turn builds a fresh window and no
    stale bound method lingers in coding_agent."""
    import coding_agent
    TG = reset_tool_gen
    w = TG.open_tool_generator()
    assert coding_agent._CONFIRM["fn"] == w._confirm_tool
    w.close()
    assert TG._TOOL_GEN_WINDOW is None                       # synchronous clear
    assert coding_agent._CONFIRM["fn"] is coding_agent._default_confirm
    # reopen in the same turn yields a brand-new window, not the closed one
    w2 = TG.open_tool_generator()
    assert w2 is not w


def test_open_tool_generator_is_singleton(reset_tool_gen):
    TG = reset_tool_gen
    a = TG.open_tool_generator()
    b = TG.open_tool_generator()
    assert a is b
    assert TG._TOOL_GEN_WINDOW is a


def test_tool_dialog_create_button_opens_generator(qapp, reset_tool_gen):
    from PySide6.QtWidgets import QPushButton
    TG = reset_tool_gen
    node = _node("tool")
    dlg = D.ToolDialog(None, node)
    try:
        # Window-modal so the Tool Generator stays interactive while it is open.
        assert dlg.windowModality() == Qt.WindowModal
        # Click the actual button (exercises clicked.connect), not the slot.
        btn = next(b for b in dlg.findChildren(QPushButton)
                   if "Create a new tool" in b.text())
        btn.click()
        assert isinstance(TG._TOOL_GEN_WINDOW, TG.ToolGeneratorWindow)
    finally:
        dlg.close()


def test_tool_dialog_activation_refreshes_list(qapp, monkeypatch):
    """The headline feature: when focus returns to the dialog (after creating a
    tool in the Tool Generator), changeEvent re-scans and the new file appears.
    Drive the activation path directly since offscreen has no real focus."""
    from PySide6.QtCore import QEvent
    node = _node("tool")
    monkeypatch.setattr("codegen.list_tools", lambda: ["alpha.py"])
    dlg = D.ToolDialog(None, node)
    try:
        monkeypatch.setattr(dlg, "isActiveWindow", lambda: True)
        # nothing changed yet → activation is a no-op (no flicker)
        dlg.changeEvent(QEvent(QEvent.ActivationChange))
        assert [dlg.listw.item(i).text() for i in range(dlg.listw.count())] == ["alpha.py"]
        # a tool was created; activation now surfaces it
        monkeypatch.setattr("codegen.list_tools", lambda: ["alpha.py", "beta.py"])
        dlg.changeEvent(QEvent(QEvent.ActivationChange))
        labels = [dlg.listw.item(i).text() for i in range(dlg.listw.count())]
        assert set(labels) == {"alpha.py", "beta.py"}
    finally:
        dlg.close()


def test_tool_dialog_seeds_and_preserves_unchecked(qapp, monkeypatch):
    """Initial selection seeds from node.props['files']; unchecking then
    refreshing must not resurrect the unticked file."""
    monkeypatch.setattr("codegen.list_tools", lambda: ["a.py", "b.py"])
    node = _node("tool")
    node.props["files"] = ["a.py", "b.py"]
    dlg = D.ToolDialog(None, node)
    try:
        assert set(dlg._checked_files()) == {"a.py", "b.py"}      # seeded from props
        for i in range(dlg.listw.count()):
            it = dlg.listw.item(i)
            if it.text() == "a.py":
                it.setCheckState(Qt.Unchecked)
        # force a rebuild even though the library list is unchanged
        dlg._lib_cache = None
        dlg._reload_library()
        assert dlg._checked_files() == ["b.py"]                   # a.py stays off
    finally:
        dlg.close()


def test_tool_dialog_reload_picks_up_new_tool(qapp, monkeypatch):
    """A tool created in the Tool Generator shows up on refresh, and the user's
    existing checked selection is preserved across the rescan."""
    node = _node("tool")
    monkeypatch.setattr("codegen.list_tools", lambda: ["alpha.py"])
    dlg = D.ToolDialog(None, node)
    try:
        # tick alpha.py
        dlg.listw.item(0).setCheckState(Qt.Checked)
        assert dlg._checked_files() == ["alpha.py"]
        # the generator "creates" beta.py; refresh should add it, keep alpha ticked
        monkeypatch.setattr("codegen.list_tools", lambda: ["alpha.py", "beta.py"])
        dlg._reload_library()
        labels = {dlg.listw.item(i).text(): dlg.listw.item(i).checkState()
                  for i in range(dlg.listw.count())}
        assert set(labels) == {"alpha.py", "beta.py"}
        assert labels["alpha.py"] == Qt.Checked
        assert labels["beta.py"] == Qt.Unchecked
        assert dlg._checked_files() == ["alpha.py"]
    finally:
        dlg.close()


def test_tool_dialog_apply_roundtrip(qapp, monkeypatch):
    node = _node("tool")
    monkeypatch.setattr("codegen.list_tools", lambda: ["t1.py", "t2.py"])
    dlg = D.ToolDialog(None, node)
    try:
        dlg.name.setText("my_tools")
        for i in range(dlg.listw.count()):
            it = dlg.listw.item(i)
            it.setCheckState(Qt.Checked if it.text() == "t2.py" else Qt.Unchecked)
        assert dlg.apply() is None
        assert node.props["files"] == ["t2.py"]
        assert node.name == "my_tools"
    finally:
        dlg.close()


def test_tool_dialog_empty_library_placeholder(qapp, monkeypatch):
    node = _node("tool")
    monkeypatch.setattr("codegen.list_tools", lambda: [])
    dlg = D.ToolDialog(None, node)
    try:
        assert dlg.listw.count() == 1
        assert "empty" in dlg.listw.item(0).text()
        assert dlg._checked_files() == []  # placeholder is not checkable
        assert dlg.apply() is None
        assert node.props["files"] == []
    finally:
        dlg.close()


def test_designer_import_stays_off_the_coding_agent():
    """Lazy-import invariant (the architectural reason for opening the Tool
    Generator via a lazy import): importing the canvas designer must NOT pull in
    the coding agent or its LLM SDKs. Run in a subprocess so an earlier test that
    imported tool_generator can't pollute sys.modules."""
    import subprocess

    import canvas_qt
    root = os.path.dirname(os.path.dirname(os.path.abspath(canvas_qt.__file__)))
    code = (
        "import sys; sys.path.insert(0, r'{root}');"
        "import canvas_qt.designer;"
        "leaked=[m for m in ('coding_agent','canvas_qt.tool_generator',"
        "'openai','langchain_core') if m in sys.modules];"
        "assert not leaked, leaked; print('OK')"
    ).format(root=root)
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen"}
    r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       text=True, env=env)
    assert r.returncode == 0, r.stderr
    assert "OK" in r.stdout


# ── Qt Tool Generator ────────────────────────────────────────────────────────
def test_tool_generator_appends_chat(qapp):
    from canvas_qt.tool_generator import ToolGeneratorWindow

    w = ToolGeneratorWindow()
    try:
        w._append("You", "hello")
        assert "You" in w.chat.toPlainText()
        assert "hello" in w.chat.toPlainText()
    finally:
        w.close()


def test_tool_generator_save_no_tools_warns(qapp, monkeypatch):
    from canvas_qt.tool_generator import ToolGeneratorWindow

    w = ToolGeneratorWindow()
    try:
        monkeypatch.setattr(w.agent, "extract_tools_from_last_reply", lambda: [])
        seen = {}
        monkeypatch.setattr("PySide6.QtWidgets.QMessageBox.information",
                            staticmethod(lambda *a, **k: seen.update(called=True)))
        w.on_save_tools()
        assert seen.get("called")
    finally:
        w.close()


def test_tool_generator_confirm_dialog_flow(qapp, monkeypatch):
    """_show_confirm builds the HITL dialog, records the answer and releases the
    waiting worker thread (no real threading needed for the slot itself)."""
    import threading

    from canvas_qt import tool_generator as TG

    w = TG.ToolGeneratorWindow()
    try:
        monkeypatch.setattr(TG.ToolConfirmDialog, "exec",
                            lambda self: TG.QDialog.Accepted)
        done = threading.Event()
        result = {"ok": False}
        w._show_confirm("save_tool", {"name": "x", "code": "def x():\n    pass"},
                        result, done)
        assert done.is_set()
        assert result["ok"] is True
    finally:
        w.close()


def test_tool_generator_clear_memory(qapp, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    from canvas_qt.tool_generator import ToolGeneratorWindow

    w = ToolGeneratorWindow()
    try:
        w.agent.history = [{"role": "user", "content": "hi"}]
        w.chat.setPlainText("something")
        monkeypatch.setattr("PySide6.QtWidgets.QMessageBox.question",
                            staticmethod(lambda *a, **k: QMessageBox.Yes))
        w.on_clear_memory()
        assert w.agent.history == []
        assert w.chat.toPlainText() == ""
    finally:
        w.close()


def test_tool_generator_send_without_key_opens_settings(qapp, monkeypatch):
    from canvas_qt.tool_generator import ToolGeneratorWindow

    w = ToolGeneratorWindow()
    try:
        monkeypatch.setattr(w.agent, "has_api_key", lambda: False)
        monkeypatch.setattr("PySide6.QtWidgets.QMessageBox.warning",
                            staticmethod(lambda *a, **k: None))
        opened = {}
        monkeypatch.setattr(w, "on_settings", lambda: opened.update(called=True))
        w.input.setPlainText("write me a tool")
        w.on_send()
        assert opened.get("called")
    finally:
        w.close()
