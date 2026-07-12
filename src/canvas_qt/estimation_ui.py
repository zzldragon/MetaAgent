"""Qt rendering for the Estimation menu (see estimation.py for the logic).

Two views, both non-modal so the canvas stays editable while estimation runs:
  * EstimationStreamWindow — the live view: runs the estimate on a worker thread
    and appends findings bit-by-bit as they are produced (deterministic ones
    instantly, then each LLM finding as it lands). Cancel / Copy / Save; findings
    that name a canvas node are clickable (jump-to-node).
  * EstimationReportDialog — a static view of a finished report (used by tests
    and anywhere a complete report is shown at once).

Kept separate from estimation.py so the estimation logic stays headless/testable.
"""

from __future__ import annotations

import html
import threading

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QTextBrowser,
    QVBoxLayout,
)

from canvas_qt.dialogs import make_dialog_resizable

_SEV_COLOR = {"error": "#E53935", "warning": "#F9A825", "info": "#78909C"}


def _esc(s) -> str:
    return html.escape(str(s or ""))


def _norm_target(target: str) -> str:
    """Strip a judge label prefix ('agent:'/'tool:') to the bare node/target name."""
    return target.split(":", 1)[-1] if target else ""


def _finding_html(f, jumpable=()) -> str:
    """One finding as an HTML block. Target renders as a jump-to-node link when it
    resolves to a canvas node name."""
    jumpable = set(jumpable or ())
    color = _SEV_COLOR.get(f.severity, "#78909C")
    tag = f.severity.upper() + (" · LLM" if f.source == "llm" else "")
    name = _norm_target(f.target)
    if f.target and f.target != "graph" and name in jumpable:
        target_html = (f" <a href='node:{_esc(name)}' "
                       f"style='color:#64B5F6; text-decoration:none'>[{_esc(name)}]</a>")
    elif f.target and f.target != "graph":
        target_html = f" <span style='color:#9aa0a6'>[{_esc(f.target)}]</span>"
    else:
        target_html = ""
    detail = (f"<div style='color:#9aa0a6; margin:2px 0 6px 16px'>{_esc(f.detail)}</div>"
              if f.detail else "")
    return (f"<div style='margin:8px 0 2px 0'>"
            f"<span style='color:{color}; font-weight:bold'>{tag}</span>{target_html}</div>"
            f"<div style='margin:0 0 2px 0'>{_esc(f.message)}</div>{detail}")


def _render_html(report, jumpable=()) -> str:
    rows = [_finding_html(f, jumpable) for f in report.sorted_findings()]
    body = "".join(rows) or "<i>No findings.</i>"
    return f"<html><body style='font-family:Segoe UI, Arial, sans-serif'>{body}</body></html>"


# ── shared jump / export mixin behaviour ─────────────────────────────────────
def _jump_from_anchor(url, on_jump) -> None:
    s = url.toString()
    if s.startswith("node:") and on_jump:
        on_jump(s[len("node:"):])


def _copy_report(report) -> None:
    import estimation
    QApplication.clipboard().setText(estimation.report_to_markdown(report))


def _save_report(parent, report) -> None:
    import estimation
    default = report.title.replace(" ", "_") + ".md"
    path, _ = QFileDialog.getSaveFileName(
        parent, "Save estimation report", default, "Markdown (*.md);;Text (*.txt)")
    if not path:
        return
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(estimation.report_to_markdown(report))
    except OSError as e:
        QMessageBox.critical(parent, "Save failed", str(e))


