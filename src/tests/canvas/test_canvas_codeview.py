"""'Check Code' — right-click a node to see the generated agent.py / config.json
with that node's contributed regions highlighted (VB-style code-behind view)."""

from __future__ import annotations

import os
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
pytest.importorskip("PySide6")

from PySide6.QtCore import QPoint  # noqa: E402
from PySide6.QtGui import QContextMenuEvent  # noqa: E402
from PySide6.QtWidgets import QMessageBox  # noqa: E402

import code_view  # noqa: E402
from canvas_qt.code_view_ui import CodeViewWindow, show_code_view  # noqa: E402


def test_node_context_menu_offers_check_code(win, monkeypatch):
    """Right-clicking a node builds a menu that includes a 'Check Code' action.

    QMenu.exec is a real modal that blocks headless; patching it at the class
    level does NOT stick in PySide6 (the C++ slot wins). Instead swap designer's
    module-global QMenu for a Python subclass whose exec() is a no-op — a Python
    override on a subclass DOES take precedence over the C++ method."""
    import canvas_qt.designer as dz
    win.insert_pattern("react")
    item = next(i for i in win.scene.node_items.values() if i.node.kind == "agent")
    captured = {}

    class _FakeMenu(dz.QMenu):
        def exec(self, *a):
            captured["menu"] = self
            return None

    monkeypatch.setattr(dz, "QMenu", _FakeMenu)
    monkeypatch.setattr(win.scene, "node_at_scene", lambda pt: item)
    ev = QContextMenuEvent(QContextMenuEvent.Mouse, QPoint(5, 5), QPoint(5, 5))
    win.view.contextMenuEvent(ev)
    labels = [a.text().replace("&", "") for a in captured["menu"].actions()]
    assert "Check Code" in labels, labels


def test_check_node_code_opens_viewer(win, monkeypatch):
    """check_node_code() opens a non-modal viewer, held on the window."""
    win.insert_pattern("react")
    node = next(n for n in win.graph.nodes.values() if n.kind == "agent")
    fake = {"files": {"agent.py": "AGENTS = {'x': 1}\n", "config.json": "{}\n"},
            "spans": {"agent.py": [(0, 9)], "config.json": []},
            "node": "x  (agent)", "note": ""}
    monkeypatch.setattr(code_view, "code_for_node", lambda g, nid: fake)
    win.check_node_code(node)
    held = getattr(win, "_codeview_windows", [])
    assert held and isinstance(held[-1], CodeViewWindow)
    held[-1].close()


def test_check_node_code_reports_generation_error(win, monkeypatch):
    """A graph that can't generate surfaces a warning, not a crash."""
    win.insert_pattern("react")
    node = next(n for n in win.graph.nodes.values() if n.kind == "agent")
    monkeypatch.setattr(code_view, "code_for_node",
                        lambda g, nid: {"error": "boom"})
    seen = {}
    monkeypatch.setattr(QMessageBox, "warning",
                        staticmethod(lambda *a, **k: seen.update(msg=a[-1])))
    win.check_node_code(node)
    assert seen.get("msg") == "boom"
    assert not getattr(win, "_codeview_windows", [])


def test_viewer_highlights_spans(qapp):
    """Each attributed span becomes one ExtraSelection on the shown file, and the
    editor text equals the generated file verbatim."""
    result = {
        "files": {"agent.py": "AGENTS = {'x': 1}\nrest of runtime\n",
                  "config.json": '{"llms": {}}\n'},
        "spans": {"agent.py": [(0, 9), (18, 22)], "config.json": []},
        "node": "x  (agent)", "note": "hi",
    }
    dlg = CodeViewWindow(result, None)
    files = {dlg._combo.itemData(i) for i in range(dlg._combo.count())}
    assert files == {"agent.py", "config.json"}
    for i in range(dlg._combo.count()):
        dlg._combo.setCurrentIndex(i)
        f = dlg._combo.currentData()
        assert dlg._editor.toPlainText() == result["files"][f]
        assert len(dlg._editor.extraSelections()) == len(result["spans"][f])
    dlg.deleteLater()


def test_show_code_view_holds_reference(win):
    """show_code_view keeps the dialog alive on the parent so it isn't GC'd."""
    result = {"files": {"agent.py": "x\n", "config.json": ""},
              "spans": {"agent.py": [], "config.json": []},
              "node": "n  (agent)", "note": ""}
    dlg = show_code_view(win, result)
    assert dlg in win._codeview_windows
    dlg.close()
