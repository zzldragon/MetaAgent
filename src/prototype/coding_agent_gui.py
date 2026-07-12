"""Cursor-like IDE GUI for the CodingAgent — a custom gui.py for the GUI node.

Three panes, like an AI code editor:
  * LEFT   — a fast code search (ripgrep if present, else a built-in walker) + a
             folder tree to browse and open files.
  * CENTER — a read-only code viewer (light syntax colouring) to check code.
  * RIGHT  — a chat with the agent (streamed answer + live tool trace).

The agent edits code through its high-risk tools (write_file / edit_file /
move_path / delete_path / run_shell). Each such call is intercepted by a HITL
confirm handler that shows a coloured OLD→NEW **diff** and asks permission before
anything touches disk. A toolbar **Mode** toggle flips between:
    HITL  — every change is confirmed with a diff (default), and
    Auto  — changes are applied without prompting (HITL ignored).

Drives the generated agent ONLY via the documented contract (import agent as
core; core.run(..., emit=, on_token=) on a worker thread; core.request_cancel();
core.set_confirm_handler(...)). See prototype/custom_gui/CONTRACT.md.
"""

import copy
import difflib
import html
import os
import shutil
import subprocess
import sys
import threading

# Make `import agent` work even under a Python started with -P / PYTHONSAFEPATH
# (which drops the script's own dir from sys.path). Must precede `import agent`.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtCore import QDir, QEvent, QRect, QSize, Qt, QModelIndex, Signal
from PySide6.QtGui import (QAction, QColor, QFont, QPainter, QSyntaxHighlighter,
                           QTextCharFormat, QTextCursor, QTextFormat)
try:                                        # QFileSystemModel moved between modules
    from PySide6.QtWidgets import QFileSystemModel
except ImportError:                         # across Qt6 point releases
    from PySide6.QtGui import QFileSystemModel
from PySide6.QtWidgets import (QApplication, QCheckBox, QColorDialog, QComboBox,
                               QDialog, QDialogButtonBox, QFontComboBox, QFormLayout,
                               QFrame, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QMainWindow, QMenu,
                               QMessageBox, QPlainTextEdit, QPushButton, QSpinBox,
                               QSplitter, QTabWidget, QTextEdit, QTreeView, QVBoxLayout,
                               QWidget)

core = None  # the generated agent, imported lazily off the UI thread

# ── dark "editor" palette ────────────────────────────────────────────────────
BG = "#1e1e1e"; PANEL = "#252526"; PANEL2 = "#2d2d30"; INK = "#d4d4d4"
MUTED = "#858585"; ACCENT = "#0e639c"; ACCENT_HI = "#1177bb"; BORDER = "#3c3c3c"
ADD_BG = "#14351f"; ADD_FG = "#6ac46a"; DEL_BG = "#3a1417"; DEL_FG = "#e06c75"
HUNK = "#4a9eff"

# ── appearance prefs (fonts + background colours) persisted beside gui.py ─────
_HERE = os.path.dirname(os.path.abspath(__file__))
_PREFS_PATH = os.path.join(_HERE, "ide_prefs.json")
_DEFAULT_PREFS = {"editor": {"font": "Consolas", "size": 11, "bg": BG},
                  "agent": {"font": "Segoe UI", "size": 13, "bg": PANEL}}


def _load_prefs():
    import json
    prefs = {k: dict(v) for k, v in _DEFAULT_PREFS.items()}
    try:
        with open(_PREFS_PATH, encoding="utf-8") as f:
            saved = json.load(f)
        for pane in ("editor", "agent"):
            if isinstance(saved.get(pane), dict):
                prefs[pane].update({k: v for k, v in saved[pane].items()
                                    if k in ("font", "size", "bg")})
    except (OSError, ValueError):
        pass
    return prefs


def _save_prefs(prefs):
    import json
    try:
        with open(_PREFS_PATH, "w", encoding="utf-8") as f:
            json.dump(prefs, f, indent=2)
    except OSError:
        pass


def _contrast_fg(bg_hex):
    """Pick black or light-grey text so it stays readable on any chosen bg."""
    try:
        c = QColor(bg_hex)
        lum = 0.299 * c.red() + 0.587 * c.green() + 0.114 * c.blue()
        return "#1b1b1b" if lum > 150 else INK
    except Exception:  # noqa: BLE001
        return INK


# tools that MUTATE the codebase → intercepted for a diff + confirm
_MUTATORS = {"write_file", "edit_file", "move_path", "delete_path",
             "make_dir", "run_shell"}
_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", ".tox",
              "dist", "build", ".mypy_cache", ".pytest_cache", ".idea", ".vs"}
_CODE_EXT = {".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".c", ".h", ".cpp",
             ".cs", ".go", ".rs", ".rb", ".php", ".sh", ".json", ".yaml", ".yml",
             ".toml", ".md", ".txt", ".html", ".css", ".sql", ".xml"}


# ── fast code search: ripgrep if available, else a bounded Python walk ─────────
def grep_code(root, pattern, ignore_case=True, max_results=500):
    """Return [(path, line_no, col, match_len, text)] for `pattern` under `root`
    (col/match_len are 0-based char offsets of the FIRST match on the line, for
    precise jump-highlight). Uses `rg` (ripgrep — Cursor-fast) when on PATH, else a
    stdlib walk that skips the usual vendor/build dirs. Never raises."""
    import re
    try:                                   # to compute the match column on the line
        rx = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    except re.error:
        rx = None

    def _colmatch(text):
        if rx is None:
            return 0, 0
        m = rx.search(text)
        return (m.start(), len(m.group(0))) if m else (0, 0)

    rg = shutil.which("rg")
    if rg:
        cmd = [rg, "--line-number", "--no-heading", "--color", "never",
               "--max-count", "50", "-e", pattern, root]
        if ignore_case:
            cmd.insert(1, "-i")
        try:
            out = subprocess.run(cmd, capture_output=True, text=True,
                                 timeout=20, encoding="utf-8", errors="replace")
            hits = []
            for line in out.stdout.splitlines():
                # path:line:text  (Windows drive letters have a colon too)
                parts = line.split(":", 2 if os.name != "nt" else 3)
                if os.name == "nt" and len(parts) == 4:
                    fp = parts[0] + ":" + parts[1]; ln, tx = parts[2], parts[3]
                elif len(parts) >= 3:
                    fp, ln, tx = parts[0], parts[1], parts[2]
                else:
                    continue
                if ln.isdigit():
                    col, mlen = _colmatch(tx)
                    hits.append((fp, int(ln), col, mlen, tx.strip()[:300]))
                if len(hits) >= max_results:
                    break
            return hits
        except Exception:  # noqa: BLE001 — fall through to the Python walker
            pass
    if rx is None:
        return []
    hits = []
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for fn in files:
            if os.path.splitext(fn)[1].lower() not in _CODE_EXT:
                continue
            fp = os.path.join(base, fn)
            try:
                with open(fp, encoding="utf-8", errors="ignore") as f:
                    for i, line in enumerate(f, 1):
                        m = rx.search(line)
                        if m:
                            hits.append((fp, i, m.start(), len(m.group(0)),
                                         line.rstrip()[:300]))
                            if len(hits) >= max_results:
                                return hits
            except OSError:
                continue
    return hits


# ── minimal, language-agnostic syntax colouring for the viewer ───────────────
class _Highlighter(QSyntaxHighlighter):
    KEYWORDS = (" def class return if elif else for while try except finally with "
                "import from as pass break continue lambda yield global nonlocal "
                "function const let var new this async await export default "
                "public private static void int float string bool true false null "
                "None True False and or not in is raise assert ").split()

    def __init__(self, doc):
        super().__init__(doc)
        self._kw = QTextCharFormat(); self._kw.setForeground(QColor("#569cd6"))
        self._str = QTextCharFormat(); self._str.setForeground(QColor("#ce9178"))
        self._com = QTextCharFormat(); self._com.setForeground(QColor("#6a9955"))
        self._num = QTextCharFormat(); self._num.setForeground(QColor("#b5cea8"))
        import re
        self._re_kw = re.compile(r"\b(" + "|".join(self.KEYWORDS) + r")\b")
        self._re_str = re.compile(r"(\"[^\"]*\"|'[^']*')")
        self._re_num = re.compile(r"\b\d+(\.\d+)?\b")
        self._re_com = re.compile(r"(#.*$|//.*$)")

    def highlightBlock(self, text):
        for rx, fmt in ((self._re_num, self._num), (self._re_kw, self._kw),
                        (self._re_str, self._str), (self._re_com, self._com)):
            for m in rx.finditer(text):
                self.setFormat(m.start(), m.end() - m.start(), fmt)