class EstimationReportDialog(QDialog):
    """Static, non-modal viewer for a finished EstimationReport."""

    def __init__(self, report, parent=None, *, on_jump=None, jumpable=None):
        # Parentless top-level (see EstimationStreamWindow): a parented QDialog is
        # an OWNED window that Windows keeps permanently above the canvas. With no
        # Qt parent it stacks normally, so the canvas can be raised in front.
        # show_estimation() holds a reference so it isn't GC'd.
        super().__init__(None)
        self._report = report
        self._on_jump = on_jump
        self.setModal(False)
        self.setWindowTitle("Estimation — " + report.title)
        self.resize(720, 560)
        make_dialog_resizable(self)          # size grip + maximize/restore
        v = QVBoxLayout(self)
        head = QLabel(report.summary or "")
        head.setStyleSheet("font-weight:bold; padding:2px 0 6px 0;")
        v.addWidget(head)
        body = QTextBrowser()
        body.setOpenLinks(False)
        body.setOpenExternalLinks(False)
        body.setHtml(_render_html(report, set(jumpable or ())))
        body.anchorClicked.connect(self._on_anchor)
        v.addWidget(body, 1)
        bb = QDialogButtonBox()
        bb.addButton("Copy", QDialogButtonBox.ActionRole).clicked.connect(self._copy)
        bb.addButton("Save…", QDialogButtonBox.ActionRole).clicked.connect(self._save)
        bb.addButton(QDialogButtonBox.Close).clicked.connect(self.close)
        v.addWidget(bb)

    def _on_anchor(self, url):
        _jump_from_anchor(url, self._on_jump)

    def _copy(self):
        _copy_report(self._report)

    def _save(self):
        _save_report(self, self._report)


def show_estimation(parent, report, *, on_jump=None, jumpable=None) -> None:
    """Show a finished report non-modally, holding a reference so it isn't GC'd."""
    dlg = EstimationReportDialog(report, parent, on_jump=on_jump, jumpable=jumpable)
    if parent is not None:
        held = getattr(parent, "_estimation_dialogs", None)
        if held is None:
            held = parent._estimation_dialogs = []
        held.append(dlg)
        dlg.finished.connect(lambda *_: held.remove(dlg) if dlg in held else None)
    dlg.show()
    dlg.raise_()


class _EstimateWorker(QThread):
    """Runs `make_fn(cancel_event, emit)` off the GUI thread. `emit(finding)` is
    the worker's `finding` signal, so each finding streams to the GUI thread as
    it is produced. `make_fn` must NOT touch Qt (it may call the LLM)."""
    finding = Signal(object)     # a Finding, streamed as produced
    done = Signal(object)        # the final EstimationReport
    failed = Signal(str)

    def __init__(self, make_fn, cancel_event, parent=None):
        super().__init__(parent)
        self._make_fn = make_fn
        self._cancel = cancel_event

    def run(self):
        try:
            self.done.emit(self._make_fn(self._cancel, self.finding.emit))
        except Exception as e:  # noqa: BLE001 — surface to the GUI, don't crash it
            self.failed.emit(str(e))


class _FixWorker(QThread):
    """Computes all fix proposals off the GUI thread (each may hit the LLM):
    `proposer(findings, cancel)` returns one prompt rewrite per agent + one
    docstring rewrite per tool function. Emits the proposal list when done."""
    ready = Signal(object)
    failed = Signal(str)

    def __init__(self, proposer, findings, cancel_event, parent=None):
        super().__init__(parent)
        self._proposer = proposer
        self._findings = findings
        self._cancel = cancel_event

    def run(self):
        try:
            self.ready.emit(self._proposer(self._findings, self._cancel) or [])
        except Exception as e:  # noqa: BLE001
            self.failed.emit(str(e))


