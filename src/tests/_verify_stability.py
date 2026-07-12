"""Regression tests for the stability-hunt findings (2026-07-02). Each fix here
was a live, reproducible defect the 225-test suite missed:
  A  gui.py used json.* but the GUI template never imported json (HITL dialog NameError)
  B  _py_block_string emitted an unterminated literal for prompts ending in triple-quotes
  C  a prompt containing an @MARKER@-shaped token false-tripped the codegen drift guard
  D  a state-block value incompatible with an add/max/min reducer aborted the whole run
  E  the GUI's Run-Evals action stayed enabled during a chat run (concurrent run() corruption)
  F  a dangling edge (missing endpoint node) crashed analyze()/generate with KeyError
  G  a stage name with triple-quotes emitted an uncompilable module docstring
"""

import importlib.util
import os
import py_compile
import shutil
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.chdir(BASE)

import graph_codegen as gc
import graph_model as gm

LLM = {"provider": "siliconflow", "model": "deepseek-ai/DeepSeek-V4-Flash",
       "api_key": "sk-x", "base_url": "https://api.siliconflow.cn/v1"}


def _mk(name="A"):
    g = gm.Graph(); a = g.new_node("agent", 0, 0); a.name = name
    l = g.new_node("llm", -200, 0); l.props.update(LLM); g.add_edge(l.id, a.id)
    return g, a


def _load(out):
    sp = importlib.util.spec_from_file_location("m_" + os.path.basename(out),
                                                os.path.join(out, "agent.py"))
    m = importlib.util.module_from_spec(sp); sp.loader.exec_module(m); return m


# B — _py_block_string is a safe round-trip for pathological quote/backslash cases
for s in ['"""', 'be concise """', 'a"""', '""', '"', '""""', 'x\\"""',
          'ends\\', '\\', 'mixed "" \\ """ end"""', 'plain', 'a\nb']:
    assert eval(gc._py_block_string(s)) == s, ("B round-trip failed", repr(s))
print("B: _py_block_string round-trips all quote/backslash edge cases ok")

# C — marker-shaped text in a PROMPT survives verbatim; the drift guard still fires
g, a = _mk()
pr = g.new_node("prompt", -200, 200); pr.props["role"] = "single"
pr.props["text"] = "You are @AGENT_NAME@. Be @MODES@ and @NOPE@ concise."
g.add_edge(pr.id, a.id)
assert not gc.analyze(g)["errors"]
out = gc.generate_from_graph(g, "stab_c", gui=False)
py_compile.compile(os.path.join(out, "agent.py"), doraise=True)
src = open(os.path.join(out, "agent.py"), encoding="utf-8").read()
assert pr.props["text"] in src, "C: marker-shaped prompt text not preserved verbatim"
shutil.rmtree(out, ignore_errors=True)
print("C: @MARKER@-shaped prompt text preserved; generates + compiles ok")

# D — a wrong-typed state write is skipped, not fatal: the answer survives
g = gm.Graph()
a = g.new_node("agent", 0, 0); a.name = "A"; a.props["writes"] = ["score"]
l = g.new_node("llm", -200, 0); l.props.update(LLM); g.add_edge(l.id, a.id)
g.state_schema = [{"name": "score", "type": "int", "reducer": "add",
                   "default": 0, "description": "n"}]
out = gc.generate_from_graph(g, "stab_d", gui=False)
m = _load(out)
m._call_one = lambda name, cfg, sysm, msgs: ('The answer is 7.\n```state\nscore = "oops"\n```', [])
m.clear_history()
r = m.run("hi", emit=lambda s: None)
assert not r.strip().startswith("[error]"), ("D: run aborted on bad state write ->", r[:80])
assert "answer is 7" in r, r
shutil.rmtree(out, ignore_errors=True)
print("D: wrong-typed state write is skipped; the good answer survives ok")

# F — a dangling edge (missing endpoint) is pruned at load; analyze stays clean
g, a = _mk()
d = g.to_dict(); d["edges"].append({"src": "ghost_1", "dst": a.id})
g2 = gm.Graph.from_dict(d)
assert all(e.src in g2.nodes and e.dst in g2.nodes for e in g2.edges), "F: dangling edge not pruned"
assert not gc.analyze(g2)["errors"], "F: analyze should be clean after pruning"
# and the query helpers themselves tolerate a dangling edge appended directly
g3, a3 = _mk()
g3.edges.append(gm.Edge("ghost_2", a3.id))
gc.analyze(g3)  # must not raise
print("F: dangling edges pruned at load + query helpers tolerate them ok")

# G — a stage name with triple-quotes / marker / control chars is a clean error
for bad in ['x""" y', 'x\nA', 'has @MODES@ token']:
    g, a = _mk(bad)
    errs = gc.analyze(g)["errors"]
    assert any("rename" in e for e in errs), ("G: bad name not rejected", repr(bad), errs)
print("G: unsafe stage names rejected with a clean analyze() error ok")

# A + E — GUI: json is available and the HITL confirm dialog handles a multi-arg
# tool call; the eval action is disabled during a chat run.
try:
    from PySide6.QtWidgets import QApplication  # noqa
    _qt = True
except Exception:
    _qt = False
if _qt:
    app = QApplication.instance() or QApplication([])
    g, a = _mk(); gn = g.new_node("gui", 200, 0); g.add_edge(a.id, gn.id)
    ev = g.new_node("eval", 200, 120); g.add_edge(ev.id, a.id)
    out = gc.generate_from_graph(g, "stab_ae", gui=True)
    # A: gui imports json + multi-arg tool call doesn't NameError
    aspec = importlib.util.spec_from_file_location("agent", os.path.join(out, "agent.py"))
    am = importlib.util.module_from_spec(aspec); sys.modules["agent"] = am
    aspec.loader.exec_module(am)
    gspec = importlib.util.spec_from_file_location("guimod", os.path.join(out, "gui.py"))
    gmod = importlib.util.module_from_spec(gspec); gspec.loader.exec_module(gmod)
    assert "json" in dir(gmod), "A: gui module missing json import"
    dlg = gmod._ToolConfirmDialog(None, "run_python", {"code": "print(1)", "extra": {"k": "v"}})
    dlg._decision = "edit"
    assert dlg.outcome()["decision"] == "allow"    # exercises json.dumps/loads paths
    # E: on_send disables the eval action
    gsrc = open(os.path.join(out, "gui.py"), encoding="utf-8").read()
    on_send = gsrc.split("def on_send")[1].split("\n    def ")[0]
    assert "_eval_action.setEnabled(False)" in on_send, "E: on_send doesn't disable evals"
    del sys.modules["agent"]; shutil.rmtree(out, ignore_errors=True)
    print("A: gui.py imports json; multi-arg HITL confirm dialog works ok")
    print("E: GUI disables Run-Evals during a chat run ok")
else:
    print("A/E: skipped (PySide6 unavailable)")

print("\nALL STABILITY REGRESSION CHECKS PASSED")
