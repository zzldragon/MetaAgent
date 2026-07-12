"""Verify the Estimation feature — Phase 0: the report primitive + the
deterministic 'Estimate Graph' (wraps design_review), and that the Qt report
dialog constructs offscreen. Later phases (LLM prompt/tool/all) extend this."""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.chdir(BASE)

import graph_model as gm  # noqa: E402
import patterns as pat  # noqa: E402
import estimation as est  # noqa: E402

LLM = {"provider": "siliconflow", "model": "deepseek-ai/DeepSeek-V4-Flash",
       "api_key": "sk-x", "base_url": "https://api.siliconflow.cn/v1"}


# 0. Report primitive: add / counts / sorted (errors first) / finalize summary.
r = est.EstimationReport("t")
r.add("info", "note")
r.add("error", "boom")
r.add("warning", "careful")
r.finalize()
assert r.counts() == {"error": 1, "warning": 1, "info": 1}, r.counts()
assert [f.severity for f in r.sorted_findings()] == ["error", "warning", "info"]
assert r.summary == "1 error(s), 1 warning(s), 1 note(s).", r.summary
print("0. EstimationReport add/counts/sort/summary ok")

# 1. estimate_graph on a valid preset: no errors, has topology + metrics notes.
g = pat.build_pattern_graph("supervisor_worker", LLM)
rep = est.estimate_graph(g)
assert rep.title == "Estimate Graph"
assert rep.counts()["error"] == 0, [f.message for f in rep.findings]
infos = [f.message for f in rep.findings if f.severity == "info"]
assert any("Mode: supervisor" in m for m in infos), infos
assert any("Agents: 2" in m for m in infos), infos
print("1. estimate_graph(supervisor) — no errors, topology + metrics notes ok")

# 2. estimate_graph surfaces a real error: an agent with no LLM linked.
bad = gm.Graph()
a = bad.new_node("agent", 0, 0)
a.name = "lonely"
rep2 = est.estimate_graph(bad)
assert rep2.counts()["error"] >= 1, "agent with no LLM must produce an error"
assert any(f.severity == "error" for f in rep2.findings)
print("2. estimate_graph surfaces analyze() errors (agent with no LLM) ok")

# 3. empty graph: estimate_graph still returns a finalized report (an error, no crash).
rep3 = est.estimate_graph(gm.Graph())
assert rep3.summary and rep3.counts()["error"] >= 1
print("3. estimate_graph(empty) handled ok")

# 4. Qt report dialog constructs offscreen and renders every finding.
from PySide6.QtWidgets import QApplication  # noqa: E402
QApplication.instance() or QApplication([])
from canvas_qt.estimation_ui import EstimationReportDialog, _render_html  # noqa: E402
dlg = EstimationReportDialog(rep)
htmlout = _render_html(rep)
assert "Mode: supervisor" in htmlout
assert dlg.windowTitle() == "Estimation — Estimate Graph"
print("4. EstimationReportDialog constructs offscreen + renders findings ok")

# ── Phase 1: the grounded LLM-judge harness (mocked LLM, no network) ──────────

# 5. judge() parses a JSON array into Findings (source='llm'), coerces a bad
#    severity to 'info', and drops empty-message items.
def fake_ok(messages, cancel_event=None):
    assert messages[0]["role"] == "system" and messages[1]["role"] == "user"
    assert "Rubric" in messages[1]["content"] and "Artifact" in messages[1]["content"]
    return ('[{"severity":"error","message":"Contradiction: says X and not-X",'
            '"detail":"lines 2 vs 9"},'
            '{"severity":"bogus","message":"vague instruction"},'
            '{"severity":"warning","message":""}]')  # empty message dropped
jr = est.judge("check contradictions", "agent:planner", "prompt text",
               complete=fake_ok)
assert jr.ok and len(jr.findings) == 2, jr
assert all(f.source == "llm" for f in jr.findings)
assert jr.findings[0].severity == "error" and jr.findings[0].detail == "lines 2 vs 9"
assert jr.findings[1].severity == "info", "unknown severity coerces to info"
print("5. judge() parses findings, coerces severity, drops empties ok")

