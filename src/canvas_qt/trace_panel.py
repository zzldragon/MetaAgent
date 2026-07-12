"""Run-trace side panel — a LangGraph-Studio-style live view of a debug run.

This is purely additive UI: it renders the structured trace records the
generated agents already emit (the same dicts fed to ``RuntimeOverlay`` and
written to ``traces/*.jsonl``) plus the per-node state ``RuntimeOverlay``
already accumulates. Nothing in the run/codegen backend is touched — the panel
is a passive consumer.

Two halves:
  * a scrolling **event timeline** (time · node · event · detail), and
  * a per-module **inspector** that shows the selected node's live status,
    step / tool-call counts, route, notes and captured output.

The designer feeds it on the GUI thread: ``on_trace(rec)`` for every record and
``show_node(node, overlay)`` whenever the canvas selection changes.
"""

from __future__ import annotations

import html
import json
import time

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from runtime_overlay import STATUS_COLOR

from .dialogs import subtitle
from .theme import canvas_colors

# Event-kind → accent color for the timeline's "Event" column. Falls back to the
# default text color for kinds not listed here.
_KIND_COLOR = {
    "run_start": "#42A5F5",
    "stage_start": "#42A5F5",
    "stage_end": STATUS_COLOR["done"],
    "llm_step": "#9aa0a6",
    "tool_call": STATUS_COLOR["running"],
    "tool_result": "#26A69A",
    "route": "#1565C0",
    "retry": STATUS_COLOR["running"],
    "failover": STATUS_COLOR["running"],
    "run_error": STATUS_COLOR["error"],
    "run_end": STATUS_COLOR["done"],
}

_MAX_ROWS = 1000   # cap the timeline so a long run can't grow it unbounded


def _describe(rec: dict) -> str:
    """One-line, human description of a trace record for the timeline."""
    kind = rec.get("kind", "")
    if kind == "run_start":
        return str(rec.get("task", "")).strip().replace("\n", " ")
    if kind == "stage_start":
        return "started"
    if kind == "stage_end":
        out = str(rec.get("output", "")).strip().replace("\n", " ")
        return ("→ " + out[:160]) if out else "done"
    if kind == "llm_step":
        return f"step {rec.get('step', '')}"
    if kind == "tool_call":
        tool = rec.get("tool", "")
        args = rec.get("args")
        extra = ""
        if isinstance(args, dict) and args:
            extra = " " + ", ".join(f"{k}={v}" for k, v in list(args.items())[:3])
        elif args:
            extra = " " + str(args)
        return f"call {tool}{extra}"[:160]
    if kind == "tool_result":
        res = str(rec.get("result", rec.get("output", ""))).strip().replace("\n", " ")
        return ("→ " + res[:160]) if res else "result"
    if kind == "route":
        return f"→ {rec.get('choice', '')}"
    if kind == "state":
        upd = rec.get("updates") or {}
        if upd:
            return ("set " + ", ".join(f"{k}={v}" for k, v in upd.items()))[:160]
        return "state updated"
    if kind == "condition":
        expr = rec.get("expr", "")
        choice = rec.get("choice", "")
        return (f"{expr} → {choice}" if expr else f"→ {choice}")[:160]
    if kind == "retry":
        return f"retry: {str(rec.get('error', ''))[:140]}"
    if kind == "failover":
        return f"failover → {rec.get('next_model', '')}"
    if kind == "run_error":
        return f"error: {str(rec.get('error', ''))[:200]}"
    if kind == "run_end":
        res = str(rec.get("result", "")).strip().replace("\n", " ")
        return ("result: " + res[:200]) if res else "finished"
    return ""


# Housekeeping keys shown in the popup header (or not worth repeating in the body).
_DETAIL_SKIP = {"ts", "trace_id", "kind", "agent", "router"}


def _full_detail(rec: dict) -> str:
    """The complete, *untruncated* payload of a trace record for the detail popup.

    Generic on purpose: it walks every field of the record (minus housekeeping
    keys) so even record kinds ``_describe`` doesn't special-case still show
    their full content. Long / multi-line / structured values render verbatim;
    dicts and lists are pretty-printed as JSON."""
    blocks = []
    for k, v in rec.items():
        if k in _DETAIL_SKIP:
            continue
        if isinstance(v, str):
            sval = v
        elif v is None or isinstance(v, (int, float, bool)):
            sval = str(v)
        else:
            try:
                sval = json.dumps(v, indent=2, ensure_ascii=False, default=str)
            except Exception:
                sval = str(v)
        if sval == "":
            continue
        if "\n" in sval or len(sval) > 72:
            blocks.append(f"{k}:\n{sval}")
        else:
            blocks.append(f"{k}: {sval}")
    return "\n\n".join(blocks) if blocks else "(no additional detail for this event)"


