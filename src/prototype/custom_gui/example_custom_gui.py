"""EXAMPLE custom gui.py — a hand-designed alternative to the standard chat
window, meant to be LOADED into a GUI node (prototype option 1).

It is intentionally minimal (a title, a transcript, an input, a Send button) to
show the contract clearly: a custom GUI drives the agent ONLY through the
generated `core` (agent.py) API — see CONTRACT.md. Layout, widgets and branding
are yours; the agent wiring is the part that must match.

This file is a TEMPLATE: @AGENT_NAME@ is substituted at generation time. It is
NOT runnable on its own — `import agent as core` needs the generated agent.py
that sits next to gui.py inside a generated agent folder, which doesn't exist
until this is loaded into a GUI node and an agent is generated.

To see it run for real:  python prototype/custom_gui/run_example.py
"""

import os
import sys
import threading

if not getattr(sys, "frozen", False):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtCore import Signal
from PySide6.QtGui import QActionGroup
from PySide6.QtWidgets import (
    QApplication,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

import agent as core  # the generated runtime — the only way to drive the agent


class CustomGui(QMainWindow):
    # Worker threads must not touch widgets directly; hop to the GUI thread.
    _line = Signal(str)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("@AGENT_NAME@ — custom front-end")
        self.resize(640, 480)
        self._line.connect(self._append)

        root = QWidget()
        self.setCentralWidget(root)
        v = QVBoxLayout(root)
        v.addWidget(QLabel("A hand-designed GUI for @AGENT_NAME@ "
                           "(loaded into the GUI node)."))
        self.transcript = QPlainTextEdit()
        self.transcript.setReadOnly(True)
        v.addWidget(self.transcript, 1)
        self.input = QLineEdit()
        self.input.setPlaceholderText("Ask the agent…  (Enter to send)")
        self.input.returnPressed.connect(self.on_send)
        v.addWidget(self.input)
        send = QPushButton("Send")
        send.clicked.connect(self.on_send)
        v.addWidget(send)

        self._build_model_menu()

    # ── connecting a menu to a real configurable parameter: the LLM ──────────
    def _build_model_menu(self) -> None:
        """One submenu per agent that has more than one linked LLM (a fallback
        chain). This is the pattern for wiring ANY control to a configurable
        parameter:

          read  : core.PIPELINE / core.get_llm_options(a) / core.get_llm_choice(a)
          write : core.set_llm_choice(a, idx)  -> persists to llm_choice.json

        set_llm_choice mutates the runtime's state, so the NEXT core.run() tries
        the chosen model first (the others stay as fallbacks).
        """
        switchable = [a for a in core.PIPELINE
                      if len(core.get_llm_options(a)) > 1]
        if not switchable:
            return
        # keep Python refs (a QMenu/QActionGroup handed back by addMenu can be
        # garbage-collected otherwise)
        self._model_menu = self.menuBar().addMenu("&Model")
        self._model_submenus, self._model_groups = [], []
        for agent_name in switchable:
            sub = self._model_menu.addMenu(agent_name)
            group = QActionGroup(self)
            group.setExclusive(True)
            self._model_submenus.append(sub)
            self._model_groups.append(group)
            current = core.get_llm_choice(agent_name)
            for i, model in enumerate(core.get_llm_options(agent_name)):
                act = sub.addAction(f"{i + 1}. {model}")
                act.setCheckable(True)
                act.setChecked(i == current)
                group.addAction(act)
                act.triggered.connect(
                    lambda checked=False, a=agent_name, idx=i: self._switch_llm(a, idx))

    def _switch_llm(self, agent_name: str, idx: int) -> None:
        core.set_llm_choice(agent_name, idx)                 # applies + persists
        self._append(f"[model] {agent_name} → {core.get_llm_options(agent_name)[idx]}")

    def _append(self, text: str) -> None:
        self.transcript.appendPlainText(text)

    def on_send(self) -> None:
        task = self.input.text().strip()
        if not task:
            return
        self.input.clear()
        self._append(f"You: {task}")
        threading.Thread(target=self._run, args=(task,), daemon=True).start()

    def _run(self, task: str) -> None:
        # CONTRACT: core.run(task, emit=..., on_token=...) drives the agent.
        # emit() = trace lines; on_token() = streamed answer tokens. Both fire on
        # the worker thread, so marshal to the GUI thread via the signal.
        try:
            result = core.run(
                task,
                emit=lambda s: self._line.emit(s),
                on_token=lambda d: self._line.emit(d))
            self._line.emit(f"=== {result}")
        except Exception as e:  # noqa: BLE001
            self._line.emit(f"[error] {e}")


if __name__ == "__main__":
    app = QApplication.instance() or QApplication(sys.argv)
    CustomGui().show()
    sys.exit(app.exec())
