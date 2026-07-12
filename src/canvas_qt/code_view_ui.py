"""'Check Code' viewer — shows the full generated code with the selected node's
contributed regions highlighted (see code_view.code_for_node for attribution)."""

from __future__ import annotations

from PySide6.QtGui import QColor, QFont, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QTextEdit,
    QVBoxLayout,
)

from canvas_qt.dialogs import make_dialog_resizable

# Semi-transparent amber so it reads on both light and dark themes (the underlying
# text colour shows through).
_HL = QColor(255, 205, 60, 90)


class CodeViewWindow(QDialog):
    """Read-only, resizable, non-modal viewer of the generated files with one
    node's character spans highlighted. `result` = code_view.code_for_node()."""

    def __init__(self, result, parent=None):
        # Parentless top-level: a parented QDialog is an OWNED window that Windows
        # keeps permanently above the canvas, so the canvas can't be raised in
        # front of it. With no Qt parent it stacks normally. show_code_view() holds
        # a reference so it isn't GC'd.
        super().__init__(None)
        self._files = result.get("files", {})
        self._spans = result.get("spans", {})
        self.setWindowTitle("Generated code — " + result.get("node", ""))
        self.resize(900, 660)
        self.setModal(False)
        make_dialog_resizable(self)

        v = QVBoxLayout(self)
        head = QLabel("Highlighted regions are this node's contribution to the "
                      "generated code (the rest is the shared runtime)."
                      + (("  —  " + result["note"]) if result.get("note") else ""))
        head.setWordWrap(True)
        head.setStyleSheet("padding:2px 0 6px 0;")
        v.addWidget(head)

        row = QHBoxLayout()
        row.addWidget(QLabel("File:"))
        self._combo = QComboBox()
        # files that actually have highlighted regions first
        for f in sorted(self._files, key=lambda x: (not self._spans.get(x), x)):
            n = len(self._spans.get(f, []))
            tag = f"  ({n} region{'s' if n != 1 else ''})" if n else "  (no regions)"
            self._combo.addItem(f + tag, f)
        self._combo.currentIndexChanged.connect(self._load)
        row.addWidget(self._combo)
        row.addStretch(1)
        v.addLayout(row)

        self._editor = QPlainTextEdit()
        self._editor.setReadOnly(True)
        self._editor.setLineWrapMode(QPlainTextEdit.NoWrap)
        self._editor.setFont(QFont("Consolas", 10))
        v.addWidget(self._editor, 1)

        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.close)
        bb.accepted.connect(self.close)
        v.addWidget(bb)

        self._load(0)

    def _load(self, _idx):
        f = self._combo.currentData()
        if f is None:
            return
        self._editor.setPlainText(self._files.get(f, ""))
        doc = self._editor.document()
        fmt = QTextCharFormat()
        fmt.setBackground(_HL)
        sels = []
        for (s, e) in self._spans.get(f, []):
            sel = QTextEdit.ExtraSelection()
            cur = QTextCursor(doc)
            cur.setPosition(s)
            cur.setPosition(e, QTextCursor.KeepAnchor)
            sel.cursor = cur
            sel.format = fmt
            sels.append(sel)
        self._editor.setExtraSelections(sels)
        spans = self._spans.get(f, [])
        if spans:                                   # scroll to the first region
            cur = QTextCursor(doc)
            cur.setPosition(spans[0][0])
            self._editor.setTextCursor(cur)
            self._editor.ensureCursorVisible()


def show_code_view(parent, result) -> CodeViewWindow:
    """Open the viewer non-modally, holding a reference so it isn't GC'd."""
    dlg = CodeViewWindow(result, parent)
    if parent is not None:
        held = getattr(parent, "_codeview_windows", None)
        if held is None:
            held = parent._codeview_windows = []
        held.append(dlg)
        dlg.finished.connect(lambda *_: held.remove(dlg) if dlg in held else None)
    dlg.show()
    dlg.raise_()
    return dlg
