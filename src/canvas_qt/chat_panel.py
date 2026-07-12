"""Chat / thread run panel (#4) — multi-turn conversational debug runs.

The generated agent already supports multi-turn (``run()`` accumulates into
``HISTORY`` and persists) and sessions (``new_session`` / ``list_sessions`` /
``load_session`` / ``current_session``). This panel is the view layer over those:
a transcript, an input box (Ctrl+Enter to send, ↑/↓ to recall prior inputs), a
session switcher, and Send/Stop — mirroring the Tool Generator's chat idioms.

It is view-only: it emits ``send`` / ``stop`` / ``new_session`` / ``load_session``
and the designer drives the agent module + run pipeline (reusing the live-overlay
trace panel beside it).
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont, QTextCursor
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .theme import canvas_colors

_MAX_RECALL = 100   # cap the ↑/↓ input-recall ring


class _ChatInput(QPlainTextEdit):
    """Multi-line message box: Ctrl+Enter sends; ↑/↓ recall prior inputs when the
    caret is on the first / last line (so multi-line editing still works)."""

    submit = Signal()
    recall_prev = Signal()
    recall_next = Signal()

    def keyPressEvent(self, event):
        key, mods = event.key(), event.modifiers()
        if key in (Qt.Key_Return, Qt.Key_Enter) and (mods & Qt.ControlModifier):
            self.submit.emit()
            return
        if key == Qt.Key_Up and self.textCursor().blockNumber() == 0:
            self.recall_prev.emit()
            return
        if (key == Qt.Key_Down
                and self.textCursor().blockNumber() == self.document().blockCount() - 1):
            self.recall_next.emit()
            return
        super().keyPressEvent(event)


class ChatPanel(QWidget):
    """Multi-turn chat run panel for the visual agent designer."""

    send = Signal(str)
    stop = Signal()
    new_session = Signal()
    load_session = Signal(str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._inputs: list[str] = []     # prior sent messages, for ↑/↓ recall
        self._recall = None              # index into _inputs (None = not recalling)
        self.setMinimumWidth(240)

        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(6)

        header = QHBoxLayout()
        title = QLabel("Run (chat)")
        tf = title.font()
        tf.setBold(True)
        title.setFont(tf)
        header.addWidget(title)
        header.addStretch(1)
        self.new_btn = QPushButton("New session")
        self.new_btn.setToolTip("Start a fresh conversation (keeps prior sessions).")
        self.new_btn.clicked.connect(self.new_session.emit)
        header.addWidget(self.new_btn)
        root.addLayout(header)

        self.sessions = QComboBox()
        self.sessions.setToolTip("Switch between saved conversations for this agent.")
        self.sessions.currentIndexChanged.connect(self._on_session_pick)
        root.addWidget(self.sessions)

        self.transcript = QPlainTextEdit()
        self.transcript.setReadOnly(True)
        self.transcript.setFont(QFont("Consolas", 9))
        root.addWidget(self.transcript, 1)

        self.input = _ChatInput()
        self.input.setPlaceholderText(
            "Message the agent…  (Ctrl+Enter to send, ↑/↓ recall)")
        self.input.setFixedHeight(64)
        self.input.submit.connect(self._emit_send)
        self.input.recall_prev.connect(self._recall_prev)
        self.input.recall_next.connect(self._recall_next)
        root.addWidget(self.input)

        row = QHBoxLayout()
        row.addStretch(1)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop.emit)
        self.send_btn = QPushButton("Send")
        self.send_btn.clicked.connect(self._emit_send)
        row.addWidget(self.stop_btn)
        row.addWidget(self.send_btn)
        root.addLayout(row)

        self.footer = QLabel("")
        self.footer.setWordWrap(True)
        root.addWidget(self.footer)
        self.restyle()

    # ── sessions ─────────────────────────────────────────────────────────────
    def set_sessions(self, sessions, active_id=None) -> None:
        self.sessions.blockSignals(True)
        self.sessions.clear()
        for s in sessions or []:
            mark = "● " if s.get("active") else "○ "
            label = mark + (s.get("title") or "(session)")
            turns = s.get("turns")
            if turns:
                label += f"  ({turns})"
            self.sessions.addItem(label, s.get("id"))
        if active_id is not None:
            idx = self.sessions.findData(active_id)
            if idx >= 0:
                self.sessions.setCurrentIndex(idx)
        self.sessions.blockSignals(False)

    def _on_session_pick(self, idx: int) -> None:
        sid = self.sessions.itemData(idx)
        if sid:
            self.load_session.emit(str(sid))

    # ── transcript ───────────────────────────────────────────────────────────
    def append(self, speaker: str, text) -> None:
        self.transcript.appendPlainText(f"{speaker}:\n{str(text).strip()}\n")
        self.transcript.ensureCursorVisible()

    def note(self, text: str) -> None:
        self.transcript.appendPlainText(f"— {text} —\n")
        self.transcript.ensureCursorVisible()

    def replay_history(self, history) -> None:
        self.transcript.clear()
        for msg in history or []:
            speaker = "You" if msg.get("role") == "user" else "Agent"
            self.append(speaker, msg.get("content", ""))

    def clear_transcript(self) -> None:
        self.transcript.clear()

    # ── input / busy state ───────────────────────────────────────────────────
    def clear_input(self) -> None:
        self.input.clear()

    def set_busy(self, busy: bool) -> None:
        busy = bool(busy)
        self.send_btn.setEnabled(not busy)
        self.input.setEnabled(not busy)
        self.new_btn.setEnabled(not busy)
        self.sessions.setEnabled(not busy)
        self.stop_btn.setEnabled(busy)
        if busy:
            self.footer.setText("Thinking…  (Stop to interrupt)")

    def set_status(self, text: str) -> None:
        self.footer.setText(text)

    def _emit_send(self) -> None:
        text = self.input.toPlainText().strip()
        if not text:
            return
        self._inputs.append(text)
        del self._inputs[:-_MAX_RECALL]
        self._recall = None
        self.send.emit(text)

    def _recall_prev(self) -> None:
        if not self._inputs:
            return
        if self._recall is None:
            self._recall = len(self._inputs)
        self._recall = max(0, self._recall - 1)
        self.input.setPlainText(self._inputs[self._recall])
        self.input.moveCursor(QTextCursor.End)

    def _recall_next(self) -> None:
        if self._recall is None:
            return
        self._recall += 1
        if self._recall >= len(self._inputs):
            self._recall = None
            self.input.clear()
            return
        self.input.setPlainText(self._inputs[self._recall])
        self.input.moveCursor(QTextCursor.End)

    # ── theme ────────────────────────────────────────────────────────────────
    def restyle(self) -> None:
        self.footer.setStyleSheet(f"color:{canvas_colors()['status']};")