# 6. Fenced ```json ... ``` output still parses.
jr2 = est.judge("r", "t", "a",
                complete=lambda m, c=None: '```json\n[{"severity":"info","message":"ok"}]\n```')
assert jr2.ok and jr2.findings[0].message == "ok", jr2
print("6. judge() extracts a fenced JSON array ok")

# 7. Unparseable output -> ok=False with a note (no crash).
jr3 = est.judge("r", "t", "a", complete=lambda m, c=None: "sorry, I cannot help")
assert not jr3.ok and "unparseable" in jr3.note.lower(), jr3
print("7. judge() degrades on unparseable output ok")

# 8. No API key -> LLM layer skipped with a clear note, WITHOUT any network call.
import app_config  # noqa: E402
_real_load = app_config.load_config
try:
    app_config.load_config = lambda: {"api_key": ""}
    assert est.llm_available() is False
    jr4 = est.judge("r", "t", "a")   # complete=None -> would hit network if not gated
    assert not jr4.ok and "no api key" in jr4.note.lower(), jr4
finally:
    app_config.load_config = _real_load
print("8. judge() skips cleanly with no API key (no network) ok")

# 9. Cancellation surfaces as a note, not a crash.
def fake_cancel(messages, cancel_event=None):
    raise est.EstimationCancelled()
jr5 = est.judge("r", "t", "a", complete=fake_cancel)
assert not jr5.ok and "cancel" in jr5.note.lower(), jr5
print("9. judge() reports cancellation as a note ok")

# 10. add_judge folds LLM findings into a report; a skipped judge adds a note.
rep_j = est.EstimationReport("Estimate Prompts")
rep_j.add_judge(est.judge("r", "agent:x", "a", complete=fake_ok))
assert any(f.source == "llm" for f in rep_j.findings)
rep_j.add_judge(est.JudgeResult(False, [], "LLM check skipped — no API key set."))
assert any("skipped" in f.message.lower() and f.source == "llm" for f in rep_j.findings)
print("10. EstimationReport.add_judge folds LLM findings / skip-notes ok")

# ── Phase 2: Estimate Prompts ────────────────────────────────────────────────
import threading  # noqa: E402


def _agent_with_prompt(g, name, text=None):
    a = g.new_node("agent", 0, 0); a.name = name
    lm = g.new_node("llm", 0, 0); lm.props.update(LLM)
    assert g.add_edge(lm.id, a.id) is None
    if text is not None:
        p = g.new_node("prompt", 0, 0); p.props["text"] = text
        assert g.add_edge(p.id, a.id) is None
    return a


# 11. Deterministic prompt checks: duplicate text + missing Prompt node.
gp = gm.Graph()
_agent_with_prompt(gp, "a1", "You are identical.")
_agent_with_prompt(gp, "a2", "You are identical.")
_agent_with_prompt(gp, "a3", None)                    # no Prompt node
rp = est.estimate_prompts(gp)                          # use_llm defaults False
msgs = [f.message for f in rp.findings]
assert any("Identical prompt text" in m for m in msgs), msgs
assert any("no Prompt node" in m for m in msgs), msgs
assert all(f.source == "deterministic" for f in rp.findings), "no LLM without use_llm"
print("11. estimate_prompts deterministic: duplicate + no-prompt ok")

# 12. LLM prompt layer (mocked): per-agent + cross-prompt judgements.
gpe = pat.build_pattern_graph("planner_executor", LLM)
rp2 = est.estimate_prompts(gpe, complete=fake_ok)
llm_findings = [f for f in rp2.findings if f.source == "llm"]
assert llm_findings, "LLM prompt findings expected"
assert any(f.target.startswith("agent:") for f in llm_findings), llm_findings
assert any(f.target == "cross-prompt" for f in llm_findings), "cross-prompt pass expected"
print("12. estimate_prompts LLM (mocked): per-agent + cross-prompt ok")