def _unified_diff_html(old_text, new_text, path):
    """Coloured unified diff (added=green, removed=red, hunk=blue)."""
    old = old_text.splitlines(keepends=False)
    new = new_text.splitlines(keepends=False)
    diff = list(difflib.unified_diff(old, new, fromfile="a/" + path,
                                     tofile="b/" + path, lineterm=""))
    if not diff:
        return f"<span style='color:{MUTED}'>(no textual change)</span>"
    rows = []
    for ln in diff:
        esc = html.escape(ln)
        if ln.startswith("+++") or ln.startswith("---"):
            rows.append(f"<div style='color:{MUTED}'>{esc}</div>")
        elif ln.startswith("@@"):
            rows.append(f"<div style='color:{HUNK}'>{esc}</div>")
        elif ln.startswith("+"):
            rows.append(f"<div style='background:{ADD_BG};color:{ADD_FG}'>{esc}</div>")
        elif ln.startswith("-"):
            rows.append(f"<div style='background:{DEL_BG};color:{DEL_FG}'>{esc}</div>")
        else:
            rows.append(f"<div style='color:{INK}'>{esc}</div>")
    return ("<pre style='font-family:Consolas,\"Courier New\",monospace;"
            "font-size:12px;margin:0'>" + "".join(rows) + "</pre>")


def _aligned_diff(old_text, new_text):
    """Row-align old vs new for a side-by-side view. Returns (left, right); each is
    a list of (line_no|None, text, kind) with kind in eq|del|add|pad."""
    old = old_text.splitlines(); new = new_text.splitlines()
    sm = difflib.SequenceMatcher(None, old, new)
    L, R = [], []
    ln_l = ln_r = 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                ln_l += 1; ln_r += 1
                L.append((ln_l, old[i1 + k], "eq")); R.append((ln_r, new[j1 + k], "eq"))
        elif tag == "delete":
            for k in range(i1, i2):
                ln_l += 1; L.append((ln_l, old[k], "del")); R.append((None, "", "pad"))
        elif tag == "insert":
            for k in range(j1, j2):
                ln_r += 1; L.append((None, "", "pad")); R.append((ln_r, new[k], "add"))
        elif tag == "replace":
            dl, dr = i2 - i1, j2 - j1
            for k in range(max(dl, dr)):
                if k < dl:
                    ln_l += 1; L.append((ln_l, old[i1 + k], "del"))
                else:
                    L.append((None, "", "pad"))
                if k < dr:
                    ln_r += 1; R.append((ln_r, new[j1 + k], "add"))
                else:
                    R.append((None, "", "pad"))
    return L, R


def _side_html(rows):
    bg = {"del": DEL_BG, "add": ADD_BG, "eq": "", "pad": "#181818"}
    fg = {"del": DEL_FG, "add": ADD_FG, "eq": INK, "pad": MUTED}
    out = []
    for ln, text, kind in rows:
        style = "color:%s;" % fg[kind]
        if bg[kind]:
            style += "background:%s;" % bg[kind]
        num = "%4s " % (ln if ln else "")
        out.append("<div style='%swhite-space:pre'><span style='color:%s'>%s</span>%s</div>"
                   % (style, MUTED, num, html.escape(text) or "&nbsp;"))
    return ("<div style='font-family:Consolas,\"Courier New\",monospace;font-size:12px'>"
            + "".join(out) + "</div>")


class _SideBySide(QWidget):
    """Two synced read-only panes: OLD (left, deletions red) | NEW (right, adds green)."""

    def __init__(self, old_text, new_text, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self); lay.setContentsMargins(0, 0, 0, 0); lay.setSpacing(4)
        self.left = QTextEdit(); self.right = QTextEdit()
        for e in (self.left, self.right):
            e.setReadOnly(True); e.setObjectName("diffbody")
            e.setLineWrapMode(QTextEdit.NoWrap)
        L, R = _aligned_diff(old_text, new_text)
        self.left.setHtml(_side_html(L)); self.right.setHtml(_side_html(R))
        lay.addWidget(self.left); lay.addWidget(self.right)
        self._syncing = False
        self.left.verticalScrollBar().valueChanged.connect(
            lambda v: self._sync(self.right, v))
        self.right.verticalScrollBar().valueChanged.connect(
            lambda v: self._sync(self.left, v))

    def _sync(self, other, v):
        if self._syncing:
            return
        self._syncing = True
        other.verticalScrollBar().setValue(v)
        self._syncing = False


class _LineNumberArea(QWidget):
    def __init__(self, editor):
        super().__init__(editor); self._e = editor

    def sizeHint(self):
        return QSize(self._e.line_number_area_width(), 0)

    def paintEvent(self, event):
        self._e.line_number_area_paint(event)


class CodeEditor(QPlainTextEdit):
    """An EDITABLE code view with a line-number gutter — the user can type here and
    save (Ctrl+S). Standard QPlainTextEdit line-number pattern."""

    def __init__(self):
        super().__init__()
        self._lna = _LineNumberArea(self)
        self.blockCountChanged.connect(lambda _: self._update_width())
        self.updateRequest.connect(self._update_area)
        self._update_width()

    def line_number_area_width(self):
        digits = max(3, len(str(max(1, self.blockCount()))))
        return 14 + self.fontMetrics().horizontalAdvance("9") * digits

    def _update_width(self):
        self.setViewportMargins(self.line_number_area_width(), 0, 0, 0)

    def _update_area(self, rect, dy):
        if dy:
            self._lna.scroll(0, dy)
        else:
            self._lna.update(0, rect.y(), self._lna.width(), rect.height())
        if rect.contains(self.viewport().rect()):
            self._update_width()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        cr = self.contentsRect()
        self._lna.setGeometry(QRect(cr.left(), cr.top(),
                                    self.line_number_area_width(), cr.height()))

    def line_number_area_paint(self, event):
        painter = QPainter(self._lna)
        painter.fillRect(event.rect(), QColor(getattr(self, "_gutter_bg", PANEL2)))
        block = self.firstVisibleBlock(); num = block.blockNumber()
        top = self.blockBoundingGeometry(block).translated(self.contentOffset()).top()
        bottom = top + self.blockBoundingRect(block).height()
        painter.setPen(QColor(MUTED)); fh = self.fontMetrics().height()
        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                painter.drawText(0, int(top), self._lna.width() - 6, fh,
                                 Qt.AlignRight, str(num + 1))
            block = block.next()
            top = bottom; bottom = top + self.blockBoundingRect(block).height(); num += 1


