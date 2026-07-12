"""Offscreen check: double-clicking a timeline row pops the full event detail."""
import os, sys
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.chdir(BASE)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QFontDatabase
app = QApplication.instance() or QApplication([])
# load a couple Windows TTFs so offscreen text isn't tofu (not required for logic)
for ttf in ("C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/consola.ttf"):
    if os.path.exists(ttf):
        QFontDatabase.addApplicationFont(ttf)

from canvas_qt.trace_panel import TracePanel, _full_detail, _describe

LONG = "Here is the plan: " + " ".join(f"step{i} do the thing" for i in range(80))
assert len(LONG) > 500

panel = TracePanel()
panel.on_trace({"kind": "run_start", "task": "encode a sentence", "ts": 1700000000})
panel.on_trace({"kind": "stage_end", "agent": "planner", "output": LONG,
                "ts": 1700000001})
panel.on_trace({"kind": "tool_call", "agent": "planner", "tool": "base64_encode",
                "args": {"text": "I have a dream " * 30}, "ts": 1700000002})

# the timeline cell IS truncated (the user's complaint)
detail_cell = panel.table.item(1, 3).text()
assert detail_cell.startswith("→ ")
assert len(detail_cell) <= 165, len(detail_cell)
assert LONG not in detail_cell                 # truncated in the table

# the stashed record is the full thing
rec = panel.table.item(1, 0).data(0x0100)      # Qt.UserRole
assert isinstance(rec, dict) and rec["output"] == LONG

# double-click handler builds an untruncated popup
panel._open_detail(1, 3)
assert len(panel._detail_dialogs) == 1
dlg = panel._detail_dialogs[0]
body = dlg.body.toPlainText()
assert LONG in body, "full output missing from popup"
assert "output:" in body
print("popup body length:", len(body), "vs truncated cell:", len(detail_cell))

# tool_call args render fully (as JSON) in the popup
panel._open_detail(2, 3)
body2 = panel._detail_dialogs[-1].body.toPlainText()
assert "base64_encode" in body2 and "I have a dream" in body2
assert "args:" in body2

# _full_detail is robust to an unknown kind (no special-case) — shows all fields
fd = _full_detail({"kind": "mystery", "agent": "x", "ts": 1, "blob": "z"*200})
assert "z"*200 in fd and "blob:" in fd

# closing removes the retained reference
dlg.close()
app.processEvents()
print("dialogs after close:", len(panel._detail_dialogs))
print("ALL CHECKS PASSED")
