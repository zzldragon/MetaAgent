"""Close/save prompts and the Qt welcome launcher."""

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



# ── close: offer to save unsaved work ────────────────────────────────────────
def test_close_clean_does_not_prompt(win, monkeypatch):
    """A clean (unchanged) canvas closes with no save prompt."""
    from PySide6.QtWidgets import QMessageBox
    asked = {"v": False}
    monkeypatch.setattr(QMessageBox, "exec",
                        lambda self: asked.update(v=True) or QMessageBox.Discard)
    assert not win._is_dirty()
    assert win.close() is True
    assert asked["v"] is False


def test_close_dirty_cancel_keeps_window_open(win, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    win.add_node("agent")                       # unsaved change
    assert win._is_dirty()
    monkeypatch.setattr(QMessageBox, "exec", lambda self: QMessageBox.Cancel)
    assert win.close() is False                 # Cancel aborts the close
    win._clean_snapshot = win._snapshot()        # keep fixture teardown quiet


def test_close_dirty_discard_closes(win, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    win.add_node("agent")
    monkeypatch.setattr(QMessageBox, "exec", lambda self: QMessageBox.Discard)
    assert win.close() is True
    win._clean_snapshot = win._snapshot()


def test_close_dirty_save_invokes_on_save(win, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    win.add_node("agent")
    monkeypatch.setattr(QMessageBox, "exec", lambda self: QMessageBox.Save)
    saved = {"v": False}
    monkeypatch.setattr(win, "on_save", lambda: saved.update(v=True) or True)
    assert win.close() is True
    assert saved["v"] is True
    win._clean_snapshot = win._snapshot()        # keep fixture teardown quiet


def test_close_save_cancelled_keeps_window_open(win, monkeypatch):
    """Backing out of the save dialog must not discard the work."""
    from PySide6.QtWidgets import QMessageBox
    win.add_node("agent")
    monkeypatch.setattr(QMessageBox, "exec", lambda self: QMessageBox.Save)
    monkeypatch.setattr(win, "on_save", lambda: False)   # user cancelled the save
    assert win.close() is False
    win._clean_snapshot = win._snapshot()


def test_on_save_returns_bool_and_marks_clean(win, monkeypatch, tmp_path):
    """on_save returns True and clears the dirty flag on success; False on cancel."""
    from PySide6.QtWidgets import QFileDialog
    win.add_node("agent")
    assert win._is_dirty()
    monkeypatch.setattr(QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **k: ("", "")))   # user cancels
    assert win.on_save() is False and win._is_dirty()
    target = str(tmp_path / "g.json")
    monkeypatch.setattr(QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **k: (target, "Graph JSON (*.json)")))
    assert win.on_save() is True
    assert not win._is_dirty() and os.path.exists(target)


def test_on_save_defaults_to_mta(win, monkeypatch, tmp_path):
    """.mta is the default target format: the dialog offers the .mta filter first and
    a no-extension name under that filter is saved as a self-contained .mta bundle."""
    from PySide6.QtWidgets import QFileDialog
    win.add_node("agent")
    seen = {}

    def _fake(parent, caption, directory, flt, *a, **k):
        seen["dir"] = directory
        seen["filter"] = flt
        seen["initial"] = a[0] if a else k.get("selectedFilter", "")
        # user accepts the suggested name WITHOUT typing an extension, .mta filter active
        return (str(tmp_path / "mybook"), "Self-contained bundle (*.mta)")

    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(_fake))
    assert win.on_save() is True
    # the .mta filter is listed first (default) and passed as the initial filter
    assert seen["filter"].startswith("Self-contained bundle (*.mta)"), seen["filter"]
    assert "*.mta" in seen.get("initial", ""), seen.get("initial")
    # the suggested filename defaulted to .mta, and the saved artifact is the bundle
    assert seen["dir"].endswith(".mta"), seen["dir"]
    assert os.path.exists(str(tmp_path / "mybook.mta"))


# ── Qt welcome launcher ──────────────────────────────────────────────────────
def test_welcome_opens_canvas_in_process(qapp):
    """The Qt welcome opens the designer in-process (no subprocess)."""
    from canvas_qt.welcome import WelcomeWindow

    w = WelcomeWindow()
    try:
        w.open_canvas()
        assert len(w._designers) == 1
        assert isinstance(w._designers[0], CanvasWindow)
        w._designers[0].close()
    finally:
        w.close()


def test_welcome_settings_dialog_saves(qapp, monkeypatch):
    from canvas_qt import welcome as W

    monkeypatch.setattr(W, "load_config", lambda: {
        "api_key": "", "model": "m", "base_url": "u",
        "hitl_confirm": True})
    saved = {}
    monkeypatch.setattr(W, "save_config", lambda cfg: saved.update(cfg))

    dlg = W.SettingsDialog()
    dlg.key.setText("sk-123")
    dlg.model.setText("gpt-x")
    dlg.hitl.setChecked(False)
    dlg.save()
    assert saved["api_key"] == "sk-123"        # provider-neutral (was deepseek_api_key)
    assert saved["model"] == "gpt-x"
    assert saved["hitl_confirm"] is False