class _FixConfirmDialog(QDialog):
    """Resizable review of ONE proposed prompt rewrite: rationale + the full
    before/after prompt in scrollable boxes. Apply or Skip. (Replaces a cramped
    QMessageBox so a long prompt is actually readable.)"""

    def __init__(self, proposal, parent=None):
        super().__init__(parent)
        kind = "docstring" if proposal.op == "set_docstring" else "prompt"
        empty = ("(none — no docstring yet)" if kind == "docstring"
                 else "(none — uses the role default template)")
        self.setWindowTitle(f"Rewrite {kind} — {proposal.target}")
        self.resize(680, 560)
        make_dialog_resizable(self)
        v = QVBoxLayout(self)
        head = QLabel(f"Rewrite {proposal.target}'s {kind}?"
                      + (f"\n{proposal.rationale}" if proposal.rationale else ""))
        head.setWordWrap(True)
        head.setStyleSheet("font-weight:bold; padding:2px 0 4px 0;")
        v.addWidget(head)
        v.addWidget(QLabel("Before:"))
        before = QPlainTextEdit(proposal.before or empty)
        before.setReadOnly(True)
        v.addWidget(before, 1)
        v.addWidget(QLabel("After:"))
        after = QPlainTextEdit(proposal.after)
        after.setReadOnly(True)
        v.addWidget(after, 1)
        bb = QDialogButtonBox()
        bb.addButton("Apply", QDialogButtonBox.AcceptRole).clicked.connect(self.accept)
        bb.addButton("Skip", QDialogButtonBox.RejectRole).clicked.connect(self.reject)
        v.addWidget(bb)


