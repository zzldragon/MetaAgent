"""Welcome launcher (canvas_qt/welcome.py) — the recent-project row click.

Regression for a real crash: clicking a recent row opened the project, whose
load pops a modal (the tool-conflict dialog); the modal's nested event loop let
an activation-change rebuild the recents list — deleting the very row still inside
its own mouseReleaseEvent — so `super().mouseReleaseEvent()` ran on a freed C++
object (`RuntimeError: Internal C++ object (_RecentRow) already deleted`)."""

from __future__ import annotations

import os
import sys
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
pytest.importorskip("PySide6")

from PySide6.QtCore import QEvent, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QMouseEvent  # noqa: E402

import canvas_qt.welcome as W  # noqa: E402


def _left_release(widget):
    ev = QMouseEvent(QEvent.MouseButtonRelease, QPointF(5, 5),
                     Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
    widget.mouseReleaseEvent(ev)


def test_recent_row_click_survives_reentrant_reload(qapp, monkeypatch):
    """A recent-row click whose handler reloads the list (deleting the row) must
    NOT crash — the click is deferred and the old rows are deleteLater'd."""
    monkeypatch.setattr(W, "load_recent_projects",
                        lambda: [{"path": "X:/nope/proj.mta", "opened_at": time.time()}])
    win = W.WelcomeWindow()
    called = {"n": 0}

    def fake_open_recent(path, exists):
        called["n"] += 1
        win._reload_recents()          # simulate the activation-change reload that
                                       # deletes the just-clicked row mid-open

    monkeypatch.setattr(win, "on_open_recent", fake_open_recent)
    win._reload_recents()              # rebuild rows wired to fake_open_recent
    host = win.recents_area.widget()
    rows = host.findChildren(W._RecentRow)
    assert rows, "a recent row should be present"

    _left_release(rows[0])             # with the old code this crashed on super()
    for _ in range(5):
        qapp.processEvents()           # fire the 0ms timer + process deleteLater

    assert called["n"] == 1, "the deferred click should reach the handler exactly once"
    win.close()


def test_recent_row_defers_the_click(qapp, monkeypatch):
    """The click must fire on the next event-loop tick, not synchronously inside
    mouseReleaseEvent (so the event can fully unwind before a window/dialog opens)."""
    monkeypatch.setattr(W, "load_recent_projects",
                        lambda: [{"path": "X:/nope/proj.mta", "opened_at": time.time()}])
    win = W.WelcomeWindow()
    seen = []
    monkeypatch.setattr(win, "on_open_recent", lambda p, e: seen.append(p))
    win._reload_recents()
    row = win.recents_area.widget().findChildren(W._RecentRow)[0]

    _left_release(row)
    assert seen == [], "click must NOT fire synchronously in the event handler"
    for _ in range(5):
        qapp.processEvents()
    assert seen == ["X:/nope/proj.mta"], "click should fire on the next tick"
    win.close()


def test_settings_language_switch(qapp, tmp_path, monkeypatch):
    """The welcome Settings → Language menu switches CN⇄EN: it persists to config
    and re-renders the menu + body live."""
    import app_config
    from PySide6.QtWidgets import QLabel, QMessageBox
    # isolate the real config.json; silence the info modal
    monkeypatch.setattr(app_config, "CONFIG_PATH", str(tmp_path / "config.json"))
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: None))
    from canvas_qt import i18n
    i18n.set_language("en")

    w = W.WelcomeWindow()
    menu_titles = lambda: [a.text() for a in w.menuBar().actions()]
    labels = lambda: {l.text() for l in w.findChildren(QLabel)}
    assert any("Settings" in t for t in menu_titles())
    assert "Start" in labels() and "Recent projects" in labels()

    w.on_set_language("zh")                       # switch to Simplified Chinese
    assert app_config.get_language() == "zh"      # persisted
    assert any("设置" in t for t in menu_titles())  # menu retranslated
    assert "开始" in labels() and "最近项目" in labels()  # body retranslated

    w.on_set_language("en")                        # and back
    assert app_config.get_language() == "en"
    assert "Start" in labels()


def test_welcome_llm_settings_persist(qapp, tmp_path, monkeypatch):
    """Regression: the welcome window's Settings → LLM Settings must SAVE to
    config.json on OK (so the canvas/Tool Generator pick up the same key). It
    previously exec()'d the dialog without calling save()."""
    import json
    from PySide6.QtWidgets import QDialog
    import app_config
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"api_key": "", "model": "m", "base_url": "u"}))
    monkeypatch.setattr(app_config, "CONFIG_PATH", str(cfg))
    w = W.WelcomeWindow()
    # simulate: user types a key, clicks OK
    monkeypatch.setattr(W.SettingsDialog, "exec",
                        lambda self: (self.key.setText("sk-xyz"), QDialog.Accepted)[1])
    w.on_llm_settings()
    assert json.loads(cfg.read_text())["api_key"] == "sk-xyz"
