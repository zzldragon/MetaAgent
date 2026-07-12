"""Smoke test for the generated PySide6 GUI.

Self-contained: generates fresh agents (no dependency on any committed example),
then drives the generated gui.py headlessly under the offscreen Qt platform. It
exercises the parts most at risk in the wx->PySide6 port: the worker-thread ->
GUI signal bridge (run / stream / Stop), the blocking HITL confirm & review
dialogs, the node-config dialogs, and the vision attach / drag-drop path.
"""

import os
import sys
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.chdir(BASE)

import shutil  # noqa: E402
import tempfile  # noqa: E402

import graph_codegen  # noqa: E402
from app_config import GENERATED_DIR  # noqa: E402
from graph_model import Graph  # noqa: E402

LLM = {"provider": "siliconflow", "model": "deepseek-ai/DeepSeek-V4-Flash",
       "api_key": "sk-test", "base_url": "https://api.siliconflow.cn/v1"}
LLM2 = {**LLM, "model": "deepseek-ai/DeepSeek-V4-Flash"}


def _gen(graph, name, gui=True):
    out = os.path.join(GENERATED_DIR, name)
    if os.path.exists(out):
        shutil.rmtree(out)
    return graph_codegen.generate_from_graph(graph, name, gui=gui)


# ── main agent: 2-LLM planner -> writer, plus an Eval node and a Skills node ──
g = Graph()
planner = g.new_node("agent", 0, 0); planner.name = "planner"
la = g.new_node("llm", 0, 0); la.props.update(LLM)
lb = g.new_node("llm", 0, 0); lb.props.update(LLM2)
g.add_edge(la.id, planner.id)
g.add_edge(lb.id, planner.id)                 # 2 LLMs -> switchable -> LLM menu
writer = g.new_node("agent", 0, 0); writer.name = "writer"
lc = g.new_node("llm", 0, 0); lc.props.update(LLM)
g.add_edge(lc.id, writer.id)
g.add_edge(planner.id, writer.id)             # pipeline planner -> writer
ev = g.new_node("eval", 0, 0); ev.name = "smoke_evals"
g.add_edge(ev.id, planner.id)
sk = g.new_node("skill", 0, 0); sk.name = "guides"
sk.props["skills"] = [{"name": "tone", "text": "Be concise."}]
g.add_edge(sk.id, planner.id)                 # Skills node -> Skills menu

