"""Qt (PySide6) Tool Generator — chat with the built-in coding agent to write &
save Python tools into the library.

This is the Qt port of the wx ``ToolGeneratorFrame``; it runs in-process inside
the Qt app (opened from the canvas designer's Tools menu or a Tool node's
"Create a new tool…" button), so the whole app is now wx-free.

Threading: the coding agent runs on a background thread. It talks back to the UI
through Qt signals (queued onto the GUI thread). The HITL confirm handler is
called *from* the worker thread and must block until the user answers, so it
emits a signal carrying a ``threading.Event`` the worker waits on.
"""

from __future__ import annotations

import json
import threading

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

import coding_agent
from coding_agent import CodingAgent


def _mono() -> QFont:
    f = QFont("Consolas")
    f.setStyleHint(QFont.Monospace)
    f.setPointSize(10)
    return f


class ToolConfirmDialog(QDialog):
    """HITL review for high-risk coding-agent tool calls: the user sees the full
    code before anything is written to the tool library."""

    def __init__(self, parent, tool_name: str, args: dict):
        super().__init__(parent)
        self.setWindowTitle(f"Confirm: {tool_name}")
        self.resize(640, 440)
        v = QVBoxLayout(self)
        if tool_name == "save_tool":
            header = (f"The coding agent wants to save "
                      f"'{args.get('name', '?')}.py' into the tool library.\n"
                      "Review the code before allowing:")
            body = args.get("code", "")
        else:
            header = f"The coding agent wants to call {tool_name} with:"
            body = json.dumps(args, indent=2, ensure_ascii=False)
        v.addWidget(QLabel(header))
        text = QPlainTextEdit(body)
        text.setReadOnly(True)
        text.setFont(_mono())
        v.addWidget(text, 1)

        bb = QDialogButtonBox()
        bb.addButton("Allow", QDialogButtonBox.AcceptRole)
        deny = bb.addButton("Deny", QDialogButtonBox.RejectRole)
        deny.setDefault(True)  # safe default
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)


class _InputEdit(QPlainTextEdit):
    """Multi-line prompt box; Ctrl+Enter submits."""

    submit = Signal()

    def keyPressEvent(self, event):
        if (event.key() in (Qt.Key_Return, Qt.Key_Enter)
                and event.modifiers() & Qt.ControlModifier):
            self.submit.emit()
            return
        super().keyPressEvent(event)


