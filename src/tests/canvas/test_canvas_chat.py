"""Chat / thread run panel and GUI node."""

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



# ── chat / thread run panel (#4) ───────────────────────────────────────────────
class _SyncThread:
    """Stand-in for threading.Thread that runs the target synchronously, so a
    chat turn completes (and emits turn_done) within the test call."""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._t, self._a, self._k = target, args, kwargs or {}

    def start(self):
        if self._t:
            self._t(*self._a, **self._k)


def _fake_chat_module():
    class M:
        HISTORY = []

        def __init__(self):
            self.HISTORY = []
            self._sessions = [{"id": "s1", "title": "chat", "updated": "now",
                               "turns": 0, "active": True}]

        def run(self, task, emit=None, on_token=None, images=None):
            self.HISTORY.append({"role": "user", "content": task})
            reply = f"echo: {task}"
            self.HISTORY.append({"role": "assistant", "content": reply})
            self._sessions[0]["turns"] = len(self.HISTORY)
            return reply

        def list_sessions(self):
            return [dict(s) for s in self._sessions]

        def current_session(self):
            return next((s["id"] for s in self._sessions if s["active"]), None)

        def new_session(self):
            for s in self._sessions:
                s["active"] = False
            self.HISTORY = []
            self._sessions.append({"id": "s2", "title": "(new)", "updated": "now",
                                   "turns": 0, "active": True})
            return "s2"

        def load_session(self, sid):
            return any(s["id"] == sid for s in self._sessions)

        def request_cancel(self):
            pass
    return M()


def test_chat_run_send_appends_turns(win, monkeypatch):
    """A chat turn appends You + Agent to the transcript and resets the busy
    flag; multi-turn accumulates into the (persisted) module HISTORY."""
    mod = _fake_chat_module()
    monkeypatch.setattr(win, "_chat_ensure_module", lambda: mod)
    monkeypatch.setattr("threading.Thread", _SyncThread)  # run the turn inline
    win._chat_send("hello")
    assert win._chat_running is False
    txt = win.chat_panel.transcript.toPlainText()
    assert "You:" in txt and "hello" in txt
    assert "Agent:" in txt and "echo: hello" in txt
    win._chat_send("again")
    assert mod.HISTORY[-1]["content"] == "echo: again" and len(mod.HISTORY) == 4


def test_chat_run_opens_side_dock(win, monkeypatch):
    """Opening a chat run reveals the chat panel (and trace panel) in the side
    dock, while the top-level palette|workspace splitter stays two-paned."""
    mod = _fake_chat_module()

    def _ensure():                      # mirror the real method: cache the module
        win._chat_mod = mod
        return mod
    monkeypatch.setattr(win, "_chat_ensure_module", _ensure)
    win.on_chat_run()
    assert not win.chat_panel.isHidden()
    assert not win.side_dock.isHidden()
    assert win.splitter.count() == 2          # palette | workspace, unchanged
    # sessions populated from the module
    assert win.chat_panel.sessions.count() == 1


def test_chat_session_controls(win, monkeypatch):
    """New Session clears the transcript and lists a second session; loading a
    known session id succeeds and is refused while a turn is in flight."""
    mod = _fake_chat_module()
    win._chat_mod = mod                 # the session controls read self._chat_mod
    win._refresh_chat_sessions()
    assert win.chat_panel.sessions.count() == 1
    win._chat_new_session()
    assert "new session" in win.chat_panel.transcript.toPlainText()
    assert win.chat_panel.sessions.count() == 2
    win._chat_load_session("s1")              # exists -> ok, no crash
    win._chat_running = True                   # guarded out while busy
    before = win.chat_panel.sessions.count()
    win._chat_new_session()
    assert win.chat_panel.sessions.count() == before


def test_chat_input_recall(qapp):
    """The chat input recalls prior messages with ↑/↓ (Studio-style)."""
    from canvas_qt.chat_panel import ChatPanel
    p = ChatPanel()
    try:
        for msg in ("first", "second"):
            p.input.setPlainText(msg)
            p._emit_send()
        p._recall_prev()
        assert p.input.toPlainText() == "second"
        p._recall_prev()
        assert p.input.toPlainText() == "first"
        p._recall_next()
        assert p.input.toPlainText() == "second"
        p._recall_next()
        assert p.input.toPlainText() == ""     # past the newest -> cleared
    finally:
        p.deleteLater()


def test_chat_turn_sentinels_routed(win):
    """run() returns "[cancelled]…"/"[error]…" sentinels instead of raising; the
    chat panel must not present those as a normal Agent reply under "Ready."."""
    win._chat_turn_done("[cancelled] stopped by the user", None)
    txt = win.chat_panel.transcript.toPlainText()
    assert "stopped by the user" in txt and "Agent:" not in txt
    assert win.chat_panel.footer.text() == "Stopped."

    win.chat_panel.clear_transcript()
    win._chat_turn_done("[error] RuntimeError: boom", None)
    txt = win.chat_panel.transcript.toPlainText()
    assert "Error:" in txt and "boom" in txt and "Agent:" not in txt

    win.chat_panel.clear_transcript()
    win._chat_turn_done("all good", None)
    txt = win.chat_panel.transcript.toPlainText()
    assert "Agent:" in txt and "all good" in txt
    assert win.chat_panel.footer.text() == "Ready."


def test_gui_node_edge_rules():
    from graph_model import Graph
    g = Graph()
    a = g.new_node("agent", 0, 0)
    llm = g.new_node("llm", 0, 0)
    gui = g.new_node("gui", 0, 0)
    assert g.add_edge(gui.id, a.id) is None          # gui -> agent allowed
    assert g.add_edge(gui.id, llm.id) is not None     # gui -> non-agent rejected
    gui2 = g.new_node("gui", 0, 0)
    assert g.add_edge(gui2.id, a.id) is not None      # one GUI per agent (singleton)


def test_gui_dialog_apply(qapp):
    node = _node("gui")
    dlg = D.GUIDialog(None, node)
    dlg.name.setText("desktop")
    assert dlg.apply() is None
    assert node.name == "desktop"


def test_gui_node_in_palette(win):
    from canvas_qt.designer import KIND_LABELS
    assert KIND_LABELS["gui"] == "GUI"
    win.add_node("gui")
    assert any(n.kind == "gui" for n in win.graph.nodes.values())