class DiffDialog(QDialog):
    """Show what a mutating tool is about to do (as a diff / summary) and ask the
    user to Approve / Reject. Returns a confirm-handler decision dict."""

    def __init__(self, parent, tool, args, root):
        super().__init__(parent)
        self._decision = {"decision": "deny"}
        self.setWindowTitle("Confirm change · %s" % tool)
        self.resize(1000, 640)
        v = QVBoxLayout(self)
        head = QLabel(self._summary(tool, args, root)); head.setObjectName("diffhead")
        head.setWordWrap(True); v.addWidget(head)
        if tool in ("edit_file", "write_file"):
            old, new, path = self._content(tool, args, root)
            tabs = QTabWidget()
            tabs.addTab(_SideBySide(old, new), "Side-by-side")
            uni = QTextEdit(); uni.setReadOnly(True); uni.setObjectName("diffbody")
            uni.setLineWrapMode(QTextEdit.NoWrap)
            uni.setHtml(_unified_diff_html(old, new, path))
            tabs.addTab(uni, "Unified")
            v.addWidget(tabs, 1)
        else:
            body = QTextEdit(); body.setReadOnly(True); body.setObjectName("diffbody")
            body.setHtml(self._render(tool, args, root)); v.addWidget(body, 1)

        bar = QHBoxLayout()
        self.remember = QPushButton("Approve all this session")
        self.remember.setObjectName("approveall")
        self.remember.clicked.connect(lambda: self._finish("allow", remember=True))
        bar.addWidget(self.remember); bar.addStretch(1)
        bb = QDialogButtonBox()
        ok = bb.addButton("Approve", QDialogButtonBox.AcceptRole)
        ok.setObjectName("approve")
        no = bb.addButton("Reject", QDialogButtonBox.RejectRole)
        no.setObjectName("reject")
        ok.clicked.connect(lambda: self._finish("allow"))
        no.clicked.connect(lambda: self._finish("deny"))
        bar.addWidget(bb)
        v.addLayout(bar)

    def _resolve(self, path, root):
        if not path:
            return ""
        if os.path.isabs(path):
            return path
        for base in ([root] if root else []) + [os.getcwd()]:
            cand = os.path.join(base, path)
            if os.path.exists(cand):
                return cand
        return os.path.join(root or os.getcwd(), path)

    def _read(self, path):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                return f.read()
        except OSError:
            return ""

    def _summary(self, tool, args, root):
        if tool == "edit_file":
            return "✏  Edit  <b>%s</b>%s" % (args.get("path", "?"),
                   "  (replace all)" if args.get("replace_all") else "")
        if tool == "write_file":
            p = self._resolve(args.get("path", ""), root)
            return ("📝  %s  <b>%s</b>" % ("Overwrite" if os.path.isfile(p)
                    else "Create", args.get("path", "?")))
        if tool == "delete_path":
            return "🗑  Delete  <b>%s</b>  (irreversible)" % args.get("path", "?")
        if tool == "move_path":
            return "➡  Move  <b>%s</b> → <b>%s</b>" % (args.get("src", "?"),
                                                       args.get("dst", "?"))
        if tool == "make_dir":
            return "📁  Create directory  <b>%s</b>" % args.get("path", "?")
        if tool == "run_shell":
            return "⚙  Run shell command"
        return "Confirm tool: <b>%s</b>" % tool

    def _content(self, tool, args, root):
        """(old_text, new_text, display_path) for a file-content change."""
        p = self._resolve(args.get("path", ""), root)
        cur = self._read(p)
        if tool == "edit_file":
            old, new = args.get("old_string", ""), args.get("new_string", "")
            return cur, cur.replace(old, new, -1 if args.get("replace_all") else 1), args.get("path", "")
        return cur, args.get("content", ""), args.get("path", "")   # write_file

    def _render(self, tool, args, root):
        if tool == "edit_file":
            p = self._resolve(args.get("path", ""), root)
            cur = self._read(p)
            old, new = args.get("old_string", ""), args.get("new_string", "")
            proposed = cur.replace(old, new, -1 if args.get("replace_all") else 1)
            return _unified_diff_html(cur, proposed, args.get("path", ""))
        if tool == "write_file":
            p = self._resolve(args.get("path", ""), root)
            return _unified_diff_html(self._read(p), args.get("content", ""),
                                      args.get("path", ""))
        if tool == "delete_path":
            p = self._resolve(args.get("path", ""), root)
            preview = self._read(p)[:4000] if os.path.isfile(p) else "(directory tree)"
            return ("<pre style='color:%s'>%s</pre>" % (DEL_FG, html.escape(preview)))
        if tool == "run_shell":
            cmd = args.get("command", "")
            cwd = args.get("cwd", "") or "(workspace)"
            return ("<pre style='color:%s;font-family:Consolas,monospace'>$ %s\n\n"
                    "cwd: %s</pre>" % (INK, html.escape(cmd), html.escape(cwd)))
        return "<pre>%s</pre>" % html.escape(str(args))

    def _finish(self, decision, remember=False):
        self._decision = {"decision": decision}
        if remember:
            self._decision["remember"] = True
        self.accept() if decision == "allow" else self.reject()

    def outcome(self):
        return self._decision


class _ColorButton(QPushButton):
    """A button that shows and picks a background colour."""

    def __init__(self, color):
        super().__init__()
        self._color = color or BG
        self.setFixedWidth(110)
        self.clicked.connect(self._pick)
        self._refresh()

    def _pick(self):
        c = QColorDialog.getColor(QColor(self._color), self, "Background colour")
        if c.isValid():
            self._color = c.name(); self._refresh()

    def _refresh(self):
        self.setText(self._color)
        self.setStyleSheet("background:%s;color:%s;border:1px solid %s;"
                           "border-radius:4px;padding:5px;"
                           % (self._color, _contrast_fg(self._color), BORDER))

    def color(self):
        return self._color


class PreferencesDialog(QDialog):
    """Settings: Appearance (fonts + background for editor & agent) and Models
    (add / remove LLMs, pick the active one, switch hard vs fallback)."""

    def __init__(self, parent, prefs):
        super().__init__(parent)
        self.setWindowTitle("Settings"); self.resize(640, 480)
        v = QVBoxLayout(self)
        self.tabs = QTabWidget(); v.addWidget(self.tabs, 1)
        self._build_appearance(prefs)
        self._build_models()
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept); bb.rejected.connect(self.reject)
        v.addWidget(bb)

    # ── Appearance tab ──────────────────────────────────────────────────────
    def _build_appearance(self, prefs):
        w = QWidget(); form = QFormLayout(w)
        ed, ag = prefs["editor"], prefs["agent"]
        form.addRow(QLabel("<b>Editor</b>"))
        self.ed_font = QFontComboBox(); self.ed_font.setCurrentFont(QFont(ed["font"]))
        self.ed_size = QSpinBox(); self.ed_size.setRange(7, 40); self.ed_size.setValue(int(ed["size"]))
        self.ed_bg = _ColorButton(ed["bg"])
        form.addRow("Font:", self.ed_font); form.addRow("Size:", self.ed_size)
        form.addRow("Background:", self.ed_bg)
        form.addRow(QLabel("<b>Agent chat</b>"))
        self.ag_font = QFontComboBox(); self.ag_font.setCurrentFont(QFont(ag["font"]))
        self.ag_size = QSpinBox(); self.ag_size.setRange(7, 40); self.ag_size.setValue(int(ag["size"]))
        self.ag_bg = _ColorButton(ag["bg"])
        form.addRow("Font:", self.ag_font); form.addRow("Size:", self.ag_size)
        form.addRow("Background:", self.ag_bg)
        self.tabs.addTab(w, "Appearance")

    def result_prefs(self):
        return {"editor": {"font": self.ed_font.currentFont().family(),
                           "size": self.ed_size.value(), "bg": self.ed_bg.color()},
                "agent": {"font": self.ag_font.currentFont().family(),
                          "size": self.ag_size.value(), "bg": self.ag_bg.color()}}

    # ── Models tab ──────────────────────────────────────────────────────────
    def _build_models(self):
        w = QWidget(); lv = QVBoxLayout(w)
        if core is None or not hasattr(core, "save_config"):
            lv.addWidget(QLabel("Runtime not loaded yet — reopen Settings in a moment."))
            self.tabs.addTab(w, "Models"); return
        agents = list(getattr(core, "PIPELINE", []) or [])
        self._agent = agents[0] if agents else None
        row = QHBoxLayout(); row.addWidget(QLabel("Agent:"))
        self.agent_combo = QComboBox(); self.agent_combo.addItems(agents)
        self.agent_combo.currentTextChanged.connect(self._reload_models)
        row.addWidget(self.agent_combo, 1); lv.addLayout(row)
        self.model_list = QListWidget(); lv.addWidget(self.model_list, 1)
        btns = QHBoxLayout()
        for label, fn in (("Add…", self._add_model), ("Edit…", self._edit_model),
                          ("Remove", self._remove_model), ("Set active", self._set_active)):
            b = QPushButton(label); b.clicked.connect(fn); btns.addWidget(b)
        lv.addLayout(btns)
        self.manual = QCheckBox("Use ONLY the active model — a hard switch (unticked = "
                                "the other models are tried as fallback on error)")
        self.manual.toggled.connect(self._toggle_mode)
        lv.addWidget(self.manual)
        self.tabs.addTab(w, "Models")
        self._reload_models()

    def _cfgs(self):
        return (core.CONFIG.get("llms") or {}).get(self._agent) or []

    def _reload_models(self, *_):
        if self.agent_combo.currentText():
            self._agent = self.agent_combo.currentText()
        self.model_list.clear()
        active = core.get_llm_choice(self._agent)
        for i, c in enumerate(self._cfgs()):
            self.model_list.addItem("%s  %s / %s   [%s]" % (
                "● active" if i == active else "○", c.get("provider", "?"),
                c.get("model", "?"), "key set" if c.get("api_key") else "no key"))
        self.manual.blockSignals(True)
        self.manual.setChecked(core.get_llm_mode(self._agent) == "manual")
        self.manual.blockSignals(False)

    def _persist(self, new_cfg):
        core.save_config(new_cfg)
        if core.get_llm_choice(self._agent) >= len(self._cfgs()):
            core.set_llm_choice(self._agent, 0)
        self._reload_models()

    def _add_model(self):
        cfgs = self._cfgs()
        c = self._edit_model_dialog(dict(cfgs[0]) if cfgs else {}, "Add model")
        if c is None:
            return
        new = copy.deepcopy(core.CONFIG)
        new.setdefault("llms", {}).setdefault(self._agent, []).append(c)
        self._persist(new)

    def _edit_model(self):
        i = self.model_list.currentRow(); cfgs = self._cfgs()
        if not (0 <= i < len(cfgs)):
            return
        c = self._edit_model_dialog(dict(cfgs[i]), "Edit model")
        if c is None:
            return
        new = copy.deepcopy(core.CONFIG); new["llms"][self._agent][i] = c
        self._persist(new)

    def _remove_model(self):
        i = self.model_list.currentRow(); cfgs = self._cfgs()
        if not (0 <= i < len(cfgs)):
            return
        if len(cfgs) <= 1:
            QMessageBox.information(self, "Models", "Keep at least one model."); return
        new = copy.deepcopy(core.CONFIG); del new["llms"][self._agent][i]
        self._persist(new)

    def _set_active(self):
        i = self.model_list.currentRow()
        if i >= 0:
            core.set_llm_choice(self._agent, i); self._reload_models()

    def _toggle_mode(self, on):
        core.set_llm_mode(self._agent, "manual" if on else "fallback")

    def _edit_model_dialog(self, cfg, title):
        d = QDialog(self); d.setWindowTitle(title); d.resize(500, 220)
        form = QFormLayout(d)
        prov = QLineEdit(cfg.get("provider", "siliconflow"))
        model = QLineEdit(cfg.get("model", "")); model.setPlaceholderText("e.g. deepseek-ai/DeepSeek-V4-Flash")
        key = QLineEdit(cfg.get("api_key", "")); key.setPlaceholderText("sk-…")
        base = QLineEdit(cfg.get("base_url", "")); base.setPlaceholderText("blank = provider default")
        form.addRow("Provider:", prov); form.addRow("Model:", model)
        form.addRow("API key:", key); form.addRow("Base URL:", base)
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.accepted.connect(d.accept); bb.rejected.connect(d.reject); form.addRow(bb)
        if d.exec() != QDialog.Accepted:
            return None
        if not model.text().strip():
            QMessageBox.warning(self, "Model", "Model name is required."); return None
        out = copy.deepcopy(cfg)
        out.update(provider=prov.text().strip() or "siliconflow", model=model.text().strip(),
                   api_key=key.text().strip(), base_url=base.text().strip())
        return out