def test_coding_agent_settings_offers_nvidia(qapp, monkeypatch):
    """The coding-agent Settings dialog offers NVIDIA build.nvidia.com as a provider
    preset (like SiliconFlow): picking it auto-fills the NIM base URL + a model, and
    the choice persists."""
    from canvas_qt import welcome as W

    monkeypatch.setattr(W, "load_config", lambda: {
        "api_key": "", "model": "m", "base_url": "u", "hitl_confirm": True})
    saved = {}
    monkeypatch.setattr(W, "save_config", lambda cfg: saved.update(cfg))

    dlg = W.SettingsDialog()
    dlg.provider.setCurrentText("nvidia")          # user action → auto-fill
    assert dlg.base_url.text() == "https://integrate.api.nvidia.com/v1"
    assert dlg.model.text() == "meta/llama-3.1-70b-instruct"
    dlg.key.setText("nvapi-xyz")
    dlg.save()
    assert saved["provider"] == "nvidia"
    assert saved["base_url"] == "https://integrate.api.nvidia.com/v1"
    assert saved["api_key"] == "nvapi-xyz"


def test_config_migrates_legacy_api_key(tmp_path, monkeypatch):
    """A config written with the old SiliconFlow-specific 'deepseek_api_key' is
    migrated to the provider-neutral 'api_key' (value carried over, old key dropped)."""
    import json

    import app_config as AC
    cfgp = tmp_path / "config.json"
    cfgp.write_text(json.dumps({"deepseek_api_key": "sk-legacy", "model": "m"}),
                    encoding="utf-8")
    monkeypatch.setattr(AC, "CONFIG_PATH", str(cfgp))
    merged = AC.load_config()
    assert merged["api_key"] == "sk-legacy"
    assert "deepseek_api_key" not in merged
    # migration persisted to disk (old key removed, new key written)
    on_disk = json.loads(cfgp.read_text(encoding="utf-8"))
    assert on_disk.get("api_key") == "sk-legacy" and "deepseek_api_key" not in on_disk
    # an explicit new api_key wins over a leftover legacy field
    cfgp.write_text(json.dumps({"deepseek_api_key": "sk-old", "api_key": "sk-new"}),
                    encoding="utf-8")
    assert AC.load_config()["api_key"] == "sk-new"


def test_window_title_shows_project_name(win, tmp_path, monkeypatch):
    """The canvas window title shows the current project/graph name (filename, or
    'Untitled') and a '•' when there are unsaved changes."""
    from PySide6.QtWidgets import QFileDialog

    # don't touch the real recents file during the test
    monkeypatch.setattr(_dz, "add_recent_project", lambda p: None)

    # fresh window: Untitled, clean (no dirty marker)
    assert "Untitled" in win.windowTitle()
    assert "Visual Agent Designer" in win.windowTitle()
    assert "•" not in win.windowTitle()

    # an edit marks it dirty (•)
    win.add_node("agent")
    assert "•" in win.windowTitle(), win.windowTitle()

    # save → title shows the file's name and the dirty marker clears
    p = tmp_path / "MyProject.json"
    monkeypatch.setattr(QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **k: (str(p), "Graph JSON (*.json)")))
    assert win.on_save() is True
    assert "MyProject" in win.windowTitle()
    assert "•" not in win.windowTitle(), win.windowTitle()

    # loading another file updates the title
    q = tmp_path / "OtherGraph.json"
    win.graph.save(str(q))
    assert win.load_path(str(q)) is True
    assert "OtherGraph" in win.windowTitle()


def test_welcome_no_longer_hosts_tool_generator(qapp):
    """The Tool Generator was moved to the canvas designer; the welcome launcher
    must no longer expose it (no handler, no menu action)."""
    from canvas_qt.welcome import WelcomeWindow

    w = WelcomeWindow()
    try:
        assert not hasattr(w, "on_open_tool_generator")
        assert not hasattr(w, "_tool_gen")
        # findChildren(QAction) introspects without QAction.menu() (whose getter
        # has an ownership quirk that can delete the submenu).
        texts = [a.text() for a in w.findChildren(QAction)]
        assert not any("Tool Generator" in t for t in texts)
    finally:
        w.close()


def test_load_preserves_custom_type_defs(win, tmp_path):
    """A graph with custom state types round-trips through save + load_path: the
    window's type_defs are restored so a field keeps its list[Type] type (not
    coerced to str)."""
    import graph_model as gm
    from graph_model import save_mta
    g = win.graph
    g.new_node("agent", 0, 0).name = "a"
    g.type_defs = {"Finding": {"schema": {"type": "object",
                                          "properties": {"id": {"type": "string"}}},
                               "merge": "merge_deep"}}
    g.state_schema = [{"name": "items", "type": "list[Finding]",
                       "reducer": "upsert_by_key", "merge_key": "id"}]
    p = str(tmp_path / "ct.mta")
    save_mta(g, p, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "tools"))
    assert win.load_path(p)
    assert set(win.graph.type_defs) == {"Finding"}, win.graph.type_defs
    sf = {f["name"]: f for f in gm.state_fields(win.graph, include_builtins=False)}
    assert sf["items"]["type"] == "list[Finding]", sf["items"]["type"]
    assert sf["items"]["reducer"] == "upsert_by_key" and sf["items"]["merge_key"] == "id"