# 13. Estimate Tool: deterministic 'no tools' note; LLM per-function (mocked).
rt_empty = est.estimate_tools(gm.Graph())
assert any("No tools are linked" in f.message for f in rt_empty.findings)
gt = pat.build_pattern_graph("react", LLM, tool_files=["load_csv.py"])
rt = est.estimate_tools(gt, complete=fake_ok)
assert any(f.source == "llm" and f.target.startswith("tool:") for f in rt.findings), \
    [(f.source, f.target) for f in rt.findings]
print("13. estimate_tools: no-tools note + LLM per-function (mocked) ok")

# 14. Estimate Graph LLM layer is OFF by default, ON with a backend.
gg = pat.build_pattern_graph("supervisor_worker", LLM)
assert not any(f.source == "llm" for f in est.estimate_graph(gg).findings), \
    "graph estimate must stay deterministic by default"
assert any(f.source == "llm" for f in est.estimate_graph(gg, complete=fake_ok).findings)
print("14. estimate_graph LLM layer off-by-default / on-with-backend ok")

# 15. Estimate All merges the three areas, tagging each finding's detail.
ra = est.estimate_all(gpe, complete=fake_ok)
details = " ".join(f.detail for f in ra.findings)
assert "[Prompts]" in details and "[Graph]" in details, details
assert ra.title == "Estimate All"
print("15. estimate_all merges Prompts/Graph/Tool with area tags ok")

# 16. A pre-set cancel event stops the LLM loop (deterministic findings remain).
ev = threading.Event(); ev.set()
rc = est.estimate_prompts(gpe, complete=fake_ok, cancel_event=ev)
assert not any(f.source == "llm" for f in rc.findings), "cancel must skip LLM calls"
print("16. cancel_event stops the LLM loop, keeps deterministic findings ok")

# ── Phase 5: polish (export, jump-to-node, synthesis) ────────────────────────

# 17. report_to_markdown renders title, severity, LLM tag, target and detail.
rm = est.EstimationReport("Estimate Prompts")
rm.add("error", "boom", target="planner", detail="line 3")
rm.add("info", "note", source="llm", target="agent:x")
rm.finalize()
md = est.report_to_markdown(rm)
assert "# Estimate Prompts" in md and "**ERROR**" in md
assert "[planner]" in md and "line 3" in md and "(LLM)" in md
print("17. report_to_markdown renders title/severity/LLM/target/detail ok")

# 18. Jump-to-node: jumpable target renders a node link and _on_anchor fires the
#     callback with the bare node name.
from PySide6.QtCore import QUrl  # noqa: E402
from canvas_qt.estimation_ui import _render_html  # noqa: E402
rj = est.EstimationReport("Estimate Prompts")
rj.add("warning", "contradiction", target="agent:planner", source="llm")
rj.finalize()
htmlj = _render_html(rj, jumpable={"planner"})
assert "href='node:planner'" in htmlj, htmlj
jumped = {}
dlg2 = EstimationReportDialog(rj, on_jump=lambda n: jumped.update(n=n),
                             jumpable={"planner"})
dlg2._on_anchor(QUrl("node:planner"))
assert jumped.get("n") == "planner", jumped
# a non-jumpable target stays plain text (no link)
rj2 = est.EstimationReport("t"); rj2.add("info", "m", target="tool:foo"); rj2.finalize()
assert "href='node:" not in _render_html(rj2, jumpable={"planner"})
print("18. jump-to-node link renders + _on_anchor fires callback ok")

# 19. Estimate All runs a holistic synthesis pass (an 'overall' finding).
ra2 = est.estimate_all(gpe, complete=fake_ok)
assert any(f.target == "overall" for f in ra2.findings), \
    "estimate_all should add a synthesis 'overall' finding"
print("19. estimate_all synthesis pass adds 'overall' findings ok")

# ── Streaming + non-modal (comfort the designer; don't block editing) ─────────

# 20. emit streams every finding, in production order, matching report.findings.
streamed = []
rp_s = est.estimate_prompts(gpe, complete=fake_ok, emit=streamed.append)
assert streamed == rp_s.findings, "emit must stream every finding in order"
assert len(streamed) >= 3
print("20. estimate_* streams findings via emit ok")