class _QuickOpen(QDialog):
    """Ctrl+P command palette — fuzzy-filter the workspace files and open one."""

    def __init__(self, parent, files):
        super().__init__(parent)
        self.setWindowTitle("Go to File"); self.resize(660, 460)
        self._files = files          # [(rel, full)]
        self._chosen = None
        v = QVBoxLayout(self)
        self.edit = QLineEdit(); self.edit.setPlaceholderText("Type to filter files…")
        self.list = QListWidget()
        v.addWidget(self.edit); v.addWidget(self.list, 1)
        self.edit.textChanged.connect(self._filter)
        self.edit.returnPressed.connect(self._accept_current)
        self.list.itemActivated.connect(lambda _it: self._accept_current())
        self.list.itemDoubleClicked.connect(lambda _it: self._accept_current())
        self.edit.installEventFilter(self)      # arrow keys drive the list
        self._filter("")

    def eventFilter(self, obj, ev):
        if obj is self.edit and ev.type() == QEvent.KeyPress and ev.key() in (Qt.Key_Down, Qt.Key_Up):
            row = self.list.currentRow() + (1 if ev.key() == Qt.Key_Down else -1)
            self.list.setCurrentRow(max(0, min(row, self.list.count() - 1)))
            return True
        return super().eventFilter(obj, ev)

    @staticmethod
    def _score(rel, t):
        """Subsequence fuzzy match; lower = better (contiguous substring wins)."""
        if not t:
            return 0
        r = rel.lower(); i = 0
        for ch in t:
            i = r.find(ch, i)
            if i < 0:
                return None
            i += 1
        return r.find(t) if t in r else 500

    def _filter(self, text):
        self.list.clear()
        t = text.lower().strip()
        scored = []
        for rel, full in self._files:
            s = self._score(rel, t)
            if s is not None:
                scored.append((s, len(rel), rel, full))
        scored.sort()
        for _s, _n, rel, full in scored[:300]:
            it = QListWidgetItem(rel); it.setData(Qt.UserRole, full)
            self.list.addItem(it)
        if self.list.count():
            self.list.setCurrentRow(0)

    def _accept_current(self):
        it = self.list.currentItem()
        if it:
            self._chosen = it.data(Qt.UserRole); self.accept()

    def chosen(self):
        return self._chosen