class ToolGeneratorWindow(QMainWindow):
    # Worker-thread → GUI-thread signals (queued).
    sig_emit = Signal(str)       # streaming text chunk
    sig_reply = Signal(str)      # final reply
    sig_error = Signal(str)      # error message
    # name, args, result-dict, done-event — blocking HITL confirm.
    sig_confirm = Signal(object, object, object, object)

    def __init__(self, parent=None, agent=None, title="MetaAgent — Tool Generator",
                 intro="Ask the coding agent to write a tool for you."):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(900, 680)
        self._intro = intro
        # `agent` lets a second window (the graph Designer) reuse this chat UI with
        # a different agent (its own brain/tools/sessions). Default = tool generator,
        # with an UNLIMITED tool-call budget (writing/reviewing a tool is multi-step;
        # the loop still ends when the model stops calling tools or the user cancels).
        self.agent = agent if agent is not None else CodingAgent(max_tool_rounds=0)
        # Worker threads blocked in a HITL confirm wait on these events; closeEvent
        # releases them so closing the window can't deadlock a worker.
        self._pending_confirms: set = set()
        self._confirm_lock = threading.Lock()
        coding_agent.set_confirm_handler(self._confirm_tool)

        self.sig_emit.connect(self._append_chunk)
        self.sig_reply.connect(self._on_reply)
        self.sig_error.connect(self._on_error)
        self.sig_confirm.connect(self._show_confirm)

        self._build_menu()
        self._build_ui()
        # A PERMANENT status-bar widget (right side) that always shows token usage +
        # context size — it is NOT cleared by transient showMessage() updates
        # ("Thinking…", "Ready.", etc.), so the counts stay visible at all times.
        self._meta_label = QLabel()
        self.statusBar().addPermanentWidget(self._meta_label)
        self._update_meta()
        self.statusBar().showMessage(self._intro)
        self._replay_history()

    def _update_meta(self) -> None:
        """Refresh the always-on token/context readout (right of the status bar)."""
        u = self.agent.usage
        txt = f"Session: {u['input_tokens']} in + {u['output_tokens']} out tokens"
        tok = getattr(self.agent, "last_context_tokens", 0)
        cap = getattr(self.agent, "context_capacity", 0)
        if cap:
            txt += f"  ·  Context: ~{tok / 1000:.1f}k / {cap // 1000}k ({int(100 * tok / cap)}%)"
        elif tok:
            txt += f"  ·  Context: ~{tok / 1000:.1f}k"
        else:
            txt += "  ·  Context: —"
        self._meta_label.setText(txt)

    # ── UI construction ─────────────────────────────────────────────────────
    def _build_menu(self) -> None:
        mb = self.menuBar()
        file_menu = mb.addMenu("&File")
        close = file_menu.addAction("&Close")
        close.setShortcut("Ctrl+W")
        close.triggered.connect(self.close)

        # Session menu: New Session + recover a recent session (rebuilt on open).
        self._session_menu = mb.addMenu("&Session")
        self._session_menu.aboutToShow.connect(self._rebuild_session_menu)
        self._rebuild_session_menu()

        settings_menu = mb.addMenu("&Settings")
        settings_menu.addAction("&API Key / Model...").triggered.connect(self.on_settings)
        settings_menu.addAction("&Clear Chat Memory").triggered.connect(self.on_clear_memory)

    def _rebuild_session_menu(self) -> None:
        m = self._session_menu
        m.clear()
        new = m.addAction("&New Session")
        new.setShortcut("Ctrl+N")
        new.triggered.connect(self.on_new_session)
        clear = m.addAction("Clear &History...")
        clear.setToolTip("Delete ALL saved sessions and start fresh.")
        clear.triggered.connect(self.on_clear_history)
        m.addSeparator()
        try:
            sessions = self.agent.list_sessions()
        except Exception:
            sessions = []
        if not sessions:
            m.addAction("(no sessions yet)").setEnabled(False)
        for s in sessions[:10]:
            mark = "● " if s.get("active") else "○ "
            when = ("  — " + s["updated"]) if s.get("updated") else ""
            act = m.addAction(mark + (s.get("title") or "(session)") + when)
            act.triggered.connect(
                lambda checked=False, i=s["id"]: self.on_load_session(i))

    def on_new_session(self) -> None:
        self.agent.new_session()
        self.chat.clear()
        self._replay_history()
        self._update_meta()
        self.statusBar().showMessage("Started a new session.")

    def on_load_session(self, sid: str) -> None:
        if self.agent.load_session(sid):
            self.chat.clear()
            self._replay_history()
            self._update_meta()
            self.statusBar().showMessage("Recovered session.")

    def on_clear_history(self) -> None:
        """Delete ALL saved sessions and start a fresh, empty conversation."""
        if QMessageBox.question(
                self, "Clear history",
                "Delete ALL saved sessions and start fresh?\nThis cannot be undone."
                ) != QMessageBox.Yes:
            return
        n = self.agent.clear_all_sessions()
        self.chat.clear()
        self._replay_history()
        self._update_meta()
        self._rebuild_session_menu()
        self.statusBar().showMessage("History cleared (%d session(s) removed)." % n)

    def _build_ui(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        v = QVBoxLayout(root)

        self.chat = QPlainTextEdit()
        self.chat.setReadOnly(True)
        self.chat.setFont(_mono())
        v.addWidget(self.chat, 1)

        row = QHBoxLayout()
        self.input = _InputEdit()
        self.input.setPlaceholderText(
            'Ask for a tool, e.g. "write a tool that loads a CSV file"  '
            "(Ctrl+Enter to send)")
        self.input.setFixedHeight(80)
        self.input.submit.connect(self.on_send)
        row.addWidget(self.input, 1)

        btns = QVBoxLayout()
        self.send_btn = QPushButton("Send")
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setEnabled(False)
        self.save_btn = QPushButton("Save Tool(s)")
        self.send_btn.clicked.connect(self.on_send)
        self.stop_btn.clicked.connect(self.on_stop)
        self.save_btn.clicked.connect(self.on_save_tools)
        btns.addWidget(self.send_btn)
        btns.addWidget(self.stop_btn)
        btns.addWidget(self.save_btn)
        btns.addStretch(1)
        row.addLayout(btns)
        v.addLayout(row)

    # ── helpers ─────────────────────────────────────────────────────────────
    def _append(self, speaker: str, text: str) -> None:
        self.chat.appendPlainText(f"{speaker}:\n{text.strip()}\n")

    def _append_chunk(self, s: str) -> None:
        self.chat.insertPlainText(s + "\n")
        self.chat.ensureCursorVisible()

    def _replay_history(self) -> None:
        for msg in self.agent.history:
            speaker = "You" if msg["role"] == "user" else "Agent"
            self._append(speaker, msg["content"])

    # ── HITL confirm (thread-safe, blocking) ─────────────────────────────────
    def _confirm_tool(self, tool_name: str, args: dict) -> bool:
        """Called from the agent worker thread; blocks until the user answers."""
        done = threading.Event()
        result = {"ok": False}
        with self._confirm_lock:
            self._pending_confirms.add(done)
        try:
            self.sig_confirm.emit(tool_name, args, result, done)
            done.wait()
        finally:
            with self._confirm_lock:
                self._pending_confirms.discard(done)
        return result["ok"]

    def _show_confirm(self, tool_name, args, result, done) -> None:
        try:
            dlg = ToolConfirmDialog(self, tool_name, args)
            result["ok"] = dlg.exec() == QDialog.Accepted
        finally:
            done.set()

    # ── events ──────────────────────────────────────────────────────────────
    def on_send(self) -> None:
        text = self.input.toPlainText().strip()
        if not text:
            return
        if not self.agent.has_api_key():
            QMessageBox.warning(
                self, "API key missing",
                "No API key configured.\nSet it in Settings → "
                "API Key / Model.")
            self.on_settings()
            return
        self.input.clear()
        self._append("You", text)
        self.send_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.statusBar().showMessage("Thinking…  (Stop to interrupt)")
        # (re)claim the shared HITL confirm slot for THIS window before running, so
        # with both windows open the right dialog handles the confirm.
        coding_agent.set_confirm_handler(self._confirm_tool)
        threading.Thread(target=self._llm_worker, args=(text,), daemon=True).start()

    def on_stop(self) -> None:
        self.agent.cancel()
        self.stop_btn.setEnabled(False)
        self.statusBar().showMessage("Stopping…")

    def _safe_emit(self, signal_name: str, payload) -> None:
        """Emit a worker-thread → GUI signal, tolerating the window having been torn
        down mid-request. The Tool Generator window is WA_DeleteOnClose, and a slow
        request (e.g. a connect that runs to its timeout) can outlive it: once the
        C++ QObject is gone, even touching the signal raises
        RuntimeError('Signal source has been deleted'). There's nothing left to
        update, so swallow it instead of crashing the worker thread."""
        try:
            getattr(self, signal_name).emit(payload)
        except RuntimeError:
            pass

    def _llm_worker(self, text: str) -> None:
        try:
            reply = self.agent.send(text, emit=lambda s: self._safe_emit("sig_emit", s))
            self._safe_emit("sig_reply", reply)
        except Exception as e:  # noqa: BLE001
            self._safe_emit("sig_error", str(e))

    def _on_reply(self, reply: str) -> None:
        self._append("Agent", reply)
        self.send_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        n = len(self.agent.extract_tools_from_last_reply())
        base = (f"Reply contains {n} tool(s) — click 'Save Tool(s)' to add to "
                "the library." if n else "Ready.")
        self.statusBar().showMessage(base)
        self._update_meta()      # refresh the always-on token/context readout

    def _on_error(self, err: str) -> None:
        self._append("Error", err)
        self.send_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.statusBar().showMessage("Error — check your API key / network.")

    def on_save_tools(self) -> None:
        tools = self.agent.extract_tools_from_last_reply()
        if not tools:
            QMessageBox.information(
                self, "Nothing to save",
                "No ```python code block with a function found in the last reply.")
            return
        saved, failed = [], []
        for name, code in tools:
            result = self.agent.save_tool(name, code)
            if result.startswith("[ERROR]"):
                failed.append(f"{name}: {result}")
            else:
                saved.append(name)
        if saved and not failed:
            QMessageBox.information(
                self, "Tools saved",
                f"Saved {len(saved)} tool(s) to the library:\n{', '.join(saved)}\n\n"
                "Link them to an agent in the canvas designer (Tool node).")
        elif saved and failed:
            QMessageBox.warning(
                self, "Some tools not saved",
                f"Saved {len(saved)}: {', '.join(saved)}\n\n"
                f"Skipped {len(failed)} (not written):\n" + "\n".join(failed))
        else:
            QMessageBox.critical(
                self, "Nothing saved",
                "No tool was saved — validation failed:\n" + "\n".join(failed))

    def on_settings(self) -> None:
        from canvas_qt.welcome import SettingsDialog
        dlg = SettingsDialog(self)
        if dlg.exec() == QDialog.Accepted:
            dlg.save()
            self.statusBar().showMessage("Settings saved.")

    def on_clear_memory(self) -> None:
        if QMessageBox.question(
                self, "Confirm",
                "Clear the coding agent's conversation memory?") == QMessageBox.Yes:
            self.agent.clear_memory()
            self.chat.clear()
            self._update_meta()
            self.statusBar().showMessage("Memory cleared.")

    # ── teardown ──────────────────────────────────────────────────────────────
    def closeEvent(self, event) -> None:
        """Tear down synchronously on close so that (a) a close-then-reopen in the
        same event-loop turn always builds a fresh window (the singleton global is
        cleared here, not via the asynchronous `destroyed` signal), (b) a still-
        running worker can't fire signals at a half-deleted window, and (c) the
        process-global HITL confirm handler doesn't outlive this window."""
        global _TOOL_GEN_WINDOW
        if _TOOL_GEN_WINDOW is self:
            _TOOL_GEN_WINDOW = None
        try:
            self.agent.cancel()
        except Exception:  # noqa: BLE001 — best-effort; never block the close
            pass
        # release any worker thread parked in a HITL confirm's done.wait()
        with self._confirm_lock:
            pending = list(self._pending_confirms)
            self._pending_confirms.clear()
        for done in pending:
            done.set()
        # restore the shared confirm handler we installed in __init__
        coding_agent.reset_confirm_handler()
        super().closeEvent(event)


# ── app-global singleton ─────────────────────────────────────────────────────
# The Tool Generator is opened from the canvas designer (Tools menu) or a Tool
# node's "Create a new tool…" button. It is kept a SINGLE shared window on
# purpose: the coding agent's confirm handler is a *module-level* global
# (coding_agent.set_confirm_handler) and the tool library is shared, so a second
# window would clobber the first's HITL handler. Parented to None so it outlives
# whichever canvas window opened it.
_TOOL_GEN_WINDOW: ToolGeneratorWindow | None = None


def open_tool_generator() -> ToolGeneratorWindow:
    """Open the shared Tool Generator window, or raise/focus it if already open.

    The global is cleared synchronously in ToolGeneratorWindow.closeEvent (not via
    the asynchronous `destroyed` signal), so it only ever points at a live window
    and a close-then-reopen in the same turn always gets a fresh one."""
    global _TOOL_GEN_WINDOW
    if _TOOL_GEN_WINDOW is not None:
        _TOOL_GEN_WINDOW.show()
        _TOOL_GEN_WINDOW.raise_()
        _TOOL_GEN_WINDOW.activateWindow()
        return _TOOL_GEN_WINDOW

    win = ToolGeneratorWindow()  # parent=None: app-global, outlives any one canvas
    win.setAttribute(Qt.WA_DeleteOnClose, True)
    _TOOL_GEN_WINDOW = win
    win.show()
    win.raise_()
    win.activateWindow()
    return win


# ── Designer agent window (parallel to the Tool Generator) ───────────────────
_DESIGNER_WINDOW: ToolGeneratorWindow | None = None


def open_designer(canvas=None) -> ToolGeneratorWindow:
    """Open the graph Designer agent — the same chat UI bound to a SEPARATE agent
    (design skill + graph tools + its own sessions). A written graph is rendered
    onto `canvas` (the CanvasWindow that opened it)."""
    global _DESIGNER_WINDOW
    if _DESIGNER_WINDOW is not None:
        _DESIGNER_WINDOW.show()
        _DESIGNER_WINDOW.raise_()
        _DESIGNER_WINDOW.activateWindow()
        return _DESIGNER_WINDOW

    import designer_agent
    win = ToolGeneratorWindow(
        agent=designer_agent.make_designer_agent(),
        title="MetaAgent — Designer Agent",
        intro="Describe the agent graph you want; I'll design + render it on the canvas.")
    win.setAttribute(Qt.WA_DeleteOnClose, True)
    # render a written graph onto the canvas that opened the designer
    if canvas is not None:
        designer_agent.set_graph_handler(
            lambda g, name: canvas.load_designed_graph(g, name))

        def _clear(_=None):
            global _DESIGNER_WINDOW
            designer_agent.reset_graph_handler()
            _DESIGNER_WINDOW = None
        win.destroyed.connect(_clear)
    _DESIGNER_WINDOW = win
    win.show()
    win.raise_()
    win.activateWindow()
    return win


def run():
    import sys

    from PySide6.QtWidgets import QApplication

    from canvas_qt.theme import apply_dark_theme
    app = QApplication.instance() or QApplication(sys.argv)
    apply_dark_theme(app)
    win = ToolGeneratorWindow()
    win.show()
    app.exec()


if __name__ == "__main__":
    run()