# 21. estimate_all streams area-tagged findings live.
streamed2 = []
ra3 = est.estimate_all(gpe, complete=fake_ok, emit=streamed2.append)
assert streamed2 == ra3.findings
assert any(f.detail.startswith("[Prompts]") for f in streamed2), streamed2
assert any(f.detail.startswith("[Graph]") for f in streamed2), streamed2
print("21. estimate_all streams area-tagged findings ok")

# 22. The streaming window is NON-MODAL (canvas stays editable); its slots stream
#     findings and finalize; jumpable targets render a node link.
from canvas_qt.estimation_ui import EstimationStreamWindow, _finding_html  # noqa: E402
win_s = EstimationStreamWindow(None,
                               lambda c, e: est.EstimationReport("t").finalize(),
                               jumpable={"planner"})
QApplication.instance().processEvents()
assert win_s.isModal() is False, "estimation window must not block the canvas"
f_one = est.Finding("warning", "contradiction here", target="agent:planner", source="llm")
win_s._on_finding(f_one)
assert win_s._count == 1 and "contradiction here" in win_s._body.toPlainText()
# streaming must ACCUMULATE (a second finding doesn't replace the first)
win_s._on_finding(est.Finding("info", "second finding msg", target="graph"))
assert win_s._count == 2
_txt = win_s._body.toPlainText()
assert "contradiction here" in _txt and "second finding msg" in _txt, _txt
win_s._on_done(est.EstimationReport("t").finalize())
assert win_s._copy_btn.isEnabled() and win_s._close_btn.text() == "Close"
assert "href='node:planner'" in _finding_html(f_one, {"planner"})
win_s._worker.wait(2000)
print("22. EstimationStreamWindow non-modal + streams + jump link ok")

# ── Phase 6: LLM-proposed fixes (HITL + analyze() self-check + revert) ─────────

# 23. is_prompt_fixable gates to prompt findings that resolve to an agent.
gfx = gm.Graph(); afx = _agent_with_prompt(gfx, "writer", "You write.")
assert est.is_prompt_fixable(gfx, est.Finding("warning", "contradiction",
                                              target="agent:writer", source="llm"))
assert est.is_prompt_fixable(gfx, est.Finding("info", "'writer' prompt is short",
                                              target="writer"))
assert not est.is_prompt_fixable(gfx, est.Finding("info", "Mode: chain", target="graph"))
assert not est.is_prompt_fixable(gfx, est.Finding("warning", "no docstring",
                                                  target="tool:foo"))
print("23. is_prompt_fixable gates prompt findings ok")

# 24. propose_fix (mocked) returns a set_prompt FixProposal; non-prompt -> None.
fixjson = lambda m, c=None: ('{"text": "You write clearly and never contradict '
                             'yourself.", "rationale": "removed the conflict"}')
prop = est.propose_fix(gfx, est.Finding("warning", "contradiction",
                                        target="agent:writer", source="llm"),
                       complete=fixjson)
assert prop is not None and prop.op == "set_prompt" and prop.target == "writer"
assert prop.before == "You write." and "clearly" in prop.after and prop.rationale
assert est.propose_fix(gfx, est.Finding("info", "Mode: chain", target="graph"),
                       complete=fixjson) is None
print("24. propose_fix returns a set_prompt proposal (mocked) ok")

# 25. apply_fix updates the agent's prompt (happy path); rejects unsupported ops.
ok, msg = est.apply_fix(gfx, est.FixProposal("set_prompt", "writer",
                                             "You write.", "You write clearly."))
assert ok, msg
assert gfx.inputs_of(afx.id, "prompt")[0].props["text"] == "You write clearly."
assert est.apply_fix(gfx, est.FixProposal("delete_everything", "writer", "", ""))[0] is False
print("25. apply_fix updates prompt text + rejects unsupported ops ok")

# 26. apply_fix self-check REVERTS when the edit would add a new graph error.
import graph_codegen as gc  # noqa: E402
_real_analyze = gc.analyze
_ac = {"n": 0}
def _fake_analyze(g):
    _ac["n"] += 1
    return {"errors": (["boom"] if _ac["n"] >= 2 else []), "warnings": []}