class _DetailDialog(QDialog):
    """Read-only, resizable, scrollable popup showing one event's full detail.

    Opened by double-clicking a timeline row — the answer to "the Detail column
    truncated my output". The text is selectable so it can be copied."""

    def __init__(self, header: str, body: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Event detail")
        self.setModal(False)          # non-modal: keep watching the run while reading
        self.resize(680, 460)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(8)

        head = QLabel(header)
        head.setTextFormat(Qt.RichText)
        head.setWordWrap(True)
        lay.addWidget(head)

        self.body = QPlainTextEdit()
        self.body.setReadOnly(True)
        self.body.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        self.body.setFont(QFont("Consolas", 9))
        self.body.setPlainText(body)
        lay.addWidget(self.body, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        copy_btn = buttons.addButton("Copy", QDialogButtonBox.ActionRole)
        copy_btn.clicked.connect(self._copy_all)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def _copy_all(self) -> None:
        self.body.selectAll()
        self.body.copy()
        cur = self.body.textCursor()
        cur.clearSelection()
        self.body.setTextCursor(cur)


class TracePanel(QWidget):
    """Live event timeline + per-module inspector for a debug run."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._overlay = None        # RuntimeOverlay for the current run (or None)
        self._sel_node = None       # the Node currently selected on the canvas
        self._detail_dialogs = []   # open event-detail popups (retain refs)
        self._muted = canvas_colors()["hint"]
        self.setMinimumWidth(240)

        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(6)
        self._root = root

        header = QHBoxLayout()
        title = QLabel("Run trace")
        tf = title.font()
        tf.setBold(True)
        title.setFont(tf)
        header.addWidget(title)
        header.addStretch(1)
        self.clear_btn = QPushButton("Clear")
        self.clear_btn.setToolTip("Clear the timeline (the next Debug Run also "
                                  "clears it automatically).")
        self.clear_btn.clicked.connect(self.clear)
        header.addWidget(self.clear_btn)
        root.addLayout(header)

        # Inspector (top) | timeline (bottom), user-resizable.
        split = QSplitter(Qt.Vertical)
        split.setChildrenCollapsible(False)
        split.setHandleWidth(6)

        insp_box = QGroupBox("Inspector")
        iv = QVBoxLayout(insp_box)
        iv.setContentsMargins(6, 6, 6, 6)
        self.insp_title = QLabel()
        self.insp_title.setTextFormat(Qt.RichText)
        self.insp_title.setWordWrap(True)
        iv.addWidget(self.insp_title)
        self.insp_body = QTextEdit()
        self.insp_body.setReadOnly(True)
        self.insp_body.setFont(QFont("Consolas", 9))
        iv.addWidget(self.insp_body, 1)
        split.addWidget(insp_box)

        tl_box = QGroupBox("Event timeline")
        tv = QVBoxLayout(tl_box)
        tv.setContentsMargins(6, 6, 6, 6)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Time", "Node", "Event", "Detail"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(False)
        self.table.setToolTip("Double-click a row to see the full, untruncated "
                              "event detail.")
        self.table.cellDoubleClicked.connect(self._open_detail)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.Stretch)
        tv.addWidget(self.table, 1)
        split.addWidget(tl_box)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 2)
        root.addWidget(split, 1)

        self.footer = QLabel("")
        self.footer.setWordWrap(True)
        root.addWidget(self.footer)

        self.restyle()
        self._refresh_inspector()

    # ── feeds from the designer ─────────────────────────────────────────────
    def set_overlay(self, overlay) -> None:
        """Point the inspector at the overlay for the current run."""
        self._overlay = overlay
        self._refresh_inspector()

    def show_node(self, node, overlay=None) -> None:
        """Show ``node`` in the inspector (None → run summary). Called whenever
        the canvas selection changes; a single selected node drills in."""
        self._sel_node = node
        if overlay is not None:
            self._overlay = overlay
        self._refresh_inspector()

    def on_trace(self, rec: dict) -> None:
        """Consume one trace record (after the overlay has already consumed it)."""
        kind = rec.get("kind", "")
        if kind == "run_start":
            self.clear()
            task = str(rec.get("task", "")).strip()
            self.footer.setText(("Running: " + task[:120]) if task else "Running…")
        self._append_row(rec)
        if kind == "run_end":
            self.footer.setText("Run finished.")
        elif kind == "run_error":
            self.footer.setText("Run failed: " + str(rec.get("error", ""))[:120])
        self._refresh_inspector()

    def clear(self) -> None:
        self.table.setRowCount(0)
        self.footer.setText("")

    # ── replay transport (#3) ────────────────────────────────────────────────
    def attach_replay_bar(self, bar: QWidget) -> None:
        """Host a replay transport bar just below the header (hidden until a
        replay starts). Kept here so the timeline + transport read as one panel."""
        self._replay_bar = bar
        self._root.insertWidget(1, bar)
        bar.setVisible(False)

    def show_replay_bar(self, on: bool) -> None:
        bar = getattr(self, "_replay_bar", None)
        if bar is not None:
            bar.setVisible(bool(on))

    # ── theme ────────────────────────────────────────────────────────────────
    def restyle(self) -> None:
        """Recolor the muted labels for the active theme. Standard widgets follow
        the app palette automatically; only the hint/footer colors are manual."""
        c = canvas_colors()
        self._muted = c["hint"]
        self.footer.setStyleSheet(f"color:{c['status']};")
        self._refresh_inspector()

    # ── internals ────────────────────────────────────────────────────────────
    def _append_row(self, rec: dict) -> None:
        kind = rec.get("kind", "")
        name = rec.get("agent") or rec.get("router") or ""
        ts = rec.get("ts")
        tstr = (time.strftime("%H:%M:%S", time.localtime(ts))
                if isinstance(ts, (int, float)) else "")
        cells = [tstr, str(name), kind, _describe(rec)]
        # Only follow the tail if the user is already at the bottom — captured
        # BEFORE inserting so we don't yank them back while they read earlier rows.
        sb = self.table.verticalScrollBar()
        at_bottom = sb.value() >= sb.maximum() - 4
        r = self.table.rowCount()
        self.table.insertRow(r)
        for col, text in enumerate(cells):
            item = QTableWidgetItem(text)
            item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            if col == 2:
                color = _KIND_COLOR.get(kind)
                if color:
                    item.setForeground(QBrush(QColor(color)))
            if col == 0:
                # Stash the whole record so a double-click can show it untruncated.
                item.setData(Qt.UserRole, rec)
            self.table.setItem(r, col, item)
        while self.table.rowCount() > _MAX_ROWS:
            self.table.removeRow(0)
        if at_bottom:
            self.table.scrollToBottom()

    def _open_detail(self, row: int, _col: int) -> None:
        """Double-click handler: pop up the selected event's full detail.

        The Detail column truncates long values (outputs, tool results); this
        shows the complete record so nothing is lost."""
        anchor = self.table.item(row, 0)
        rec = anchor.data(Qt.UserRole) if anchor is not None else None
        if not isinstance(rec, dict):
            return
        kind = rec.get("kind", "")
        name = rec.get("agent") or rec.get("router") or ""
        ts = rec.get("ts")
        tstr = (time.strftime("%H:%M:%S", time.localtime(ts))
                if isinstance(ts, (int, float)) else "")
        bits = [b for b in (html.escape(tstr),
                            html.escape(str(name)),
                            html.escape(str(kind))) if b]
        header = "<b>" + "</b> &middot; <b>".join(bits) + "</b>" if bits else "<b>Event</b>"
        dlg = _DetailDialog(header, _full_detail(rec), self)
        dlg.setAttribute(Qt.WA_DeleteOnClose)
        self._detail_dialogs.append(dlg)
        dlg.finished.connect(lambda _=0, d=dlg: self._detail_dialogs.remove(d)
                             if d in self._detail_dialogs else None)
        dlg.show()
        dlg.raise_()

    def _refresh_inspector(self) -> None:
        node = self._sel_node
        if node is None:
            self.insp_title.setText("<b>Run summary</b>")
            self.insp_body.setPlainText(self._summary_text())
            return
        ov = self._overlay
        name = node.name
        status = ov.status_of(name) if ov is not None else "idle"
        color = STATUS_COLOR.get(status, self._muted)
        self.insp_title.setText(
            f"<b>{html.escape(str(name))}</b>"
            f'&nbsp;&nbsp;<span style="color:{color}">&#9679; {status}</span>')

        lines = [f"kind: {node.kind}"]
        sub = subtitle(node)
        if sub:
            lines.append(sub)
        n = ov.nodes.get(name) if ov is not None else None
        if n is not None:
            if n["step"]:
                lines.append(f"step: {n['step']}")
            if n["tool_calls"]:
                last = f"  (last: {n['last_tool']})" if n["last_tool"] else ""
                lines.append(f"tool calls: {n['tool_calls']}{last}")
            if n["route"]:
                lines.append(f"route → {n['route']}")
            if n["note"]:
                lines.append(f"note: {n['note']}")
            if n["output"]:
                lines.append("")
                lines.append("output:")
                lines.append(str(n["output"]))
        else:
            lines.append("")
            lines.append("(no run data yet — start a Debug Run)")
        self.insp_body.setPlainText("\n".join(lines))

    def _summary_text(self) -> str:
        ov = self._overlay
        if ov is None:
            return ("Select a module to inspect it.\n\n"
                    "Run a Debug Run (Generate ▸ Debug Run) to watch the graph "
                    "execute step by step here.")
        if ov.error:
            base = "Run failed:\n\n" + ov.error
        elif ov.finished:
            base = "Run finished.\n\nResult:\n" + (ov.result or "(empty)")
        elif ov.active:
            base = f"Running… active module: {ov.active}"
        else:
            base = "Ready. (Select a module to inspect it.)"
        return base + self._usage_block(ov) + self._state_block(ov)

    @staticmethod
    def _usage_block(ov) -> str:
        """Per-agent token / tool-call / step attribution for the run (the
        LangSmith/Vellum-style breakdown), appended to the run summary. Empty
        until there's something to attribute (tokens arrive at run_end)."""
        summ = getattr(ov, "summary", None)
        if not callable(summ):
            return ""
        try:
            s = ov.summary()
        except Exception:
            return ""
        per_agent, tot = s.get("per_agent", {}), s.get("totals", {})
        rows = [(nm, a) for nm, a in per_agent.items()
                if a["input_tokens"] or a["output_tokens"]
                or a["tool_calls"] or a["llm_steps"]]
        if not rows:
            return ""
        lines = ["", "", "Usage by agent (in/out tokens · tools · steps):"]
        for nm, a in rows:
            lines.append(f"  {nm}: {a['input_tokens']}/{a['output_tokens']} tok"
                         f"  ·  {a['tool_calls']} tools  ·  {a['llm_steps']} steps")
        extra = []
        if tot.get("retries"):
            extra.append(f"{tot['retries']} retries")
        if tot.get("failovers"):
            extra.append(f"{tot['failovers']} failovers")
        lines.append(
            f"  total: {tot.get('input_tokens', 0)}/{tot.get('output_tokens', 0)}"
            f" tok  ·  {tot.get('tool_calls', 0)} tools  ·  "
            f"{tot.get('llm_steps', 0)} steps"
            + (("  ·  " + ", ".join(extra)) if extra else ""))
        return "\n".join(lines)

    # write_todos status → checklist marker for the live plan view.
    _TODO_MARK = {"completed": "[x]", "in_progress": "[~]", "pending": "[ ]"}

    @classmethod
    def _state_block(cls, ov) -> str:
        """The live shared-state snapshot, appended to the run summary. The `todos`
        field (from the write_todos tool) renders as a checklist; everything else
        prints generically. Empty for graphs with no shared state."""
        state = getattr(ov, "state", None)
        if not state:
            return ""
        lines = []
        todos = state.get("todos")
        if isinstance(todos, list) and todos:
            lines += ["", "", "Plan (todos):"]
            for t in todos:
                if isinstance(t, dict):
                    mark = cls._TODO_MARK.get(t.get("status"), "[ ]")
                    lines.append(f"  {mark} {t.get('content', '')}")
                else:
                    lines.append(f"  [ ] {t}")
        others = {k: v for k, v in state.items() if k != "todos"}
        if others:
            lines += ["", "", "Shared state:"]
            for k, v in others.items():
                sv = str(v)
                if len(sv) > 200:
                    sv = sv[:200] + "…"
                lines.append(f"  {k} = {sv}")
        return "\n".join(lines)