out = _gen(g, "smoke_gui_demo")
vis_out = None
try:
    assert os.path.isfile(os.path.join(out, "gui.py")), "gui.py not generated"
    reqs = open(os.path.join(out, "requirements.txt"), encoding="utf-8").read()
    assert "PySide6" in reqs and "wxPython" not in reqs, reqs
    print("generated PySide6 gui.py + requirements ok")

    sys.path.insert(0, out)
    os.chdir(out)
    from PySide6.QtGui import QAction
    from PySide6.QtWidgets import QApplication, QMessageBox

    import agent as core
    import gui

    app = QApplication.instance() or QApplication([])
    frame = gui.ChatFrame()
    frame.append("smoke test line")
    assert "smoke test line" in frame.output.toPlainText()
    assert frame.windowTitle() == "smoke_gui_demo"

    # ── menus ────────────────────────────────────────────────────────────────
    titles = [a.text().replace("&", "") for a in frame.menuBar().actions()]
    print("menus:", titles)
    for want in ("Workspace", "LLM", "History", "Settings", "Skills", "Evals"):
        assert any(want in t for t in titles), (want, titles)
    assert not any(t == "Server" for t in titles), titles  # no webserver node
    actions = [a.text().replace("&", "") for a in frame.findChildren(QAction)]
    assert "planner" in actions, actions             # switchable LLM submenu
    for want in ("Run Evals", "Edit Eval Sets", "Manage Skills",
                 "Clear History", "Edit Settings", "Reload Config (config.json)"):
        assert any(want in a for a in actions), (want, actions)
    print("menus + actions ok (incl. Skills, Settings, LLM switch)")

    # ── Settings dialog: edit a per-LLM key + proxy, collect, round-trips ─────
    assert hasattr(core, "save_config"), "runtime exposes save_config"
    sdlg = gui.SettingsDialog(frame)
    assert sdlg._llm_rows, "Settings dialog lists the per-agent LLMs"
    a_name, idx, fields = sdlg._llm_rows[0]
    fields["api_key"].setText("sk-edited")
    fields["proxy"].setText("http://10.0.0.5:8080")
    new_cfg = sdlg.collect()
    lc = new_cfg["llms"][a_name][idx]
    assert lc["api_key"] == "sk-edited" and lc["proxy"] == "http://10.0.0.5:8080", lc
    # blank proxy must be dropped (falls back to env/direct), not stored as ""
    fields["proxy"].setText("")
    assert "proxy" not in sdlg.collect()["llms"][a_name][idx]
    # save_config persists + reloads (CONFIG picks up the edited key)
    core.save_config(sdlg.collect())
    assert core.CONFIG["llms"][a_name][idx]["api_key"] == "sk-edited"
    print("settings dialog ok (edit per-LLM key/proxy, save_config round-trips)")

    # ── EvalSetsDialog: edit + persist ───────────────────────────────────────
    n0 = len(core.EVAL_SETS)
    edlg = gui.EvalSetsDialog(frame)
    assert edlg.sets.count() == n0
    core.add_eval_set("smoke_set", None)
    core.add_eval_case(len(core.EVAL_SETS) - 1,
                       {"id": "s1", "input": "hi", "expected_output": "hello"})
    edlg._reload_sets(keep=len(core.EVAL_SETS) - 1)
    assert edlg.sets.count() == n0 + 1 and edlg.cases.count() == 1
    assert os.path.exists(os.path.join(out, "evals.json"))
    core.remove_eval_set(len(core.EVAL_SETS) - 1)
    print("EvalSetsDialog edit + persist ok")

    # ── leaf eval dialogs: legacy cases load into the typed grader form ───────
    # (EvalCaseEditDialog now emits {type, value} typed assertions; a legacy
    # judge/expected_regex/expected_output maps to its equivalent grader type.)
    for case, want_type in (({"judge": "good?"}, "judge"),
                            ({"expected_regex": "[0-9]+"}, "regex"),
                            ({"expected_output": "hi"}, "contains")):
        vals = gui.EvalCaseEditDialog(
            frame, "t", {"id": "c", "input": "i", **case}).values()
        assert vals.get("type") == want_type, (want_type, vals)
        assert vals.get("value") == list(case.values())[0], vals
    assert gui.EvalSetDialog(frame, "t", "s", None).values() == ("s", None)
    tgt = core.eval_targets()[0]
    assert gui.EvalSetDialog(frame, "t", "s", tgt).values() == ("s", tgt)
    print("EvalCaseEditDialog / EvalSetDialog values() round-trip ok")

    # ── SkillsDialog: dialog <-> core wiring (remove path, no sub-dialog) ─────
    sdlg = gui.SkillsDialog(frame)
    assert sdlg.agent.count() >= 1 and "planner" in [
        sdlg.agent.itemText(i) for i in range(sdlg.agent.count())]
    core.add_skill("planner", "extra", "Cite sources.")
    sdlg._reload()
    before = len(core.skills_for("planner"))
    sdlg.listbox.setCurrentRow(sdlg.listbox.count() - 1)
    sdlg.on_remove()
    assert len(core.skills_for("planner")) == before - 1
    print("SkillsDialog reflects + edits core skills ok")

    # ── WorkspaceDialog.on_remove: mutate-then-persist ───────────────────────
    d1, d2 = tempfile.mkdtemp(), tempfile.mkdtemp()
    try:
        core.set_workspace([d1, d2])
        wdlg = gui.WorkspaceDialog(frame)
        assert wdlg.listbox.count() == 2
        wdlg.listbox.setCurrentRow(0)
        wdlg.on_remove()
        assert core.get_workspace() == [d2], core.get_workspace()
        print("WorkspaceDialog on_remove persists ok")
    finally:
        core.set_workspace([])
        shutil.rmtree(d1, ignore_errors=True); shutil.rmtree(d2, ignore_errors=True)

    # ── Reload config ────────────────────────────────────────────────────────
    frame.on_reload_config()
    assert "[reload]" in frame.output.toPlainText()
    print("Reload config ok")

    # ── run path: worker thread -> signals -> _done (the core port contract) ──
    def fake_run(task, emit=None, on_token=None, images=None):
        emit("trace: thinking")
        on_token("ANSWER")
        return "FINAL-RESULT"
    core.run = fake_run
    frame.output.setPlainText("")
    frame.input.setPlainText("do a thing")
    frame.on_send()
    assert not frame.send_btn.isEnabled()           # disabled while working
    deadline = time.time() + 10
    while time.time() < deadline and "FINAL-RESULT" not in frame.output.toPlainText():
        app.processEvents()
        time.sleep(0.02)
    txt = frame.output.toPlainText()
    assert "trace: thinking" in txt and "ANSWER" in txt and "FINAL-RESULT" in txt, txt
    assert frame.send_btn.isEnabled() and not frame.stop_btn.isEnabled()
    print("run path ok: worker streamed via signals, buttons reset")

    # ── Stop -> core.request_cancel ──────────────────────────────────────────
    cancelled = {"v": False}
    core.request_cancel = lambda: cancelled.update(v=True)
    frame.stop_btn.setEnabled(True)
    frame.on_stop()
    assert cancelled["v"] and not frame.stop_btn.isEnabled()
    print("Stop -> request_cancel ok")

    # ── HITL confirm: worker-thread blocking bridge via signal + Event ───────
    # The confirm now uses a resizable Allow/Edit/Deny dialog returning a dict
    # {decision, args, remember}; stub the dialog's exec to auto-allow.
    gui._ToolConfirmDialog.exec = lambda self: (setattr(self, "_decision", "allow")
                                                or 1)
    payload = {"tool": "tool_x", "args": {"code": "print(1)"},
               "done": threading.Event(), "res": {"decision": "deny"}}
    frame._show_confirm(payload)
    assert payload["res"]["decision"] == "allow" and payload["done"].is_set()
    # and the worker-side _confirm_tool(tool_name, args) round-trips the dict
    got = {}
    t = threading.Thread(
        target=lambda: got.update(res=frame._confirm_tool("tool_x", {"code": "x"})))
    t.start()
    while t.is_alive():
        app.processEvents(); time.sleep(0.01)
    t.join()
    assert got["res"]["decision"] == "allow"
    print("HITL confirm bridge ok (signal + threading.Event, dict contract)")

    # ── HITL review: _ReviewDialog outcomes + _show_review marshaling ────────
    r = gui._ReviewDialog(frame, "review?", "ORIGINAL")
    r._approve(); assert r.outcome()["decision"] == "approve"
    r2 = gui._ReviewDialog(frame, "review?", "ORIGINAL")
    r2.text.setPlainText("EDITED"); r2._approve()
    assert r2.outcome() == {"decision": "edit", "content": "EDITED", "feedback": ""}
    r3 = gui._ReviewDialog(frame, "review?", "ORIGINAL")
    r3.feedback.setText("nope"); r3._reject()
    assert r3.outcome()["decision"] == "reject" and r3.outcome()["feedback"] == "nope"
    gui._ReviewDialog.exec = lambda self: setattr(self, "_decision", "reject") or 1
    rp = {"prompt": "p", "content": "C", "done": threading.Event(),
          "res": {"decision": "approve", "content": "C", "feedback": ""}}
    frame._show_review(rp)
    assert rp["res"]["decision"] == "reject" and rp["done"].is_set()
    # route mode: the dialog shows a BUTTON PER BRANCH and returns the chosen branch
    from PySide6.QtWidgets import QPushButton
    rr = gui._ReviewDialog(frame, "pick", "DRAFT", choices=["send", "reviser", "stop"])
    assert {b.text() for b in rr.findChildren(QPushButton)} == {"send", "reviser", "stop"}
    rr._pick("reviser")
    assert rr.outcome() == {"decision": "reviser", "content": "DRAFT", "feedback": ""}
    print("HITL review dialog + marshaling ok (gate + route branch buttons)")

    # ── history replay ───────────────────────────────────────────────────────
    core.clear_history()
    core.HISTORY.extend([{"role": "user", "content": "earlier question"},
                         {"role": "assistant", "content": "earlier answer"}])
    core.save_history()
    frame2 = gui.ChatFrame()
    assert "earlier question" in frame2.output.toPlainText()
    core.clear_history()
    frame2.close(); frame.close(); app.processEvents()
    print("history replay ok")

    # ── vision agent: attach widgets + drag-drop fix ─────────────────────────
    # Runs in a CLEAN subprocess: `import agent`/`import gui` are already bound
    # to the main agent's dir in this process, so the vision agent needs its own
    # interpreter to import its own gui.py/agent.py.
    import subprocess
    gv = Graph()
    va = gv.new_node("agent", 0, 0); va.name = "seer"
    vl = gv.new_node("llm", 0, 0); vl.props.update({**LLM, "vision": True})
    gv.add_edge(vl.id, va.id)
    vis_out = _gen(gv, "smoke_gui_vision")
    vischeck = os.path.join(vis_out, "_vischeck.py")
    with open(vischeck, "w", encoding="utf-8") as f:
        f.write(
            "import os, sys\n"
            "os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')\n"
            "sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))\n"
            "from PySide6.QtCore import QUrl\n"
            "from PySide6.QtWidgets import QApplication\n"
            "import agent as core, gui\n"
            "app = QApplication.instance() or QApplication([])\n"
            "f = gui.ChatFrame()\n"
            "assert f._vision is True\n"
            "assert hasattr(f, 'attach_btn') and hasattr(f, 'attach_lbl')\n"
            "assert f.output.acceptDrops() is False and f.input.acceptDrops() is False\n"
            "assert f.acceptDrops() is True\n"
            "f._add_attachments(['a.png', 'b.png'])\n"
            "assert '2 image(s)' in f.attach_lbl.text()\n"
            "class M:\n"
            "    def __init__(s, ps): s._u = [QUrl.fromLocalFile(p) for p in ps]\n"
            "    def hasUrls(s): return True\n"
            "    def urls(s): return s._u\n"
            "class D:\n"
            "    def __init__(s, ps): s._m = M(ps)\n"
            "    def mimeData(s): return s._m\n"
            "    def acceptProposedAction(s): pass\n"
            "f._attachments = []\n"
            "f.dropEvent(D(['pic.png', 'notes.txt']))\n"
            "assert f._attachments == ['pic.png'], f._attachments\n"
            "print('VISION_OK')\n")
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen"}
    r = subprocess.run([sys.executable, "_vischeck.py"], cwd=vis_out,
                       capture_output=True, text=True, env=env, timeout=60)
    assert "VISION_OK" in r.stdout, (r.stdout, r.stderr)
    print("vision attach + drag-drop (fixed) ok")

    print("GENERATED GUI SMOKE TEST PASSED")
finally:
    os.chdir(BASE)
    shutil.rmtree(out, ignore_errors=True)
    if vis_out:
        shutil.rmtree(vis_out, ignore_errors=True)