gc.analyze = _fake_analyze
try:
    g6 = gm.Graph(); a6 = _agent_with_prompt(g6, "w2", "orig text")
    ok3, msg3 = est.apply_fix(g6, est.FixProposal("set_prompt", "w2", "orig text", "changed"))
    assert not ok3 and "revert" in msg3.lower(), (ok3, msg3)
    assert g6.inputs_of(a6.id, "prompt")[0].props["text"] == "orig text", "must revert"
finally:
    gc.analyze = _real_analyze
print("26. apply_fix self-check reverts on a new error ok")

# 27. Streaming window exposes 'Fix with AI…' only when fix callbacks are given,
#     and enables it once a fixable finding arrives.
w_fix = EstimationStreamWindow(
    None, lambda c, e: est.EstimationReport("t").finalize(),
    proposer=lambda findings, c: [], applier=lambda p: (True, ""),
    fixable=lambda f: est.is_prompt_fixable(gfx, f))
QApplication.instance().processEvents()
assert w_fix._fix_btn is not None
rep_fx = est.EstimationReport("t")
rep_fx.add("warning", "contradiction", target="agent:writer", source="llm")
w_fix._on_done(rep_fx.finalize())
assert w_fix._fix_btn.isEnabled(), "Fix button should enable with a fixable finding"
w_fix._worker.wait(2000)
w_plain = EstimationStreamWindow(None, lambda c, e: est.EstimationReport("t").finalize())
QApplication.instance().processEvents()
assert w_plain._fix_btn is None, "no Fix button without fix callbacks"
w_plain._worker.wait(2000)
print("27. streaming window gates the 'Fix with AI…' button ok")

# 28. Estimation windows + the rewrite-review dialog are resizable (size grip),
#     and the review dialog shows the full before/after prompt.
from PySide6.QtWidgets import QPlainTextEdit  # noqa: E402
from canvas_qt.estimation_ui import EstimationReportDialog, _FixConfirmDialog  # noqa: E402
w_rs = EstimationStreamWindow(None, lambda c, e: est.EstimationReport("t").finalize())
QApplication.instance().processEvents()
assert w_rs.isSizeGripEnabled(), "estimation stream window must be resizable"
w_rs._worker.wait(2000)
rep_rs = est.EstimationReport("t"); rep_rs.add("info", "x"); rep_rs.finalize()
assert EstimationReportDialog(rep_rs).isSizeGripEnabled(), "report dialog must be resizable"
fc = _FixConfirmDialog(est.FixProposal("set_prompt", "planner", "OLD PROMPT", "NEW PROMPT"))
assert fc.isSizeGripEnabled(), "rewrite-review dialog must be resizable"
boxes = "\n".join(b.toPlainText() for b in fc.findChildren(QPlainTextEdit))
assert "OLD PROMPT" in boxes and "NEW PROMPT" in boxes, boxes
print("28. estimation + rewrite windows resizable; review shows before/after ok")

# 29. propose_agent_fix rewrites ONE agent's prompt for ALL its findings at once
#     (no clobber), and fixable_by_agent groups fixable findings by agent.
findings_multi = [
    est.Finding("error", "contradiction A", target="agent:writer", source="llm"),
    est.Finding("warning", "ambiguity B", target="agent:writer", source="llm"),
]
seen_issues = {}
def fix_capture(messages, cancel_event=None):
    seen_issues["user"] = messages[1]["content"]
    return '{"text": "You write clearly, resolving A and B.", "rationale": "fixed A+B"}'
p_multi = est.propose_agent_fix(gfx, "writer", findings_multi, complete=fix_capture)
assert p_multi is not None and p_multi.op == "set_prompt", p_multi
assert "contradiction A" in seen_issues["user"] and "ambiguity B" in seen_issues["user"], \
    "both issues must be sent in ONE rewrite request (no clobber)"
grouped = est.fixable_by_agent(
    gfx, findings_multi + [est.Finding("info", "Mode: chain", target="graph")])
assert set(grouped) == {"writer"} and len(grouped["writer"]) == 2, grouped
# propose_fix still works as a single-finding wrapper
assert est.propose_fix(gfx, findings_multi[0], complete=fix_capture) is not None
print("29. propose_agent_fix batches an agent's findings; grouping ok")

