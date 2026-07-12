"""Shared fixtures for the canvas_qt designer tests (split out of the former
monolithic test_canvas_qt.py). Scoped to this subdir so the autouse fixtures
do not leak into the rest of the suite."""
from __future__ import annotations

import os
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402,F401
from canvas_qt.designer import CanvasWindow  # noqa: E402,F401


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def _no_modal_dialogs(monkeypatch):
    """The designer opens a modal config dialog when adding/configuring a node;
    stub it out so headless tests don't block on .exec()."""
    monkeypatch.setattr("canvas_qt.designer.open_config_dialog",
                        lambda parent, node: None)


@pytest.fixture(autouse=True)
def _english_ui(monkeypatch):
    """Pin the UI language to English so the (English-asserting) canvas tests are
    hermetic regardless of the user's saved `language` config, and so a prior
    test's language switch can't leak into later windows via i18n's module state."""
    import canvas_qt.i18n as _i18n
    monkeypatch.setattr("canvas_qt.designer.get_language", lambda: "en")
    _i18n.set_language("en")


@pytest.fixture(autouse=True)
def _isolate_coding_sessions(tmp_path, monkeypatch):
    """Redirect the coding agent's history/session storage to a per-test temp dir,
    so tests that open the Tool Generator never read or write the real project's
    chat history / sessions/ (the CodingAgent now migrates + persists on init)."""
    import coding_agent as CA
    monkeypatch.setattr(CA, "HISTORY_PATH", str(tmp_path / "chat_history.json"))
    monkeypatch.setattr(CA, "SUMMARY_PATH", str(tmp_path / "chat_summary.txt"))
    monkeypatch.setattr(CA, "SESSIONS_DIR", str(tmp_path / "sessions"))


@pytest.fixture
def win(qapp):
    w = CanvasWindow()
    yield w
    # Mark clean before closing so teardown never triggers the unsaved-changes
    # save prompt (a real QMessageBox would hang headless). Tests that need the
    # prompt drive closeEvent themselves with QMessageBox.exec monkeypatched.
    w._clean_snapshot = w._snapshot()
    w.close()
