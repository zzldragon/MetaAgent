"""Qt (PySide6) welcome launcher — the app's home screen.

Replaces the wx WelcomeFrame. Because it is Qt, everything runs in one process:
"New project" / "Open" / "Open recent" open the canvas designer. The Tool
Generator (coding agent) now lives inside the canvas designer (Tools menu /
Tool node button), not here — opening a project is where tools get built.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from typing import TYPE_CHECKING

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from app_config import (
    BASE_DIR,
    add_recent_project,
    get_language,
    get_theme,
    load_config,
    load_recent_projects,
    remove_recent_project,
    save_config,
    set_language,
)
from .i18n import set_language as _i18n_set
from .i18n import t

# Only the lightweight theme helper is imported at startup. The canvas designer
# (and the code-generation backend it pulls in) is imported lazily in
# `open_canvas()`, so the welcome screen opens without loading any of it.
from .theme import apply_dark_theme, apply_theme  # noqa: F401  (re-exported)

if TYPE_CHECKING:  # for annotations only; not imported at runtime
    from .designer import CanvasWindow

GRAPHS_DIR = os.path.join(BASE_DIR, "graphs")

APP_VERSION = "1.0"
APP_AUTHOR = "Zheng Zhilong"
APP_AUTHOR_EMAIL = "348466951@qq.com"

ACCENT = "#1565C0"
ACCENT_HOVER = "#1E6FCB"
BG = "#1e1f24"
CARD_BG = "#26272e"
CARD_HOVER = "#2f3340"
TEXT = "#e6e6e6"
MUTED = "#9aa0a6"

_QSS = f"""
QMainWindow, QWidget#root {{ background: {BG}; }}
QLabel {{ color: {TEXT}; }}
QLabel#tagline {{ color: #C5DCF6; }}
QLabel#muted {{ color: {MUTED}; }}
QLabel#heading {{ color: {TEXT}; font-size: 15px; font-weight: bold; }}
QFrame#hero {{ background: {ACCENT}; }}
QLabel#logoTile {{ background: white; color: {ACCENT}; border-radius: 11px;
                   font-size: 24px; font-weight: bold; }}