class EstimationStreamWindow(QDialog):
    """Non-modal live view: streams findings as the estimate produces them, so the
    designer sees results bit-by-bit AND can keep editing the graph meanwhile."""

    MAX_FIX_ROUNDS = 5

    def __init__(self, parent, make_fn, *, on_jump=None, jumpable=None,
                 proposer=None, applier=None, fixable=None):
        # Parentless top-level so the canvas can be brought in front of it: a
        # parented QDialog is an OWNED window that Windows keeps permanently above
        # its parent, even when non-modal and even when the parent is clicked. With
        # no Qt parent there's no owner → normal z-order stacking. It's kept alive
        # by run_estimation's held reference (not by a Qt parent).
        super().__init__(None)
        self._make_fn = make_fn          # re-run for the fix→re-estimate loop
        self._on_jump = on_jump
        self._jumpable = set(jumpable or ())
        self._proposer = proposer        # (findings, cancel) -> list[FixProposal] (off-thread)
        self._applier = applier          # (proposal) -> (ok, msg) (GUI thread)
        self._fixable = fixable          # (finding) -> bool
        self._fix_btn = None
        self._declined: set = set()      # targets the user skipped (don't re-propose)
        self._fixed: set = set()         # targets already fixed once this session —
                                         # never rewrite the same agent/tool twice, so
                                         # a later round can't fight an earlier fix
        self._need_save = False          # a prompt fix landed in the canvas (in-memory)
        self._fix_round = 0
        self._auto_continue = False      # resume the fix loop after a re-estimate
        self._report = None
        self._count = 0
        self._html_parts: list[str] = []     # accumulated finding HTML (see _on_finding)
        self._cancel = threading.Event()
        self.setModal(False)                       # canvas stays editable
        self.setWindowTitle("Estimation")
        self.resize(720, 560)
        make_dialog_resizable(self)                # size grip + maximize/restore

        v = QVBoxLayout(self)
        self._status = QLabel("Running…")
        self._status.setStyleSheet("font-weight:bold; padding:2px 0 6px 0;")
        v.addWidget(self._status)
        self._body = QTextBrowser()
        self._body.setOpenLinks(False)
        self._body.setOpenExternalLinks(False)
        self._body.anchorClicked.connect(lambda u: _jump_from_anchor(u, self._on_jump))
        v.addWidget(self._body, 1)
        bb = QDialogButtonBox()
        self._copy_btn = bb.addButton("Copy", QDialogButtonBox.ActionRole)
        self._save_btn = bb.addButton("Save…", QDialogButtonBox.ActionRole)
        if proposer and applier and fixable:
            self._fix_btn = bb.addButton("Fix with AI…", QDialogButtonBox.ActionRole)
            self._fix_btn.setEnabled(False)
            self._fix_btn.clicked.connect(self._start_fix)
        self._close_btn = bb.addButton("Cancel", QDialogButtonBox.RejectRole)
        self._copy_btn.setEnabled(False)
        self._save_btn.setEnabled(False)
        self._copy_btn.clicked.connect(lambda: _copy_report(self._report))
        self._save_btn.clicked.connect(lambda: _save_report(self, self._report))
        self._close_btn.clicked.connect(self.close)
        v.addWidget(bb)

        self._worker = _EstimateWorker(make_fn, self._cancel, self)
        self._worker.finding.connect(self._on_finding)
        self._worker.done.connect(self._on_done)
        self._worker.failed.connect(self._on_failed)
        self._worker.start()

    def _render_body(self):
        # Re-render the WHOLE accumulated document with setHtml() rather than
        # insertHtml()-ing fragments piecewise: successive insertHtml() calls
        # collapse block separation and bleed the previous span's bold/colour
        # into the next block, so the live view rendered malformed. A single
        # well-formed document (the same path as the copied/exported report)
        # renders correctly. Blocks are few, so re-rendering is cheap.
        self._body.setHtml(
            "<html><body style='font-family:Segoe UI, Arial, sans-serif'>"
            + "".join(self._html_parts) + "</body></html>")
        sb = self._body.verticalScrollBar()
        sb.setValue(sb.maximum())                  # keep the newest block in view

    def _note(self, text: str, color: str = "#8ab4f8"):
        """Append a progress line to the live body so the window always shows what
        the fix flow is doing (not just the status label)."""
        self._html_parts.append(
            f"<div style='margin:8px 0 2px 0; color:{color}'>• {_esc(text)}</div>")
        self._render_body()

    def _on_finding(self, f):
        self._count += 1
        self._html_parts.append(_finding_html(f, self._jumpable))
        self._render_body()
        self._status.setText(f"Running… {self._count} finding(s)")

    def _on_done(self, report):
        self._report = report
        cancelled = self._cancel.is_set()
        self._status.setText(
            (("Cancelled — " if cancelled else "") + (report.summary or ""))
            or f"Done — {self._count} finding(s).")
        self._copy_btn.setEnabled(True)
        self._save_btn.setEnabled(True)
        self._close_btn.setText("Close")
        if self._fix_btn is not None and self._fixable_findings():
            self._fix_btn.setEnabled(True)
        # Autonomous loop: after a re-estimate triggered by a fix, resume fixing.
        if self._auto_continue and not cancelled:
            self._auto_continue = False
            self._fix_round_step()

    # ── AI fix→re-check loop: propose (prompts per agent + tool docstrings),
    #    confirm each, apply w/ self-check, then re-estimate and repeat ──────────
    def _fixable_findings(self) -> list:
        """Fixable findings in the current report, minus targets the user declined
        AND targets already fixed once this session — each agent/tool is rewritten
        at most once per fix run, so a later round can't clobber an earlier fix."""
        return [f for f in (self._report.findings if self._report else [])
                if self._fixable(f)
                and _norm_target(f.target) not in self._declined
                and _norm_target(f.target) not in self._fixed]

    def _start_fix(self):
        self._fix_round = 0
        self._declined.clear()
        self._fixed.clear()
        self._need_save = False
        self._note("Fix with AI — proposing changes (this may call the LLM)…")
        self._fix_round_step()

    def _fix_round_step(self):
        units = self._fixable_findings()
        if not units:
            self._finish_fixing("No more fixable findings — done.")
            return
        if self._fix_round >= self.MAX_FIX_ROUNDS:
            self._finish_fixing(f"Stopped after {self.MAX_FIX_ROUNDS} fix rounds.")
            return
        self._fix_round += 1
        self._fix_btn.setEnabled(False)
        self._status.setText(f"Round {self._fix_round}: proposing fixes…")
        self._fixworker = _FixWorker(self._proposer, units, self._cancel, self)
        self._fixworker.ready.connect(self._on_proposals)
        self._fixworker.failed.connect(self._on_failed)
        self._fixworker.start()

    def _on_proposals(self, proposals):
        if not proposals:
            # No AI change was produced: the model proposed no edit, returned an
            # unparseable reply, or there's no LLM key. Say so in the body (not
            # just the status) so the click never looks like it did nothing.
            self._note("No applicable AI fix was produced — the model proposed no "
                       "change (or no LLM key is set). Nothing to apply.", "#F9A825")
            self._finish_fixing("No fixes proposed.")
            return
        self._note(f"{len(proposals)} proposed change(s) — review each below.")
        applied = 0
        for p in proposals:                       # HITL: review each rewrite (resizable)
            if self._cancel.is_set():
                break
            what = ("docstring" if p.op == "set_docstring" else "prompt")
            dlg = _FixConfirmDialog(p, self)
            dlg.show()
            dlg.raise_()
            dlg.activateWindow()                   # make sure the review is frontmost
            if dlg.exec() == QDialog.Accepted:
                ok, msg = self._applier(p)         # applies w/ analyze() self-check
                applied += 1 if ok else 0
                if ok:
                    self._fixed.add(p.target)      # fix this target once, never re-fight it
                    if p.op == "set_prompt":       # prompt edits live in the canvas only
                        self._need_save = True
                self._note((f"✓ Applied: rewrote {p.target}'s {what}." if ok
                            else f"⚠ Not applied ({p.target}'s {what}): {msg}"),
                           "#7CB342" if ok else "#F9A825")
            else:
                self._declined.add(p.target)       # skipped -> don't re-propose it
                self._note(f"Skipped {p.target}'s {what}.", "#9aa0a6")
        if applied and not self._cancel.is_set():
            # Re-check on the UPDATED graph, then continue the loop (see _on_done).
            self._auto_continue = True
            self._restart("Re-checking after fixes…")
        else:
            self._finish_fixing(f"Applied {applied} fix(es); nothing more to apply.")

    def _restart(self, status: str):
        """Re-run the estimate in this same window (for the fix→re-check loop)."""
        if self._cancel.is_set():
            return
        self._html_parts = []
        self._count = 0
        self._report = None
        self._body.clear()
        self._status.setText(status)
        self._copy_btn.setEnabled(False)
        self._save_btn.setEnabled(False)
        if self._fix_btn is not None:
            self._fix_btn.setEnabled(False)
        self._worker = _EstimateWorker(self._make_fn, self._cancel, self)
        self._worker.finding.connect(self._on_finding)
        self._worker.done.connect(self._on_done)
        self._worker.failed.connect(self._on_failed)
        self._worker.start()

    def _finish_fixing(self, msg: str):
        self._auto_continue = False
        self._status.setText(msg)
        if self._need_save:
            # Prompt rewrites are applied to the in-memory canvas graph, not to any
            # file — the user must save the graph for them to persist (unlike tool
            # docstring fixes, which are written straight to tools/*.py).
            self._note("Prompt fixes were applied to the canvas — Save the graph "
                       "(Ctrl+S / Graph → Save) to keep them. Tool-docstring fixes "
                       "are already written to their tools/*.py file.", "#7CB342")
            self._need_save = False
        if self._fix_btn is not None:
            self._fix_btn.setEnabled(bool(self._fixable_findings()))

    def _on_failed(self, msg):
        self._status.setText("Estimation failed.")
        QMessageBox.critical(self, "Estimation failed", msg)
        self._close_btn.setText("Close")

    def closeEvent(self, event):
        # Closing mid-run cancels the estimate; the worker finishes on its own and
        # the window (still referenced by the designer) tears down when it does.
        self._cancel.set()
        super().closeEvent(event)


def run_estimation(parent, make_fn, *, on_jump=None, jumpable=None,
                   proposer=None, applier=None, fixable=None) -> "EstimationStreamWindow":
    """Open a non-modal streaming estimation window and start it. Held on the
    parent so it isn't GC'd; released when it closes. proposer/applier/fixable
    enable the 'Fix with AI…' fix→re-check loop."""
    win = EstimationStreamWindow(parent, make_fn, on_jump=on_jump, jumpable=jumpable,
                                 proposer=proposer, applier=applier, fixable=fixable)
    if parent is not None:
        held = getattr(parent, "_estimation_windows", None)
        if held is None:
            held = parent._estimation_windows = []
        held.append(win)
        win.finished.connect(lambda *_: held.remove(win) if win in held else None)
    win.show()
    win.raise_()
    return win