# ── "Fix all": tool-docstring fixes too ──────────────────────────────────────
import ast  # noqa: E402

# 30. _rewrite_docstring inserts a missing docstring and replaces an existing one.
out1 = est._rewrite_docstring("def foo(a, b):\n    return a + b\n", "foo", "Add a and b.")
fn1 = next(n for n in ast.walk(ast.parse(out1)) if getattr(n, "name", None) == "foo")
assert ast.get_docstring(fn1) == "Add a and b.", out1
out2 = est._rewrite_docstring('def bar(x):\n    """old."""\n    return x\n',
                              "bar", "New multi\nline doc.")
fn2 = next(n for n in ast.walk(ast.parse(out2)) if getattr(n, "name", None) == "bar")
assert ast.get_docstring(fn2) == "New multi\nline doc.", out2
assert est._rewrite_docstring("def f():\n    return 1\n", "missing", "x") is None
print("30. _rewrite_docstring inserts/replaces docstrings ok")

# 31. apply_fix set_docstring rewrites a tool file (self-checked); reverts safely.
import os  # noqa: E402
import shutil  # noqa: E402
import tempfile  # noqa: E402
_tdir = tempfile.mkdtemp(prefix="est_tool_")
try:
    _tp = os.path.join(_tdir, "mytool.py")
    with open(_tp, "w", encoding="utf-8") as _f:
        _f.write("def do_thing(n):\n    return n * 2\n")
    ok_ds, msg_ds = est.apply_fix(None, est.FixProposal(
        "set_docstring", "do_thing", "", "Double n and return it.", meta={"path": _tp}))
    assert ok_ds, msg_ds
    fnd = next(n for n in ast.walk(ast.parse(open(_tp, encoding="utf-8").read()))
               if getattr(n, "name", None) == "do_thing")
    assert ast.get_docstring(fnd) == "Double n and return it."
    # a missing function -> not applied
    assert est.apply_fix(None, est.FixProposal(
        "set_docstring", "nope", "", "x", meta={"path": _tp}))[0] is False
finally:
    shutil.rmtree(_tdir, ignore_errors=True)
print("31. apply_fix set_docstring rewrites a tool file (self-checked) ok")

# 32. Tool findings route through is_fixable / propose_fixes to a set_docstring.
gt2 = pat.build_pattern_graph("react", LLM, tool_files=["load_csv.py"])
_funcs = est._tool_functions("load_csv.py")
assert _funcs, "load_csv.py should define a function"
_fn0 = _funcs[0][0]
tfind = est.Finding("warning", f"tool '{_fn0}' has no docstring", target=f"tool:{_fn0}")
assert est.is_tool_fixable(gt2, tfind) and est.is_fixable(gt2, tfind)
assert not est.is_fixable(gt2, est.Finding("info", "Mode: chain", target="graph"))
ds_json = lambda m, c=None: '{"docstring": "A clearer docstring.", "rationale": "clarified"}'
props = est.propose_fixes(gt2, [tfind], complete=ds_json)
assert len(props) == 1 and props[0].op == "set_docstring" and props[0].target == _fn0, props
print("32. tool findings route via is_fixable/propose_fixes to set_docstring ok")

# 33. (bug fix) The non-modal viewer windows are PARENTLESS top-levels so the
#     canvas can be raised in front of them — a parented QDialog is an OWNED
#     window that Windows keeps permanently above its parent. Passing a parent
#     (the canvas) must NOT re-own them.
from PySide6.QtWidgets import QWidget  # noqa: E402
from canvas_qt.code_view_ui import CodeViewWindow  # noqa: E402
_owner = QWidget()                                    # a stand-in "canvas window"
_sw = EstimationStreamWindow(_owner, lambda c, e: est.EstimationReport("t").finalize())
QApplication.instance().processEvents()
assert _sw.parent() is None, "stream window must not be owned by the canvas"
_sw._worker.wait(2000)
_rd = EstimationReportDialog(est.EstimationReport("t").finalize(), _owner)
assert _rd.parent() is None, "report dialog must not be owned by the canvas"
_cv = CodeViewWindow({"files": {"agent.py": "x\n", "config.json": ""},
                      "spans": {"agent.py": [], "config.json": []},
                      "node": "n (agent)", "note": ""}, _owner)