class CodingIDE(QMainWindow):
    # worker-thread → GUI-thread signals (never touch widgets off the GUI thread)
    _sig_append = Signal(str)
    _sig_token = Signal(str)
    _sig_status = Signal(str)
    _sig_done = Signal(str, str)          # (result, error)
    _sig_ready = Signal(bool, str)
    _sig_confirm = Signal(object)         # blocking HITL diff payload
    _sig_grep = Signal(object)            # grep results from a worker thread
    _sig_trace = Signal(object)           # structured trace records (set_trace_sink)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("CodingAgent · AI Code Editor")
        self.resize(1360, 860)
        self._root = os.getcwd()
        self._auto_mode = False
        self._running = False
        self._prefs = _load_prefs()    # fonts + background colours
        self._agent_rows = []          # (subagent_name, QListWidgetItem) for the Agents panel
        self._changes = []             # {tool, path, abspath, before} for the Changes review pane
        self._sig_append.connect(self._append)
        self._sig_token.connect(self._append_token)
        self._sig_status.connect(lambda s: self.statusBar().showMessage(s))
        self._sig_done.connect(self._finish_run)
        self._sig_ready.connect(self._on_ready)
        self._sig_confirm.connect(self._show_confirm)
        self._sig_grep.connect(self._show_grep)
        self._sig_trace.connect(self._on_trace_gui)
        self._build()
        self.setStyleSheet(_QSS)
        self._apply_prefs()
        threading.Thread(target=self._load_core, daemon=True).start()

    # ── layout ────────────────────────────────────────────────────────────
    def _build(self):
        self._build_menubar()
        self._build_toolbar()
        split = QSplitter(Qt.Horizontal)

        # LEFT — search + file tree
        left = QWidget(); lv = QVBoxLayout(left); lv.setContentsMargins(6, 6, 6, 6)
        lv.setSpacing(6)
        self.search = QLineEdit(); self.search.setObjectName("search")
        self.search.setPlaceholderText("🔎  Search code (regex)…  Enter")
        self.search.returnPressed.connect(self._on_search)
        lv.addWidget(self.search)
        self.results = QTreeView(); self.results.setObjectName("results")
        self.results.setHeaderHidden(True); self.results.setRootIsDecorated(False)
        from PySide6.QtGui import QStandardItemModel
        self._res_model = QStandardItemModel(); self.results.setModel(self._res_model)
        self.results.clicked.connect(self._on_result_click)
        lv.addWidget(self.results, 1)
        lv.addWidget(QLabel("EXPLORER"))
        self.fs_model = QFileSystemModel(); self.fs_model.setRootPath(self._root)
        self.tree = QTreeView(); self.tree.setObjectName("tree")
        self.tree.setModel(self.fs_model)
        self.tree.setRootIndex(self.fs_model.index(self._root))
        for c in range(1, 4):
            self.tree.hideColumn(c)
        self.tree.setHeaderHidden(True)
        self.tree.doubleClicked.connect(self._on_tree_open)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._tree_menu)
        lv.addWidget(self.tree, 2)
        split.addWidget(left)

        # CENTER — tabbed code editor (multiple open files)
        center = QWidget(); cv = QVBoxLayout(center); cv.setContentsMargins(6, 6, 6, 6)
        hdr = QHBoxLayout()
        self.file_label = QLabel("Open a file to edit its code"); self.file_label.setObjectName("filelabel")
        hdr.addWidget(self.file_label, 1)
        self.save_btn = QPushButton("Save"); self.save_btn.setObjectName("savebtn")
        self.save_btn.setEnabled(False); self.save_btn.clicked.connect(self._save_file)
        hdr.addWidget(self.save_btn)
        cv.addLayout(hdr)
        self.tabs = QTabWidget(); self.tabs.setObjectName("tabs")
        self.tabs.setTabsClosable(True); self.tabs.setMovable(True)
        self.tabs.tabCloseRequested.connect(self._close_tab)
        self.tabs.currentChanged.connect(lambda _i: self._update_header())
        cv.addWidget(self.tabs, 1)
        split.addWidget(center)

        # RIGHT — agents + plan panels (top) over the chat (bottom)
        right = QWidget(); rv = QVBoxLayout(right); rv.setContentsMargins(6, 6, 6, 6)
        rsplit = QSplitter(Qt.Vertical)
        panels = QTabWidget(); panels.setObjectName("panels")
        self.agents_list = QListWidget(); self.agents_list.setObjectName("agents")
        self.todo_list = QListWidget(); self.todo_list.setObjectName("todos")
        panels.addTab(self.agents_list, "Agents")
        panels.addTab(self.todo_list, "Plan")
        # Changes review tab — files the agent changed this session (accept/revert)
        changes_w = QWidget(); clv = QVBoxLayout(changes_w)
        clv.setContentsMargins(0, 0, 0, 0); clv.setSpacing(4)
        self.changes_list = QListWidget(); self.changes_list.setObjectName("changes")
        self.changes_list.itemDoubleClicked.connect(self._open_change)
        clv.addWidget(self.changes_list, 1)
        crow = QHBoxLayout()
        b_diff = QPushButton("View diff"); b_diff.clicked.connect(self._view_change_diff)
        b_rev = QPushButton("Revert"); b_rev.setObjectName("reject")
        b_rev.clicked.connect(self._revert_change)
        crow.addWidget(b_diff); crow.addWidget(b_rev); crow.addStretch(1)
        clv.addLayout(crow)
        panels.addTab(changes_w, "Changes")
        rsplit.addWidget(panels)
        chatw = QWidget(); cvl = QVBoxLayout(chatw); cvl.setContentsMargins(0, 0, 0, 0)
        cvl.addWidget(QLabel("AGENT"))
        self.chat = QTextEdit(); self.chat.setObjectName("chat"); self.chat.setReadOnly(True)
        cvl.addWidget(self.chat, 1)
        self.input = QPlainTextEdit(); self.input.setObjectName("input")
        self.input.setPlaceholderText("Ask the agent to read, search, or change code…  (Ctrl+Enter to send)")
        self.input.setFixedHeight(78)
        cvl.addWidget(self.input)
        row = QHBoxLayout()
        self.send_btn = QPushButton("Send"); self.send_btn.setObjectName("send")
        self.send_btn.setEnabled(False); self.send_btn.clicked.connect(self.on_send)
        self.stop_btn = QPushButton("Stop"); self.stop_btn.setObjectName("stop")
        self.stop_btn.setEnabled(False); self.stop_btn.clicked.connect(self.on_stop)
        row.addStretch(1); row.addWidget(self.send_btn); row.addWidget(self.stop_btn)
        cvl.addLayout(row)
        rsplit.addWidget(chatw)
        rsplit.setSizes([210, 520])
        rv.addWidget(rsplit)
        split.addWidget(right)

        split.setSizes([300, 640, 420])
        self.setCentralWidget(split)
        self.statusBar().showMessage("Loading runtime…")
        self._mode_label = QLabel(); self.statusBar().addPermanentWidget(self._mode_label)
        self._refresh_mode_label()

        # Ctrl+Enter sends; Ctrl+S saves; Ctrl+P quick-open; Ctrl+Shift+F project search
        from PySide6.QtGui import QShortcut, QKeySequence
        QShortcut(QKeySequence("Ctrl+Return"), self.input, activated=self.on_send)
        QShortcut(QKeySequence.Save, self, activated=self._save_file)
        QShortcut(QKeySequence("Ctrl+P"), self, activated=self._quick_open)
        QShortcut(QKeySequence("Ctrl+Shift+F"), self, activated=self._focus_search)

    def _build_menubar(self):
        mb = self.menuBar()
        m = mb.addMenu("&Settings")
        a_appear = QAction("&Appearance (fonts / colours)…", self)
        a_appear.triggered.connect(lambda: self._open_preferences(0))
        m.addAction(a_appear)
        a_models = QAction("&Models (add / switch LLMs)…", self)
        a_models.triggered.connect(lambda: self._open_preferences(1))
        m.addAction(a_models)
        m.addSeparator()
        a_reset = QAction("&Reset appearance to defaults", self)
        a_reset.triggered.connect(self._reset_prefs)
        m.addAction(a_reset)

    def _build_toolbar(self):
        tb = self.addToolBar("main"); tb.setMovable(False)
        a_open = QAction("📂 Open Folder", self); a_open.triggered.connect(self.on_open_folder)
        tb.addAction(a_open)
        tb.addSeparator()
        self.act_mode = QAction("Mode: HITL (confirm changes)", self)
        self.act_mode.setCheckable(True)
        self.act_mode.setToolTip("Toggle Auto mode — apply the agent's changes "
                                 "without asking (HITL ignored).")
        self.act_mode.toggled.connect(self._on_mode_toggle)
        tb.addAction(self.act_mode)
        tb.addSeparator()
        a_key = QAction("🔑 API Key…", self); a_key.triggered.connect(self.on_set_key)
        tb.addAction(a_key)

    # ── core lifecycle ──────────────────────────────────────────────────────
    def _load_core(self):
        global core
        try:
            import agent as c
            core = c
            self._sig_ready.emit(True, "")
        except Exception as e:  # noqa: BLE001
            self._sig_ready.emit(False, str(e))

    def _on_ready(self, ok, err):
        if not ok:
            self.chat.append("<span style='color:#e06c75'>Runtime failed to import: "
                             + html.escape(err) + "<br>Run: pip install -r requirements.txt</span>")
            self.statusBar().showMessage("Runtime failed")
            return
        if hasattr(core, "set_confirm_handler"):
            core.set_confirm_handler(self._confirm_tool)
        if hasattr(core, "set_trace_sink"):      # live agents + plan panels
            core.set_trace_sink(lambda rec: self._sig_trace.emit(rec))
        # root the tree at the agent's workspace if it has one
        try:
            ws = core.get_workspace() if hasattr(core, "get_workspace") else []
            if ws:
                self._set_root(ws[0])
        except Exception:  # noqa: BLE001
            pass
        self.send_btn.setEnabled(True)
        self.statusBar().showMessage("Ready.")
        if not self._has_key():
            self.chat.append("<span style='color:#d7a642'>⚠ No API key set — "
                             "click 🔑 API Key… before sending.</span>")

    # ── folder / files ──────────────────────────────────────────────────────
    def on_open_folder(self):
        from PySide6.QtWidgets import QFileDialog
        d = QFileDialog.getExistingDirectory(self, "Open Folder", self._root)
        if d:
            self._set_root(d)

    def _set_root(self, d):
        self._root = d
        self.fs_model.setRootPath(d)
        self.tree.setRootIndex(self.fs_model.index(d))
        self.setWindowTitle("CodingAgent · " + os.path.basename(d.rstrip("/\\")))
        if core is not None and hasattr(core, "set_workspace"):
            try:
                core.set_workspace([d])           # agent tools resolve paths here
            except Exception:  # noqa: BLE001
                pass

    def _on_tree_open(self, idx: QModelIndex):
        p = self.fs_model.filePath(idx)
        if os.path.isfile(p):
            self._open_file(p)

    # ── tabbed editor ─────────────────────────────────────────────────────────
    def _editors(self):
        return [self.tabs.widget(i) for i in range(self.tabs.count())]

    def _active_editor(self):
        w = self.tabs.currentWidget()
        return w if isinstance(w, CodeEditor) else None

    def _tab_for(self, path):
        ap = os.path.abspath(path)
        for ed in self._editors():
            if getattr(ed, "_path", None) and os.path.abspath(ed._path) == ap:
                return ed
        return None

    def _new_editor(self, path, text):
        ed = CodeEditor()
        ed._path = path; ed._dirty = False; ed._loading = True
        ed._hl = _Highlighter(ed.document())
        ed.setPlainText(text)
        ed._loading = False
        self._style_editor(ed)
        ed.textChanged.connect(lambda e=ed: self._on_edit(e))
        self.tabs.addTab(ed, os.path.basename(path))
        self.tabs.setCurrentWidget(ed)
        return ed

    def _open_file(self, path, goto_line=0, col=0, match_len=0):
        ed = self._tab_for(path)
        if ed is None:
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    text = f.read()
            except OSError as e:
                self.statusBar().showMessage("[cannot open] %s" % e); return
            ed = self._new_editor(path, text)
        else:
            self.tabs.setCurrentWidget(ed)
        self._update_header()
        if goto_line > 0:
            self._goto(ed, goto_line, col, match_len)

    def _goto(self, ed, line, col=0, match_len=0):
        """Place the caret on `line`, SELECT the matched substring (col..col+len),
        and full-width-highlight the line — Cursor-style jump-to-result."""
        block = ed.document().findBlockByLineNumber(max(0, line - 1))
        cur = QTextCursor(block)
        if match_len > 0:
            cur.setPosition(block.position() + col)
            cur.setPosition(block.position() + col + match_len, QTextCursor.KeepAnchor)
        ed.setTextCursor(cur)
        ed.centerCursor()
        sel = QTextEdit.ExtraSelection()
        sel.format.setBackground(QColor("#2f3a4a"))
        sel.format.setProperty(QTextFormat.FullWidthSelection, True)
        sel.cursor = QTextCursor(block)
        ed.setExtraSelections([sel])
        ed.setFocus()

    # ── user editing / save ─────────────────────────────────────────────────
    def _on_edit(self, ed):
        if getattr(ed, "_loading", False) or not getattr(ed, "_path", None):
            return
        if not ed._dirty:
            ed._dirty = True
            self._update_tab_title(ed)
            self._update_header()

    def _update_tab_title(self, ed):
        i = self.tabs.indexOf(ed)
        if i >= 0:
            self.tabs.setTabText(i, ("● " if ed._dirty else "") + os.path.basename(ed._path))

    def _update_header(self):
        ed = self._active_editor()
        if ed is None:
            self.file_label.setText("Open a file to edit its code")
            self.save_btn.setEnabled(False); return
        self.file_label.setText(("● " if ed._dirty else "") + ed._path)
        self.save_btn.setEnabled(ed._dirty)

    def _save_file(self):
        ed = self._active_editor()
        if ed is None or not ed._dirty:
            return True
        try:
            with open(ed._path, "w", encoding="utf-8", newline="") as f:
                f.write(ed.toPlainText())
        except OSError as e:
            QMessageBox.critical(self, "Save failed", str(e)); return False
        ed._dirty = False
        self._update_tab_title(ed); self._update_header()
        self.statusBar().showMessage("Saved %s" % ed._path)
        return True

    def _maybe_save(self, ed):
        """Ask about one editor's unsaved edits. Returns True to proceed, False = cancel."""
        if not getattr(ed, "_dirty", False):
            return True
        r = QMessageBox.question(
            self, "Unsaved changes", "Save changes to\n%s ?" % ed._path,
            QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel, QMessageBox.Save)
        if r == QMessageBox.Cancel:
            return False
        if r == QMessageBox.Save:
            self.tabs.setCurrentWidget(ed)
            return self._save_file()
        return True     # discard

    def _close_tab(self, i):
        ed = self.tabs.widget(i)
        if isinstance(ed, CodeEditor) and not self._maybe_save(ed):
            return
        self.tabs.removeTab(i)
        self._update_header()

    # ── command palette + file operations ────────────────────────────────────
    def _list_files(self, cap=8000):
        out = []
        for base, dirs, fns in os.walk(self._root):
            dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
            for fn in fns:
                full = os.path.join(base, fn)
                out.append((os.path.relpath(full, self._root).replace("\\", "/"), full))
                if len(out) >= cap:
                    return out
        return out

    def _quick_open(self):
        dlg = _QuickOpen(self, self._list_files())
        if dlg.exec() == QDialog.Accepted and dlg.chosen():
            self._open_file(dlg.chosen())

    def _focus_search(self):
        self.search.setFocus(); self.search.selectAll()

    def _tree_menu(self, pos):
        idx = self.tree.indexAt(pos)
        path = self.fs_model.filePath(idx) if idx.isValid() else self._root
        is_dir = os.path.isdir(path)
        target_dir = path if is_dir else os.path.dirname(path)
        menu = QMenu(self)
        menu.addAction("New File…", lambda: self._new_file(target_dir))
        menu.addAction("New Folder…", lambda: self._new_folder(target_dir))
        if idx.isValid():
            if not is_dir:
                menu.addAction("Open", lambda: self._open_file(path))
            menu.addSeparator()
            menu.addAction("Rename…", lambda: self._rename_path(path))
            menu.addAction("Delete", lambda: self._delete_path_ui(path))
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _new_file(self, folder):
        name, ok = QInputDialog.getText(self, "New File", "File name:")
        if not (ok and name.strip()):
            return
        p = os.path.join(folder, name.strip())
        if os.path.exists(p):
            QMessageBox.warning(self, "New File", "Already exists."); return
        try:
            os.makedirs(os.path.dirname(os.path.abspath(p)), exist_ok=True)
            open(p, "w", encoding="utf-8").close()
        except OSError as e:
            QMessageBox.critical(self, "New File", str(e)); return
        self._open_file(p)

    def _new_folder(self, folder):
        name, ok = QInputDialog.getText(self, "New Folder", "Folder name:")
        if not (ok and name.strip()):
            return
        try:
            os.makedirs(os.path.join(folder, name.strip()), exist_ok=True)
        except OSError as e:
            QMessageBox.critical(self, "New Folder", str(e))

    def _rename_path(self, path):
        name, ok = QInputDialog.getText(self, "Rename", "New name:", text=os.path.basename(path))
        if not (ok and name.strip()) or name.strip() == os.path.basename(path):
            return
        dst = os.path.join(os.path.dirname(path), name.strip())
        if os.path.exists(dst):
            QMessageBox.warning(self, "Rename", "Target already exists."); return
        try:
            os.rename(path, dst)
        except OSError as e:
            QMessageBox.critical(self, "Rename", str(e)); return
        ed = self._tab_for(path)                 # keep an open tab pointing at the new name
        if ed is not None:
            ed._path = dst; self._update_tab_title(ed); self._update_header()

    def _delete_path_ui(self, path):
        if QMessageBox.question(self, "Delete", "Delete\n%s ?\n\nThis cannot be undone." % path,
                                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        try:
            if os.path.isdir(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
        except OSError as e:
            QMessageBox.critical(self, "Delete", str(e)); return
        ed = self._tab_for(path)                 # close an open tab for a deleted file
        if ed is not None:
            i = self.tabs.indexOf(ed)
            if i >= 0:
                self.tabs.removeTab(i)
            self._update_header()

    # ── code search ─────────────────────────────────────────────────────────
    def _on_search(self):
        pat = self.search.text().strip()
        if not pat:
            return
        self._res_model.clear()
        self.statusBar().showMessage("Searching…")
        threading.Thread(target=lambda: self._sig_grep.emit(
            (pat, grep_code(self._root, pat))), daemon=True).start()

    def _show_grep(self, payload):
        from PySide6.QtGui import QStandardItem
        pat, hits = payload
        self._res_model.clear()
        for fp, ln, col, mlen, tx in hits:
            item = QStandardItem("%s:%d  %s" % (os.path.basename(fp), ln, tx))
            item.setData((fp, ln, col, mlen), Qt.UserRole); item.setEditable(False)
            item.setToolTip("%s:%d" % (fp, ln))
            self._res_model.appendRow(item)
        self.statusBar().showMessage("%d match(es) for /%s/%s" % (
            len(hits), pat, "" if shutil.which("rg") else "  (python — install ripgrep for speed)"))

    def _on_result_click(self, idx):
        data = self._res_model.itemFromIndex(idx).data(Qt.UserRole)
        if data:
            fp, ln, col, mlen = data
            self._open_file(fp, ln, col, mlen)     # jump to + highlight the exact match

    # ── live agents + plan panels (structured trace) ─────────────────────────
    def _on_trace_gui(self, rec):
        kind = rec.get("kind")
        if kind == "spawn":
            name = rec.get("subagent", "?")
            item = QListWidgetItem("▶  %s  —  %s" % (name, (rec.get("task") or "")[:70]))
            item.setForeground(QColor(HUNK))
            self.agents_list.addItem(item)
            self.agents_list.scrollToBottom()
            self._agent_rows.append((name, item))
        elif kind == "spawn_result":
            name = rec.get("subagent", "?")
            for n, item in reversed(self._agent_rows):     # mark the latest running one done
                if n == name and item.text().startswith("▶"):
                    item.setText("✓  " + item.text()[3:])
                    item.setForeground(QColor(ADD_FG))
                    break
        elif kind == "state":
            todos = (rec.get("updates") or {}).get("todos")
            if todos is None:
                todos = (rec.get("state") or {}).get("todos")
            if isinstance(todos, list):
                self._render_todos(todos)

    def _render_todos(self, todos):
        self.todo_list.clear()
        icon = {"completed": "✓", "in_progress": "◐", "pending": "○"}
        for t in todos:
            st = t.get("status", "pending")
            it = QListWidgetItem("%s  %s" % (icon.get(st, "○"), t.get("content", "")))
            it.setForeground(QColor(ADD_FG if st == "completed"
                                    else HUNK if st == "in_progress" else MUTED))
            self.todo_list.addItem(it)

    # ── mode / HITL ─────────────────────────────────────────────────────────
    def _on_mode_toggle(self, checked):
        self._auto_mode = checked
        self.act_mode.setText("Mode: AUTO (apply without asking)" if checked
                              else "Mode: HITL (confirm changes)")
        self._refresh_mode_label()

    def _refresh_mode_label(self):
        self._mode_label.setText("  ● AUTO — changes applied automatically  "
                                 if self._auto_mode else
                                 "  ● HITL — changes need approval  ")
        self._mode_label.setStyleSheet(
            "color:#e06c75;font-weight:600" if self._auto_mode else "color:#6ac46a;font-weight:600")

    def _confirm_tool(self, tool_name, args):
        # Auto mode: skip HITL, apply everything (but still log it for review/revert).
        if self._auto_mode:
            self._record_change(tool_name, args)
            return {"decision": "allow"}
        # Non-mutating high-risk tools (if any) also route here; show a generic diff.
        payload = {"tool": tool_name, "args": dict(args or {}),
                   "done": threading.Event(), "res": {"decision": "deny"}}
        self._sig_confirm.emit(payload)
        payload["done"].wait()
        if payload["res"].get("decision") == "allow":
            self._record_change(tool_name, args)
        return payload["res"]

    # ── changed-files review (Changes tab) ───────────────────────────────────
    def _resolve_path(self, path):
        if not path:
            return ""
        if os.path.isabs(path):
            return path
        for base in ([self._root] if self._root else []) + [os.getcwd()]:
            cand = os.path.join(base, path)
            if os.path.exists(cand):
                return cand
        return os.path.join(self._root or os.getcwd(), path)

    def _record_change(self, tool, args):
        """Snapshot a mutating change (called BEFORE the tool applies, on the worker
        thread) so the Changes pane can diff / revert it. Never raises."""
        if tool not in ("write_file", "edit_file", "delete_path", "move_path"):
            return
        try:
            if tool == "move_path":
                self._changes.append({"tool": tool, "abspath": None, "before": None,
                                      "path": "%s → %s" % (args.get("src", ""), args.get("dst", ""))})
                return
            p = args.get("path", ""); ap = self._resolve_path(p)
            before = None
            if os.path.isfile(ap):
                try:
                    with open(ap, encoding="utf-8", errors="replace") as f:
                        before = f.read()
                except OSError:
                    before = None
            self._changes.append({"tool": tool, "path": p, "abspath": ap, "before": before})
        except Exception:  # noqa: BLE001
            pass

    def _refresh_changes(self):
        self.changes_list.clear()
        icon = {"write_file": "📝", "edit_file": "✏", "delete_path": "🗑", "move_path": "➡"}
        for i, c in enumerate(self._changes):
            it = QListWidgetItem("%s  %s" % (icon.get(c["tool"], "•"), c["path"]))
            it.setData(Qt.UserRole, i)
            self.changes_list.addItem(it)

    def _sel_change(self):
        it = self.changes_list.currentItem()
        return self._changes[it.data(Qt.UserRole)] if it else None

    def _open_change(self, item):
        c = self._changes[item.data(Qt.UserRole)]
        if c.get("abspath") and os.path.isfile(c["abspath"]):
            self._open_file(c["abspath"])

    def _view_change_diff(self):
        c = self._sel_change()
        if not c or c.get("abspath") is None:
            QMessageBox.information(self, "Diff", "No text diff available for this change.")
            return
        now = ""
        if os.path.isfile(c["abspath"]):
            try:
                with open(c["abspath"], encoding="utf-8", errors="replace") as f:
                    now = f.read()
            except OSError:
                pass
        dlg = QDialog(self); dlg.setWindowTitle("Changed · %s" % c["path"]); dlg.resize(1000, 600)
        lay = QVBoxLayout(dlg)
        lay.addWidget(QLabel("before  →  after (current on disk)"))
        lay.addWidget(_SideBySide(c.get("before") or "", now), 1)
        bb = QDialogButtonBox(QDialogButtonBox.Close); bb.rejected.connect(dlg.reject)
        bb.accepted.connect(dlg.accept); lay.addWidget(bb)
        dlg.exec()

    def _revert_change(self):
        c = self._sel_change()
        if not c or c.get("abspath") is None:
            QMessageBox.information(self, "Revert", "This change can't be auto-reverted here.")
            return
        if QMessageBox.question(self, "Revert", "Restore the previous contents of\n%s ?" % c["path"],
                                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        try:
            if c.get("before") is None:            # created by the agent -> revert = delete
                if os.path.isfile(c["abspath"]):
                    os.remove(c["abspath"])
            else:
                os.makedirs(os.path.dirname(os.path.abspath(c["abspath"])), exist_ok=True)
                with open(c["abspath"], "w", encoding="utf-8", newline="") as f:
                    f.write(c["before"])
        except OSError as e:
            QMessageBox.critical(self, "Revert failed", str(e)); return
        ed = self._tab_for(c["abspath"])           # reflect the revert in an open tab
        if ed is not None:
            ed._loading = True
            ed.setPlainText(c.get("before") or "")
            ed._loading = False; ed._dirty = False
            self._update_tab_title(ed); self._update_header()
        self.statusBar().showMessage("Reverted %s" % c["path"])

    def _show_confirm(self, payload):
        try:
            dlg = DiffDialog(self, payload["tool"], payload["args"], self._root)
            dlg.exec()
            payload["res"] = dlg.outcome()
        finally:
            payload["done"].set()
            # refresh the viewer if the just-approved change touched the open file
            if payload["res"].get("decision") == "allow":
                self._sig_status.emit("Change approved: %s" % payload["tool"])

    # ── run ─────────────────────────────────────────────────────────────────
    def on_send(self):
        if self._running or core is None:
            return
        task = self.input.toPlainText().strip()
        if not task:
            return
        if not self._has_key():
            self.on_set_key(); return
        self.input.clear()
        self.agents_list.clear(); self.todo_list.clear(); self._agent_rows = []
        self.changes_list.clear(); self._changes = []
        self.chat.append("<br><b style='color:#4a9eff'>You</b><br>" + html.escape(task).replace("\n", "<br>"))
        self.chat.append("<b style='color:#6ac46a'>Agent</b>")
        self._running = True
        self.send_btn.setEnabled(False); self.stop_btn.setEnabled(True)
        self.statusBar().showMessage("Working…")
        threading.Thread(target=self._run, args=(task,), daemon=True).start()

    def _run(self, task):
        try:
            result = core.run(task,
                              emit=lambda s: self._sig_append.emit(str(s)),
                              on_token=lambda t: self._sig_token.emit(str(t)))
            self._sig_done.emit(result or "(no result)", "")
        except Exception as e:  # noqa: BLE001
            self._sig_done.emit("", "%s: %s" % (type(e).__name__, e))

    def on_stop(self):
        if core is not None:
            try:
                core.request_cancel()
            except Exception:  # noqa: BLE001
                pass
        self.statusBar().showMessage("Stopping…")

    def _append(self, line):
        # tool-trace lines shown dimmed
        self.chat.append("<span style='color:#858585'>" + html.escape(line) + "</span>")
        self.chat.moveCursor(QTextCursor.End)

    def _append_token(self, tok):
        self.chat.moveCursor(QTextCursor.End)
        self.chat.insertPlainText(tok)
        self.chat.moveCursor(QTextCursor.End)

    def _finish_run(self, result, err):
        self._running = False
        self.send_btn.setEnabled(True); self.stop_btn.setEnabled(False)
        if err:
            self.chat.append("<span style='color:#e06c75'>⚠ " + html.escape(err) + "</span>")
            self.statusBar().showMessage("Error")
        else:
            self.statusBar().showMessage("Done.")
        # the agent may have changed open files — reload each CLEAN tab whose file
        # changed on disk, but NEVER clobber a tab with unsaved user edits.
        warned = False
        for ed in self._editors():
            if not (ed._path and os.path.isfile(ed._path)):
                continue
            try:
                with open(ed._path, encoding="utf-8", errors="replace") as f:
                    disk = f.read()
            except OSError:
                continue
            if disk == ed.toPlainText():
                continue                          # unchanged
            if ed._dirty:
                warned = True
                continue                          # keep the user's edits
            ln = ed.textCursor().blockNumber() + 1
            ed._loading = True
            ed.setPlainText(disk)
            ed._loading = False
            self._goto(ed, ln)
        if warned:
            self.statusBar().showMessage("⚠ Some open files changed on disk but have "
                                         "unsaved edits — save or reopen to see the agent's version.")
        self._refresh_changes()      # populate the Changes review pane

    def closeEvent(self, event):
        for ed in self._editors():
            if not self._maybe_save(ed):
                event.ignore(); return
        event.accept()

    # ── appearance ──────────────────────────────────────────────────────────
    def _style_editor(self, ed):
        """Apply the editor pref (font + background) to one CodeEditor. Font goes IN
        the per-widget stylesheet — the app-level `QWidget{font}` rule cascades to
        children and would otherwise beat setFont(). setFont() is kept so the
        line-number gutter's fontMetrics matches the rendered font."""
        e = self._prefs["editor"]; efg = _contrast_fg(e["bg"])
        ed.setFont(QFont(e["font"], int(e["size"])))
        ed._gutter_bg = e["bg"]
        ed.setStyleSheet(
            "QPlainTextEdit{background:%s;color:%s;border:1px solid %s;border-radius:6px;"
            "padding:6px;selection-background-color:%s;font-family:'%s';font-size:%dpt;}"
            % (e["bg"], efg, BORDER, ACCENT, e["font"], int(e["size"])))
        ed.viewport().update()

    def _apply_prefs(self):
        ag = self._prefs["agent"]; afg = _contrast_fg(ag["bg"])
        for ed in self._editors():
            self._style_editor(ed)
        self.chat.setFont(QFont(ag["font"], int(ag["size"])))
        self.chat.setStyleSheet(
            "QTextEdit{background:%s;color:%s;border:1px solid %s;border-radius:6px;"
            "padding:6px;font-family:'%s';font-size:%dpt;}"
            % (ag["bg"], afg, BORDER, ag["font"], int(ag["size"])))

    def _open_preferences(self, tab=0):
        dlg = PreferencesDialog(self, self._prefs)
        dlg.tabs.setCurrentIndex(tab)
        if dlg.exec() != QDialog.Accepted:
            return
        self._prefs = dlg.result_prefs()
        _save_prefs(self._prefs)
        self._apply_prefs()
        self.statusBar().showMessage("Appearance saved.")

    def _reset_prefs(self):
        self._prefs = {k: dict(v) for k, v in _DEFAULT_PREFS.items()}
        _save_prefs(self._prefs)
        self._apply_prefs()
        self.statusBar().showMessage("Appearance reset to defaults.")

    # ── settings ────────────────────────────────────────────────────────────
    def _has_key(self):
        if core is None:
            return False
        llms = core.CONFIG.get("llms") or {}
        if isinstance(llms, dict) and llms:
            return any(c.get("api_key") for cfgs in llms.values() for c in (cfgs or []))
        return bool(core.CONFIG.get("api_key"))

    def on_set_key(self):
        if core is None or not hasattr(core, "save_config"):
            QMessageBox.information(self, "Settings", "Runtime not loaded yet."); return
        import copy
        llms = core.CONFIG.get("llms") or {}
        any_cfg = next((c for cfgs in llms.values() for c in (cfgs or [])), {})
        dlg = QDialog(self); dlg.setWindowTitle("API Key / Model"); dlg.resize(520, 200)
        from PySide6.QtWidgets import QFormLayout
        form = QFormLayout(dlg)
        key = QLineEdit(any_cfg.get("api_key", "")); key.setPlaceholderText("sk-…")
        base = QLineEdit(any_cfg.get("base_url", "")); base.setPlaceholderText("blank = provider default")
        model = QLineEdit(any_cfg.get("model", "")); model.setPlaceholderText("blank = unchanged")
        form.addRow("API key:", key); form.addRow("Base URL:", base); form.addRow("Model:", model)
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.accepted.connect(dlg.accept); bb.rejected.connect(dlg.reject); form.addRow(bb)
        if dlg.exec() != QDialog.Accepted:
            return
        new = copy.deepcopy(core.CONFIG)
        for _a, cfgs in (new.get("llms") or {}).items():
            for lc in (cfgs or []):
                lc["api_key"] = key.text().strip(); lc["base_url"] = base.text().strip()
                if model.text().strip():
                    lc["model"] = model.text().strip()
        try:
            core.save_config(new)
        except Exception:  # noqa: BLE001
            import traceback
            QMessageBox.critical(self, "Save failed", traceback.format_exc()[-1200:]); return
        self.statusBar().showMessage("Settings saved — used on the next run.")


_QSS = f"""
QMainWindow, QWidget {{ background: {BG}; color: {INK};
    font-family: 'Segoe UI','Microsoft YaHei',sans-serif; font-size: 13px; }}
QToolBar {{ background: {PANEL2}; border: none; spacing: 6px; padding: 4px; }}
QToolBar QToolButton {{ color: {INK}; padding: 5px 10px; border-radius: 5px; }}
QToolBar QToolButton:hover {{ background: {ACCENT}; }}
QToolBar QToolButton:checked {{ background: {DEL_BG}; color: {DEL_FG}; }}
QLabel {{ color: {MUTED}; font-size: 11px; font-weight: 600; letter-spacing: 1px; }}
#filelabel {{ color: {INK}; font-weight: 400; font-family: Consolas,monospace; }}
QLineEdit, QPlainTextEdit, QTextEdit {{ background: {PANEL}; color: {INK};
    border: 1px solid {BORDER}; border-radius: 6px; padding: 6px;
    selection-background-color: {ACCENT}; }}
QLineEdit:focus, QPlainTextEdit:focus {{ border: 1px solid {ACCENT_HI}; }}
QTreeView {{ background: {PANEL}; border: 1px solid {BORDER}; border-radius: 6px;
    outline: 0; }}
QTreeView::item {{ padding: 2px; }}
QTreeView::item:selected {{ background: {ACCENT}; color: #fff; }}
#savebtn {{ background: #2ea043; color: #fff; font-weight: 600; padding: 4px 16px;
    border: none; border-radius: 5px; }}
#savebtn:disabled {{ background: #37373d; color: {MUTED}; }}
#send {{ background: {ACCENT}; color: #fff; font-weight: 700; padding: 7px 20px;
    border: none; border-radius: 6px; }}
#send:hover {{ background: {ACCENT_HI}; }}
#send:disabled {{ background: #37373d; color: {MUTED}; }}
#stop {{ background: {PANEL2}; color: {INK}; padding: 7px 16px;
    border: 1px solid {BORDER}; border-radius: 6px; }}
#stop:disabled {{ color: {MUTED}; }}
#diffhead {{ color: {INK}; font-size: 14px; font-weight: 400; padding: 4px; }}
#diffbody {{ background: #141414; }}
#approve {{ background: #2ea043; color: #fff; font-weight: 700; padding: 6px 18px; border-radius: 6px; }}
#reject {{ background: #6e2730; color: #fff; padding: 6px 18px; border-radius: 6px; }}
#approveall {{ background: {PANEL2}; color: {INK}; padding: 6px 14px;
    border: 1px solid {BORDER}; border-radius: 6px; }}
QStatusBar {{ background: {PANEL2}; color: {MUTED}; }}
QTabWidget::pane {{ border: 1px solid {BORDER}; border-radius: 6px; top: -1px; }}
QTabBar::tab {{ background: {PANEL2}; color: {MUTED}; padding: 5px 12px;
    border: 1px solid {BORDER}; border-bottom: none;
    border-top-left-radius: 5px; border-top-right-radius: 5px; margin-right: 2px; }}
QTabBar::tab:selected {{ background: {PANEL}; color: {INK}; }}
QTabBar::close-button {{ subcontrol-position: right; }}
#agents, #todos, #changes {{ background: {PANEL}; color: {INK}; border: 1px solid {BORDER};
    border-radius: 6px; font-size: 12px; }}
#agents::item, #todos::item, #changes::item {{ padding: 3px 4px; }}
"""


def main():
    app = QApplication.instance() or QApplication(sys.argv)
    win = CodingIDE()
    win.show()
    app.exec()


if __name__ == "__main__":
    main()