QLabel#wordmark {{ color: white; font-size: 24px; font-weight: bold; }}
"""


def _relative_time(ts: float) -> str:
    if not ts:
        return ""
    delta = time.time() - ts
    if delta < 0:
        return ""
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)} min ago"
    if delta < 86400:
        return f"{int(delta // 3600)} hr ago"
    if delta < 7 * 86400:
        d = int(delta // 86400)
        return f"{d} day{'s' if d != 1 else ''} ago"
    return time.strftime("%Y-%m-%d", time.localtime(ts))


# ── settings dialog (Qt port of main_frame.SettingsDialog) ───────────────────
class SettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(t("Settings — LLM (Tool Generator & Estimation)"))
        # Force form labels to the theme's own contrast colour. Opened from the
        # welcome window, this dialog otherwise INHERITS the welcome's hard-coded
        # light QLabel colour while its background follows the app palette — so the
        # labels vanish (light-on-light). palette(window-text) always contrasts.
        self.setStyleSheet("QLabel { color: palette(window-text); }")
        cfg = load_config()
        v = QVBoxLayout(self)
        form = QFormLayout()
        self.key = QLineEdit(cfg.get("api_key", ""))
        self.key.setMinimumWidth(360)
        self.model = QLineEdit(cfg["model"])
        self.base_url = QLineEdit(cfg["base_url"])
        self.proxy = QLineEdit(cfg.get("proxy", ""))
        self.proxy.setPlaceholderText(
            "e.g. http://1.1.1.1:8080  —  blank = use system / env proxy")
        self.hitl = QCheckBox("ask before the agent saves tools")
        self.hitl.setChecked(bool(cfg.get("hitl_confirm", True)))
        # Provider preset (same routers as the canvas LLM node) — picking one
        # auto-fills Model + Base URL, e.g. NVIDIA build.nvidia.com or SiliconFlow.
        from canvas_qt.dialogs import PROVIDERS, PROVIDER_DEFAULTS
        self.provider = QComboBox()
        self.provider.addItems(PROVIDERS)
        _cur = cfg.get("provider") or next(
            (p for p, (m, b) in PROVIDER_DEFAULTS.items()
             if b and b == cfg.get("base_url", "")), "")
        if _cur:                                  # preselect WITHOUT auto-filling on open
            self.provider.blockSignals(True)
            self.provider.setCurrentText(_cur)
            self.provider.blockSignals(False)
        self.provider.currentTextChanged.connect(self._on_provider)
        form.addRow(t("Provider:"), self.provider)
        form.addRow(t("API key:"), self.key)
        form.addRow(t("Model:"), self.model)
        form.addRow(t("Base URL:"), self.base_url)
        form.addRow(t("Proxy (optional):"), self.proxy)
        form.addRow(t("HITL:"), self.hitl)
        v.addLayout(form)
        hint = QLabel(
            "Pick a Provider to auto-fill Model + Base URL (NVIDIA build.nvidia.com, "
            "SiliconFlow, OpenAI, DeepSeek, Gemini), then enter that provider's API "
            "key. Or set Base URL + Model by hand for any OpenAI-compatible endpoint "
            "or local server; the Anthropic API is also supported.")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color:{MUTED}; font-size:11px; padding:2px 0 4px 0;")
        v.addWidget(hint)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def _on_provider(self, name: str) -> None:
        """Auto-fill Model + Base URL from the chosen provider preset (user action
        only — init preselect is signal-blocked so it never clobbers a saved config)."""
        from canvas_qt.dialogs import PROVIDER_DEFAULTS
        if name in PROVIDER_DEFAULTS:
            model, base_url = PROVIDER_DEFAULTS[name]
            self.model.setText(model)
            self.base_url.setText(base_url)

    def save(self) -> None:
        cfg = load_config()
        cfg["provider"] = self.provider.currentText()
        cfg["api_key"] = self.key.text().strip()
        cfg["model"] = self.model.text().strip()
        cfg["base_url"] = self.base_url.text().strip()
        cfg["proxy"] = self.proxy.text().strip()
        cfg["hitl_confirm"] = self.hitl.isChecked()
        save_config(cfg)


# ── clickable widgets ────────────────────────────────────────────────────────
class _Card(QFrame):
    clicked = Signal()

    def __init__(self, title: str, subtitle: str, accent: bool = False):
        super().__init__()
        self.setCursor(Qt.PointingHandCursor)
        base = ACCENT if accent else CARD_BG
        hover = ACCENT_HOVER if accent else CARD_HOVER
        title_c = "white" if accent else TEXT
        sub_c = "#C5DCF6" if accent else MUTED
        self.setStyleSheet(
            f"_Card {{ background: {base}; border: 1px solid "
            f"{ACCENT if accent else '#34363f'}; border-radius: 12px; }}"
            f"_Card:hover {{ background: {hover}; border-color: {ACCENT}; }}")
        self.setFixedSize(316, 88)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 14, 18, 14)
        t = QLabel(title)
        t.setStyleSheet(f"color:{title_c}; font-size:14px; font-weight:bold; background:transparent;")
        s = QLabel(subtitle)
        s.setStyleSheet(f"color:{sub_c}; font-size:11px; background:transparent;")
        lay.addWidget(t)
        lay.addWidget(s)
        lay.addStretch(1)

    def mouseReleaseEvent(self, event):
        hit = (event.button() == Qt.LeftButton
               and self.rect().contains(event.position().toPoint()))
        super().mouseReleaseEvent(event)
        if hit:
            # Defer: the click opens a window / modal dialog whose nested event
            # loop can delete this widget while we're still inside its event
            # handler. Firing on the next tick lets the event fully unwind first.
            QTimer.singleShot(0, self.clicked.emit)


class _RecentRow(QFrame):
    clicked = Signal(str, bool)

    def __init__(self, path: str, opened_at: float):
        super().__init__()
        self.path = path
        self.exists = os.path.exists(path)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip(path)
        self.setStyleSheet(
            f"_RecentRow {{ background: {CARD_BG}; border: 1px solid #2f3038; "
            f"border-radius: 8px; }}"
            f"_RecentRow:hover {{ background: {CARD_HOVER}; border-color: {ACCENT}; }}")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 8, 14, 8)

        ext = os.path.splitext(path)[1].lower()
        chip = "MTA" if ext == ".mta" else "JSON" if ext == ".json" else (ext.lstrip(".").upper() or "?")
        chip_c = ACCENT if ext == ".mta" else "#C9A227"
        if not self.exists:
            chip_c = "#888"
        chip_lbl = QLabel(chip)
        chip_lbl.setStyleSheet(
            f"color:{chip_c}; border:1px solid {chip_c}; border-radius:6px; "
            f"padding:1px 6px; font-size:10px; font-weight:bold; background:transparent;")
        chip_lbl.setAlignment(Qt.AlignCenter)
        lay.addWidget(chip_lbl, 0, Qt.AlignVCenter)

        col = QVBoxLayout()
        col.setSpacing(1)
        name = os.path.basename(path) + ("   (missing)" if not self.exists else "")
        name_lbl = QLabel(name)
        name_lbl.setStyleSheet(
            f"color:{TEXT if self.exists else '#9aa0a6'}; font-size:12px; "
            f"font-weight:bold; background:transparent;")
        folder_lbl = QLabel(os.path.dirname(path))
        folder_lbl.setStyleSheet(f"color:{MUTED}; font-size:10px; background:transparent;")
        col.addWidget(name_lbl)
        col.addWidget(folder_lbl)
        lay.addLayout(col, 1)

        when = _relative_time(opened_at)
        if when:
            w = QLabel(when)
            w.setStyleSheet(f"color:{MUTED}; font-size:10px; background:transparent;")
            lay.addWidget(w, 0, Qt.AlignVCenter)

    def mouseReleaseEvent(self, event):
        left = event.button() == Qt.LeftButton
        super().mouseReleaseEvent(event)
        if left:
            # Defer: opening the project runs load_mta, which can pop a modal
            # (e.g. the tool-conflict dialog). Its nested event loop lets an
            # activation-change rebuild the recents list — deleting THIS row —
            # before this handler returns, so touching `self` afterwards would
            # crash (RuntimeError: C++ object already deleted). Fire next tick,
            # after the mouse event has fully unwound.
            path, exists = self.path, self.exists
            QTimer.singleShot(0, lambda: self.clicked.emit(path, exists))


# ── main window ──────────────────────────────────────────────────────────────
class WelcomeWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"MetaAgent v{APP_VERSION}")
        self.resize(880, 660)
        self.setStyleSheet(_QSS)
        self._designers: list[CanvasWindow] = []
        self._build_menu()
        self._build_ui()

    # ── menu ─────────────────────────────────────────────────────────────────
    def _build_menu(self) -> None:
        mb = self.menuBar()
        file_menu = mb.addMenu(t("&File"))
        a_new = file_menu.addAction(t("&New Project"))
        a_new.setShortcut("Ctrl+N")
        a_new.triggered.connect(self.on_new_project)
        a_open = file_menu.addAction(t("&Open Project..."))
        a_open.setShortcut("Ctrl+O")
        a_open.triggered.connect(self.on_open_project)
        file_menu.addSeparator()
        a_exit = file_menu.addAction(t("E&xit"))
        a_exit.triggered.connect(self.close)

        settings_menu = mb.addMenu(t("&Settings"))
        a_llm = settings_menu.addAction(t("&LLM Settings..."))
        a_llm.triggered.connect(self.on_llm_settings)
        lang_menu = settings_menu.addMenu(t("&Language"))
        cur = get_language()
        from PySide6.QtGui import QActionGroup
        grp = QActionGroup(self)
        grp.setExclusive(True)
        for code, label in (("en", "English"), ("zh", "简体中文")):
            act = lang_menu.addAction(t(label))
            act.setCheckable(True)
            act.setChecked(cur == code)
            grp.addAction(act)
            act.triggered.connect(lambda _checked=False, c=code: self.on_set_language(c))

    def on_llm_settings(self) -> None:
        from PySide6.QtWidgets import QDialog
        dlg = SettingsDialog(self)
        if dlg.exec() == QDialog.Accepted:
            dlg.save()          # persist to config.json so the canvas picks it up
            self.statusBar().showMessage("LLM settings saved.")

    def on_set_language(self, code: str) -> None:
        if code == get_language():
            return
        set_language(code)          # persist
        _i18n_set(code)             # apply to t()
        # rebuild menu + body live in the new language
        self.menuBar().clear()
        self._build_menu()
        self._build_ui()
        from PySide6.QtWidgets import QMessageBox
        QMessageBox.information(
            self, t("Language changed"),
            t("The canvas designer opens in the selected language. Some screens are "
              "still English-only for now."))

    # ── layout ───────────────────────────────────────────────────────────────
    def _build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._build_hero())

        body = QWidget()
        bl = QVBoxLayout(body)
        bl.setContentsMargins(24, 20, 24, 16)

        h1 = QLabel(t("Start"))
        h1.setObjectName("heading")
        bl.addWidget(h1)
        cards = QHBoxLayout()
        new_card = _Card(t("New project"), t("Open the canvas with an empty graph"), accent=True)
        new_card.clicked.connect(self.on_new_project)
        open_card = _Card(t("Open bundle"), t("Load a .mta bundle or .json graph"))
        open_card.clicked.connect(self.on_open_project)
        cards.addWidget(new_card)
        cards.addWidget(open_card)
        cards.addStretch(1)
        bl.addLayout(cards)
        bl.addSpacing(18)

        h2 = QLabel(t("Recent projects"))
        h2.setObjectName("heading")
        bl.addWidget(h2)
        self.recents_area = QScrollArea()
        self.recents_area.setWidgetResizable(True)
        self.recents_area.setFrameShape(QFrame.NoFrame)
        self.recents_area.setStyleSheet("background: transparent;")
        bl.addWidget(self.recents_area, 1)

        foot = QLabel(t("The coding agent that writes tools now lives in the canvas "
                        "designer — Tools → Tool Generator, or a Tool node's "
                        "“Create a new tool…” button."))
        foot.setObjectName("muted")
        foot.setStyleSheet(f"color:{MUTED}; font-size:10px;")
        bl.addWidget(foot)

        outer.addWidget(body, 1)
        self._reload_recents()

    def _build_hero(self) -> QWidget:
        hero = QFrame()
        hero.setObjectName("hero")
        hero.setFixedHeight(118)
        lay = QHBoxLayout(hero)
        lay.setContentsMargins(30, 0, 30, 0)
        lay.setSpacing(18)
        tile = QLabel("M")
        tile.setObjectName("logoTile")
        tile.setFixedSize(46, 46)
        tile.setAlignment(Qt.AlignCenter)
        lay.addWidget(tile, 0, Qt.AlignVCenter)
        col = QVBoxLayout()
        col.setSpacing(4)
        wm = QLabel("MetaAgent")
        wm.setObjectName("wordmark")
        tag = QLabel(t("Design, generate and run multi-agent systems — visually."))
        tag.setObjectName("tagline")
        col.addStretch(1)
        col.addWidget(wm)
        col.addWidget(tag)
        col.addStretch(1)
        lay.addLayout(col)
        lay.addStretch(1)
        # developer credit + version (top-right of the hero)
        credit = QLabel(
            f"Developed by {APP_AUTHOR}<br>"
            f"{APP_AUTHOR_EMAIL} &nbsp;·&nbsp; v{APP_VERSION}")
        credit.setObjectName("muted")
        credit.setTextFormat(Qt.RichText)
        credit.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        credit.setStyleSheet("font-size:11px; background:transparent;")
        lay.addWidget(credit, 0, Qt.AlignVCenter)
        return hero

    def _reload_recents(self) -> None:
        host = QWidget()
        v = QVBoxLayout(host)
        v.setContentsMargins(0, 4, 0, 0)
        v.setSpacing(6)
        items = load_recent_projects()
        if not items:
            empty = QLabel(t("No recent projects yet — start a new project or open "
                             "a bundle above."))
            empty.setStyleSheet(f"color:{MUTED}; padding:8px;")
            v.addWidget(empty)
        else:
            for it in items:
                row = _RecentRow(it["path"], it["opened_at"])
                row.clicked.connect(self.on_open_recent)
                v.addWidget(row)
        v.addStretch(1)
        # Replace the old rows via deleteLater (not setWidget's immediate delete):
        # this reload can be triggered (by an activation change) while a row is
        # still mid signal-emission from its own click, and freeing it now would
        # crash. Deferring the delete lets that stack unwind first.
        old = self.recents_area.takeWidget()
        self.recents_area.setWidget(host)
        if old is not None:
            old.deleteLater()

    # ── refresh recents when the launcher regains focus ─────────────────────
    def changeEvent(self, event):
        if event.type() == QEvent.ActivationChange and self.isActiveWindow():
            self._reload_recents()
        super().changeEvent(event)

    # ── actions ──────────────────────────────────────────────────────────────
    def open_canvas(self, open_path: str | None = None) -> None:
        # Imported here (not at module load) so launching the welcome screen
        # doesn't pull in the designer + code-generation backend.
        from .designer import CanvasWindow
        win = CanvasWindow(open_path=open_path)
        win.setAttribute(Qt.WA_DeleteOnClose, True)
        win.destroyed.connect(lambda *_: self._forget_designer(win))
        self._designers.append(win)
        win.show()
        win.raise_()
        win.activateWindow()

    def _forget_designer(self, win) -> None:
        if win in self._designers:
            self._designers.remove(win)

    def on_new_project(self) -> None:
        self.open_canvas()

    def on_open_project(self) -> None:
        os.makedirs(GRAPHS_DIR, exist_ok=True)
        path, _ = QFileDialog.getOpenFileName(
            self, "Open project (.mta bundle or .json graph)", GRAPHS_DIR,
            "Project (*.mta *.json);;MetaAgent bundle (*.mta);;Graph JSON (*.json)")
        if not path:
            return
        add_recent_project(path)
        self._reload_recents()
        self.open_canvas(open_path=path)

    def on_open_recent(self, path: str, exists: bool) -> None:
        if not exists:
            if QMessageBox.question(
                self, "File not found",
                f"This project is no longer at:\n{path}\n\nRemove it from the "
                "recent list?") == QMessageBox.Yes:
                remove_recent_project(path)
                self._reload_recents()
            return
        self.open_canvas(open_path=path)


# Qt's Windows plugin logs a benign "monitorData: Unable to obtain handle for
# monitor ... defaulting to 96 DPI" when it can't read a monitor's DPI (common
# over RDP / with some GPU drivers / on monitor hotplug). It safely falls back
# to 96 DPI, so we drop just that one line and pass every other Qt message
# through to whatever handler was already installed.
_QT_NOISE = ("Unable to obtain handle for monitor",)
_PREV_QT_HANDLER = None


def _qt_message_handler(mode, context, message):
    if any(noise in message for noise in _QT_NOISE):
        return
    if _PREV_QT_HANDLER is not None:
        _PREV_QT_HANDLER(mode, context, message)
    else:
        sys.stderr.write(str(message) + "\n")


def _install_qt_log_filter():
    """Silence the benign Windows DPI-probe warning (see _QT_NOISE). Installed
    before QApplication so it catches the startup/screen-setup messages."""
    from PySide6.QtCore import qInstallMessageHandler
    global _PREV_QT_HANDLER
    _PREV_QT_HANDLER = qInstallMessageHandler(_qt_message_handler)


def _prewarm_heavy_imports():
    """Import the slow, lazily-loaded modules on a background thread once the
    welcome window is up, so the first 'Open project' / 'Tool Generator' feels
    instant instead of stalling on a cold import.

    The OpenAI SDK (~3s) dominates the Tool Generator's first send; the designer
    pulls in the codegen backend (~90ms) for opening a project. Python serializes
    imports, so if the user clicks mid-prewarm the main thread just waits for that
    one module and continues with a warm cache. Pure module imports only — no Qt
    objects are created off the GUI thread."""
    def _work():
        for mod in ("openai", "canvas_qt.designer", "coding_agent"):
            try:
                __import__(mod)
            except Exception:
                pass        # pre-warming is best-effort; never crash startup
    threading.Thread(target=_work, name="prewarm-imports", daemon=True).start()


def app_icon():
    """The MetaAgent window/taskbar icon (assets/MetaAgent.ico), resolved for
    both a plain ``python main.py`` run and the frozen PyInstaller bundle
    (BASE_DIR points into the bundle, where assets/ is shipped via --add-data).
    Returns a null QIcon if the file is missing, so startup never fails on it."""
    from PySide6.QtGui import QIcon
    return QIcon(os.path.join(BASE_DIR, "assets", "MetaAgent.ico"))


def run():
    from PySide6.QtWidgets import QApplication
    _install_qt_log_filter()
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("MetaAgent")
    # Give the Windows taskbar its own AppUserModelID so it shows our icon
    # (and groups correctly) instead of borrowing the Python host's icon.
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "MetaAgent.VisualAgentDesigner")
        except Exception:
            pass
    app.setWindowIcon(app_icon())
    apply_theme(app, get_theme())
    _i18n_set(get_language())            # apply the saved UI language before building
    win = WelcomeWindow()
    win.show()
    _prewarm_heavy_imports()
    app.exec()


if __name__ == "__main__":
    run()