assert _cv.parent() is None, "code view must not be owned by the canvas"
print("33. viewer windows are parentless top-levels (canvas can come forward) ok")

# 34. (bug fix) 'Fix with AI…' narrates into the sliding-window BODY so a click
#     never looks like it did nothing: an empty proposal set explains why, and an
#     applied proposal is logged in the body (not only the status label).
from PySide6.QtWidgets import QDialog  # noqa: E402
import canvas_qt.estimation_ui as _EUI  # noqa: E402
w_nar = EstimationStreamWindow(
    None, lambda c, e: est.EstimationReport("t").finalize(),
    proposer=lambda findings, c: [], applier=lambda p: (True, "Updated x's prompt."),
    fixable=lambda f: True)
QApplication.instance().processEvents()
w_nar._worker.wait(2000)
w_nar._html_parts = []                                 # empty-proposals path
w_nar._on_proposals([])
assert "No applicable AI fix" in w_nar._body.toPlainText(), w_nar._body.toPlainText()
_orig_exec = _EUI._FixConfirmDialog.exec               # auto-accept the modal review
_EUI._FixConfirmDialog.exec = lambda self: QDialog.Accepted
w_nar._restart = lambda status="": None                # stub the re-estimate (no worker)
w_nar._html_parts = []
w_nar._on_proposals([est.FixProposal("set_prompt", "planner", "old", "new", "why")])
_body = w_nar._body.toPlainText()
_EUI._FixConfirmDialog.exec = _orig_exec
assert "1 proposed change" in _body and "Applied" in _body, _body
print("34. fix flow narrates proposals + apply into the sliding-window body ok")

# 35. (bug fix) Each agent/tool is fixed AT MOST ONCE per fix run — _fixable_findings
#     excludes both declined and already-fixed targets, so a later re-check round
#     can't re-propose (and clobber) an earlier fix. Batching per agent is unchanged
#     (one proposal per agent, see check 29).
w_once = EstimationStreamWindow(
    None, lambda c, e: est.EstimationReport("t").finalize(),
    proposer=lambda f, c: [], applier=lambda p: (True, ""), fixable=lambda f: True)
QApplication.instance().processEvents()
w_once._worker.wait(2000)
rep_once = est.EstimationReport("t")
rep_once.add("warning", "vague A", target="agent:writer", source="llm")
rep_once.add("warning", "contradiction B", target="agent:writer", source="llm")
rep_once.add("warning", "vague C", target="agent:editor", source="llm")
rep_once.finalize()
w_once._report = rep_once
assert len(w_once._fixable_findings()) == 3, "all three fixable to start"
w_once._fixed.add("writer")                 # writer already fixed this run
left = w_once._fixable_findings()
assert all("writer" not in f.target for f in left) and len(left) == 1, left
w_once._declined.add("editor")              # editor skipped by the user
assert w_once._fixable_findings() == [], "fixed + declined targets are excluded"
print("35. fix-once: _fixable_findings excludes fixed + declined targets ok")

# 36. (bug fix) A prompt fix is in-memory only, so the loop reminds the user to save;
#     a tool-docstring fix does not (it's already on disk).
w_save = EstimationStreamWindow(
    None, lambda c, e: est.EstimationReport("t").finalize(),
    proposer=lambda f, c: [], applier=lambda p: (True, ""), fixable=lambda f: True)
QApplication.instance().processEvents()
w_save._worker.wait(2000)
w_save._need_save = True
w_save._html_parts = []
w_save._finish_fixing("done")
assert "Save the graph" in w_save._body.toPlainText(), w_save._body.toPlainText()
assert w_save._need_save is False, "save reminder shows once"
print("36. prompt fixes prompt a save reminder (in-memory, not written to file) ok")

print("\nALL ESTIMATION CHECKS PASSED")
