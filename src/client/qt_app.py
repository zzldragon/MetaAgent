"""PySide6 desktop client for a MetaAgent-generated agent (web-server node on).

This is the interactive "operator console" channel: it opens ONE persistent
connection to the agent's WebSocket server and runs multi-turn conversations,
streaming tokens live, showing step traces, and answering HITL prompts via
dialogs. The transport is AgentClient — the same core the unattended WeChat/
DingTalk/Feishu webhook channels use (see client/channel.py + client/channels/).

Async bridge: AgentClient is asyncio; Qt is its own event loop. We run one
asyncio loop in a background thread (AgentWorker) and marshal everything to the
GUI thread through Qt signals (cross-thread signals are queued = thread-safe).

Run:
    python -m client --url ws://127.0.0.1:8765 [--token SECRET] [--connect]
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import threading

from PySide6.QtCore import Qt, QObject, Signal
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (QApplication, QCheckBox, QDialog, QDialogButtonBox,
                               QHBoxLayout, QLabel, QLineEdit, QMainWindow,
                               QMessageBox, QPlainTextEdit, QPushButton, QSplitter,
                               QTextEdit, QVBoxLayout, QWidget)

from client.agent_client import AgentClient


# ── async worker: owns one asyncio loop + AgentClient in a background thread ──
class AgentWorker(QObject):
    connected = Signal(dict)
    disconnected = Signal(str)
    token = Signal(str)
    trace = Signal(str)
    result = Signal(str, list)
    error = Signal(str)
    status = Signal(str)
    busy = Signal(bool)
    # kind, prompt, content, request_id  -> GUI shows a dialog, calls answer_hitl()
    hitl = Signal(str, str, str, int)

    def __init__(self):
        super().__init__()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client: AgentClient | None = None
        self._busy = False
        self._hitl_futures: dict[int, asyncio.Future] = {}
        self._hitl_seq = 0
        self._ready = threading.Event()
        threading.Thread(target=self._run_loop, daemon=True).start()
        # Block until the loop is actually running, so the very first connect()
        # (especially with --connect, fired right after construction) is never
        # dropped by run_coroutine_threadsafe before the loop exists.
        self._ready.wait(5)

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.call_soon(self._ready.set)     # signal once run_forever() is live
        self._loop.run_forever()

    def _submit(self, coro):
        if self._loop is not None:
            asyncio.run_coroutine_threadsafe(coro, self._loop)
        else:                                     # loop never came up (shouldn't happen)
            self.error.emit("internal: event loop not running")

    # ── GUI-thread API (all thread-safe; they only schedule onto the loop) ────
    def connect(self, url: str, token: str):
        self._submit(self._connect(url, token))

    def disconnect(self):
        self._submit(self._disconnect())

    def send(self, task: str, images=None):
        self._submit(self._run(task, images or []))

    def cancel(self):
        if self._client is not None:
            self._submit(self._client.cancel())

    def answer_hitl(self, req_id: int, decision):
        fut = self._hitl_futures.pop(req_id, None)
        if fut is not None and self._loop is not None:
            self._loop.call_soon_threadsafe(fut.set_result, decision)

    # ── coroutines (run on the loop thread) ───────────────────────────────────
    async def _connect(self, url, token):
        await self._disconnect()
        try:
            self._client = AgentClient(url, token)
            hello = await self._client.connect()
            self.connected.emit(hello)
            self.status.emit(f"Connected to {hello.get('agent', 'agent')}")
        except Exception as e:                       # noqa: BLE001 (surface any failure)
            self._client = None
            self.error.emit(f"connect failed: {e}")
            self.disconnected.emit(str(e))

    async def _disconnect(self):
        if self._client is not None:
            await self._client.close()
            self._client = None
            self.disconnected.emit("disconnected")

    async def _run(self, task, images):
        if self._client is None:
            self.error.emit("not connected")
            return
        if self._busy:
            self.error.emit("a run is already in progress")
            return
        self._busy = True
        self.busy.emit(True)
        self.status.emit("Thinking…")
        try:
            res = await self._client.run(
                task, images=images,
                on_token=lambda d: self.token.emit(d),
                on_trace=lambda t: self.trace.emit(t),
                on_hitl=self._on_hitl)
            self.result.emit(res.text, res.files)
            self.status.emit("Ready.")
        except Exception as e:                       # noqa: BLE001
            self.error.emit(str(e))
            self.status.emit("Error.")
        finally:
            self._busy = False
            self.busy.emit(False)

    async def _on_hitl(self, kind, prompt, content):
        self._hitl_seq += 1
        rid = self._hitl_seq
        fut = self._loop.create_future()
        self._hitl_futures[rid] = fut
        self.hitl.emit(kind, prompt, content, rid)   # GUI thread shows the dialog
        return await fut                             # resolved by answer_hitl()


class _ConfirmDialog(QDialog):
    """High-risk tool confirmation: show the FULL prompt (tool + args, never
    trimmed) in a resizable, scrollable box. Returns True to allow."""
    def __init__(self, parent, prompt):
        super().__init__(parent)
        self.setWindowTitle("Approve tool call")
        self.resize(680, 520)
        self.setSizeGripEnabled(True)
        self.setWindowFlags(self.windowFlags()
                            | Qt.WindowMaximizeButtonHint | Qt.WindowMinimizeButtonHint)
        self.allow = False
        v = QVBoxLayout(self)
        v.addWidget(QLabel("Review this high-risk tool call before allowing it:"))
        box = QPlainTextEdit(prompt or "Allow this tool call?")
        box.setReadOnly(True)
        v.addWidget(box, 1)
        bb = QDialogButtonBox()
        deny = bb.addButton("Deny", QDialogButtonBox.RejectRole)
        allow = bb.addButton("Allow", QDialogButtonBox.AcceptRole)
        allow.clicked.connect(lambda: self._set(True))
        deny.clicked.connect(lambda: self._set(False))
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def _set(self, allow):
        self.allow = allow
        self.accept()


class _ReviewDialog(QDialog):
    """Human-review checkpoint: show the prompt + (editable) content, return a
    {decision, content, feedback} dict."""
    def __init__(self, parent, prompt, content):
        super().__init__(parent)
        self.setWindowTitle("Human review")
        self.resize(680, 520)
        self.setSizeGripEnabled(True)
        self.setWindowFlags(self.windowFlags()
                            | Qt.WindowMaximizeButtonHint | Qt.WindowMinimizeButtonHint)
        v = QVBoxLayout(self)
        v.addWidget(QLabel(prompt or "Review the agent's output:"))
        self.body = QPlainTextEdit(content or "")
        v.addWidget(self.body, 1)
        v.addWidget(QLabel("Feedback (optional):"))
        self.feedback = QLineEdit()
        v.addWidget(self.feedback)
        self._decision = "reject"
        bb = QDialogButtonBox()
        approve = bb.addButton("Approve", QDialogButtonBox.AcceptRole)
        edit = bb.addButton("Approve edited", QDialogButtonBox.AcceptRole)
        reject = bb.addButton("Reject", QDialogButtonBox.RejectRole)
        approve.clicked.connect(lambda: self._set("approve"))
        edit.clicked.connect(lambda: self._set("edit"))
        reject.clicked.connect(lambda: self._set("reject"))
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def _set(self, decision):
        self._decision = decision
        self.accept()

    def result_dict(self):
        return {"decision": self._decision,
                "content": self.body.toPlainText(),
                "feedback": self.feedback.text()}


class ChatWindow(QMainWindow):
    def __init__(self, url="ws://127.0.0.1:8765", token="", auto_connect=False):
        super().__init__()
        self.setWindowTitle("MetaAgent Client")
        self.resize(900, 640)
        self._streamed = False          # did this turn stream any tokens?
        self._connected = False

        self.worker = AgentWorker()
        self.worker.connected.connect(self._on_connected)
        self.worker.disconnected.connect(self._on_disconnected)
        self.worker.token.connect(self._on_token)
        self.worker.trace.connect(self._on_trace)
        self.worker.result.connect(self._on_result)
        self.worker.error.connect(self._on_error)
        self.worker.status.connect(lambda s: self.statusBar().showMessage(s))
        self.worker.busy.connect(self._on_busy)
        self.worker.hitl.connect(self._on_hitl)

        self._build_ui(url, token)
        if auto_connect:
            self._toggle_connect()

    # ── UI ────────────────────────────────────────────────────────────────────
    def _build_ui(self, url, token):
        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)

        bar = QHBoxLayout()
        bar.addWidget(QLabel("Agent:"))
        self.url_edit = QLineEdit(url)
        bar.addWidget(self.url_edit, 1)
        bar.addWidget(QLabel("Token:"))
        self.token_edit = QLineEdit(token)
        self.token_edit.setEchoMode(QLineEdit.Password)
        self.token_edit.setMaximumWidth(160)
        bar.addWidget(self.token_edit)
        self.connect_btn = QPushButton("Connect")
        self.connect_btn.clicked.connect(self._toggle_connect)
        bar.addWidget(self.connect_btn)
        self.trace_chk = QCheckBox("Trace")
        self.trace_chk.toggled.connect(self._toggle_trace)
        bar.addWidget(self.trace_chk)
        outer.addLayout(bar)

        self.split = QSplitter(Qt.Horizontal)
        self.transcript = QTextEdit(readOnly=True)
        self.split.addWidget(self.transcript)
        self.tracebox = QPlainTextEdit(readOnly=True)
        self.tracebox.setVisible(False)
        self.split.addWidget(self.tracebox)
        self.split.setSizes([640, 0])
        outer.addWidget(self.split, 1)

        row = QHBoxLayout()
        self.input = QPlainTextEdit()
        self.input.setPlaceholderText("Type a message…  (Ctrl+Enter to send)")
        self.input.setMaximumHeight(90)
        row.addWidget(self.input, 1)
        col = QVBoxLayout()
        self.send_btn = QPushButton("Send")
        self.send_btn.clicked.connect(self._send)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.worker.cancel)
        col.addWidget(self.send_btn)
        col.addWidget(self.stop_btn)
        row.addLayout(col)
        outer.addLayout(row)

        self.statusBar().showMessage("Not connected.")
        self._set_inputs_enabled(False)

    def keyPressEvent(self, e):
        if (e.key() in (Qt.Key_Return, Qt.Key_Enter)
                and e.modifiers() & Qt.ControlModifier):
            self._send()
        else:
            super().keyPressEvent(e)

    # ── transcript helpers ─────────────────────────────────────────────────────
    def _append_role(self, role, color):
        self.transcript.moveCursor(QTextCursor.End)
        self.transcript.insertHtml(
            f'<br><b style="color:{color}">{role}:</b> ')
        self.transcript.moveCursor(QTextCursor.End)

    def _append_text(self, text):
        self.transcript.moveCursor(QTextCursor.End)
        self.transcript.insertPlainText(text)
        self.transcript.moveCursor(QTextCursor.End)
        sb = self.transcript.verticalScrollBar()
        sb.setValue(sb.maximum())

    # ── actions ─────────────────────────────────────────────────────────────────
    def _toggle_connect(self):
        if self._connected:
            self.worker.disconnect()
        else:
            self.connect_btn.setEnabled(False)
            self.statusBar().showMessage("Connecting…")
            self.worker.connect(self.url_edit.text().strip(),
                                self.token_edit.text())

    def _send(self):
        text = self.input.toPlainText().strip()
        if not text or not self._connected:
            return
        self.input.clear()
        self._append_role("You", "#1565C0")
        self._append_text(text)
        self._streamed = False
        self.worker.send(text)

    def _set_inputs_enabled(self, on):
        self.input.setEnabled(on)
        self.send_btn.setEnabled(on)

    # ── worker signal handlers (GUI thread) ─────────────────────────────────────
    def _on_connected(self, hello):
        self._connected = True
        self.connect_btn.setText("Disconnect")
        self.connect_btn.setEnabled(True)
        self._set_inputs_enabled(True)
        vision = " · vision" if hello.get("vision") else ""
        self._append_text(f"\n[connected to {hello.get('agent','agent')}{vision}]\n")

    def _on_disconnected(self, why):
        self._connected = False
        self.connect_btn.setText("Connect")
        self.connect_btn.setEnabled(True)
        self._set_inputs_enabled(False)
        self.stop_btn.setEnabled(False)
        self.statusBar().showMessage(
            f"Not connected ({why})" if why and why != "disconnected"
            else "Not connected.")

    def _on_busy(self, busy):
        self.stop_btn.setEnabled(busy)
        self.send_btn.setEnabled(not busy and self._connected)

    def _on_token(self, delta):
        if not self._streamed:
            self._append_role("Agent", "#2E7D32")
            self._streamed = True
        self._append_text(delta)

    def _on_trace(self, line):
        self.tracebox.appendPlainText(line)

    def _on_result(self, text, files):
        if not self._streamed:                  # non-streaming agent: show the result
            self._append_role("Agent", "#2E7D32")
            self._append_text(text)
        if files:
            names = ", ".join(f.get("name", "?") for f in files)
            self._append_text(f"\n[files: {names}]")
        self._append_text("\n")

    def _on_error(self, msg):
        self._append_text(f"\n[error] {msg}\n")

    def _on_hitl(self, kind, prompt, content, req_id):
        if kind == "hitl_confirm":
            dlg = _ConfirmDialog(self, prompt)
            dlg.exec()
            self.worker.answer_hitl(req_id, dlg.allow)
        else:                                   # hitl_review
            dlg = _ReviewDialog(self, prompt, content)
            dlg.exec()
            self.worker.answer_hitl(req_id, dlg.result_dict())

    def _toggle_trace(self, on):
        self.tracebox.setVisible(on)
        self.split.setSizes([640, 260] if on else [900, 0])


def _probe(url, token):
    """Connect once, print the exact result/error, and exit (no GUI). For
    diagnosing 'client can't connect but the web UI can'."""
    from client.agent_client import AgentError

    async def go():
        try:
            async with AgentClient(url, token, open_timeout=8) as ac:
                print(f"OK: connected to {ac.agent_name!r}  "
                      f"auth_required={ac.hello.get('auth_required')}  "
                      f"vision={ac.vision}")
                return 0
        except AgentError as e:
            print(f"FAILED (protocol): {e}")
            print("  -> if it says auth, the agent has an auth_token: re-run with "
                  "--token <the value from config.json -> server.auth_token>")
            return 1
        except Exception as e:                       # noqa: BLE001
            print(f"FAILED ({type(e).__name__}): {e}")
            print("  -> check: is the URL EXACTLY the web UI's address (same host "
                  "AND port)? is server.py actually running? try --url "
                  "ws://127.0.0.1:<port> (not 'localhost', which may resolve to IPv6).")
            return 1

    return asyncio.run(go())


def main(argv=None):
    p = argparse.ArgumentParser(description="PySide6 client for a MetaAgent agent.")
    p.add_argument("--url", default="ws://127.0.0.1:8765",
                   help="agent WebSocket URL (default ws://127.0.0.1:8765)")
    p.add_argument("--token", default="", help="auth token, if the server sets one")
    p.add_argument("--connect", action="store_true", help="connect on startup")
    p.add_argument("--probe", action="store_true",
                   help="connect, print the result/error, and exit (no GUI)")
    args = p.parse_args(argv)

    if args.probe:
        return _probe(args.url, args.token)

    app = QApplication.instance() or QApplication(sys.argv)
    win = ChatWindow(args.url, args.token, auto_connect=args.connect)
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
