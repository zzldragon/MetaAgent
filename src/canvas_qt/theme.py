"""Lightweight dark-theme helper, split out of ``designer.py``.

Kept dependency-free (only PySide6) on purpose: the welcome launcher needs to
theme the app at startup but must NOT drag in the whole canvas designer +
code-generation backend (``designer``/``dialogs``/``codegen``/``graph_codegen``
and the large template modules). Importing those just to paint the home screen
is what made launch feel slow — especially on machines with on-access antivirus
that scans every source file as it is read. Those modules are now imported only
when a canvas is actually opened.
"""

from __future__ import annotations

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

# Canvas (scene/view) colors per theme, read by the designer for the background,
# grid, and the muted hint/status labels. The node cards themselves are pastel
# fills with dark text, so they read well on both themes and are not themed here.
CANVAS_COLORS = {
    "dark":  {"bg": "#1e1f24", "grid": "#2b2d34", "panel": "#26272e",
              "hint": "#9aa0a6", "status": "#b8bcc6"},
    "light": {"bg": "#ffffff", "grid": "#e3e5ec", "panel": "#f1f2f5",
              "hint": "#5f6470", "status": "#3a3f49"},
}
_current = ["dark"]


def current_theme() -> str:
    return _current[0]


def canvas_colors(name: str | None = None) -> dict:
    """Canvas color set for a theme (defaults to the current one)."""
    return CANVAS_COLORS.get(name or _current[0], CANVAS_COLORS["dark"])


def apply_theme(app: QApplication, name: str) -> None:
    """Apply a named theme ("dark" | "light") to the whole app palette and record
    it as current (so canvas_colors() follows). Unknown names fall back to dark."""
    name = name if name in CANVAS_COLORS else "dark"
    _current[0] = name
    (_apply_light if name == "light" else apply_dark_theme)(app)


def _set_palette(app: QApplication, *, base, panel, text, accent,
                 button, hi_text, placeholder) -> None:
    app.setStyle("Fusion")
    pal = QPalette()
    pal.setColor(QPalette.Window, QColor(panel))
    pal.setColor(QPalette.WindowText, QColor(text))
    pal.setColor(QPalette.Base, QColor(base))
    pal.setColor(QPalette.AlternateBase, QColor(panel))
    pal.setColor(QPalette.Text, QColor(text))
    pal.setColor(QPalette.Button, QColor(button))
    pal.setColor(QPalette.ButtonText, QColor(text))
    pal.setColor(QPalette.ToolTipBase, QColor(panel))
    pal.setColor(QPalette.ToolTipText, QColor(text))
    pal.setColor(QPalette.Highlight, QColor(accent))
    pal.setColor(QPalette.HighlightedText, QColor(hi_text))
    pal.setColor(QPalette.PlaceholderText, QColor(placeholder))
    app.setPalette(pal)


def apply_dark_theme(app: QApplication) -> None:
    """Studio-style dark theme so the whole UI (palette, dialogs, menus) matches
    the dark canvas — using a Fusion palette plus a light accent."""
    _current[0] = "dark"
    _set_palette(app, base="#1e1f24", panel="#26272e", text="#e6e6e6",
                 accent="#42A5F5", button="#2f3038", hi_text="#0d1117",
                 placeholder="#8a8f99")


def _apply_light(app: QApplication) -> None:
    """Bright theme: white canvas/base, black text, blue accent."""
    _current[0] = "light"
    _set_palette(app, base="#ffffff", panel="#f1f2f5", text="#1a1a1a",
                 accent="#1565C0", button="#e7e8ee", hi_text="#ffffff",
                 placeholder="#9aa0a6")
