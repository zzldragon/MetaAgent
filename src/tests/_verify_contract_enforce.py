"""Verify ENFORCED link contracts (opt-in per edge): when a link's contract has
'validate' on, the producer's output is checked as JSON against the declared
fields; a mismatch re-runs the producer (bounded), and if it still fails the run
STOPS with '[contract not met]'. Off (default) = advisory prompt-only, unchanged.
Covers both run loops (chain=run_pipeline, graph=run_graph) + the validators."""

import importlib.util
import json
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
import patterns

LLM = {"provider": "siliconflow", "model": "deepseek-ai/DeepSeek-V4-Flash",
       "api_key": "sk-x", "base_url": "https://api.siliconflow.cn/v1"}


def _llm(g, a):
    n = g.new_node("llm", a.x - 150, a.y + 120); n.props.update(LLM); g.add_edge(n.id, a.id)


def _load(g, name):
    out = gc.generate_from_graph(g, name, gui=False)
    py_compile.compile(os.path.join(out, "agent.py"), doraise=True)
    cfg = json.load(open(os.path.join(out, "config.json"), encoding="utf-8"))
    sp = importlib.util.spec_from_file_location(name, os.path.join(out, "agent.py"))
    m = importlib.util.module_from_spec(sp); sp.loader.exec_module(m)
    return m, out, cfg


def _chain(enforce, retries=2):
    g = gm.Graph()
    p = g.new_node("agent", 0, 0); p.name = "planner"; _llm(g, p)
    e = g.new_node("agent", 300, 0); e.name = "executor"; _llm(g, e)
    g.add_edge(p.id, e.id)
    ed = g.edges[-1]
    ed.props["contract"] = [{"name": "plan", "type": "list", "description": "steps"}]
    ed.props["contract_enforce"] = enforce
    ed.props["contract_max_retries"] = retries
    return _load(g, "ce_chain")


# 0. validator unit
m, out, _ = _chain(False)
F = [{"name": "plan", "type": "list"}, {"name": "n", "type": "int"}]
assert m._validate_contract(F, '{"plan": ["a"], "n": 3}')[0] is True
assert m._validate_contract(F, 'no json here')[0] is False
assert not m._validate_contract(F, '{"plan": ["a"]}')[0]              # missing n
assert not m._validate_contract(F, '{"plan": "x", "n": 3}')[0]        # plan wrong type
assert m._validate_contract(F, '```json\n{"plan": [], "n": 1}\n```')[0]  # fenced ok
shutil.rmtree(out, ignore_errors=True)
print("0. _validate_contract: valid / not-json / missing / wrong-type / fenced ok")

# 1. chain ENFORCED: invalid -> retry -> valid JSON reaches the downstream agent
m, out, cfg = _chain(True)
assert m.CONTRACTS_OUT.get("planner", {}).get("fields") == [{"name": "plan", "type": "list"}]
calls = {"planner": 0}; seen = {}
def _s1(name, c, sysm, msgs):
    if name == "planner":
        calls["planner"] += 1
        return ("here's my plan", []) if calls["planner"] == 1 else ('{"plan": ["A","B"]}', [])
    seen["q"] = msgs[-1]["content"]; return ("done", [])
m._call_one = _s1; m.clear_history()
res = m.run("plan it", emit=lambda s: None)
assert calls["planner"] == 2 and res == "done", (calls, res)
assert '"plan"' in seen["q"], seen["q"][-80:]
shutil.rmtree(out, ignore_errors=True)
print("1. chain enforced: invalid->retry->valid JSON reaches downstream ok")

# 2. chain ENFORCED, never valid -> stop with [contract not met] after 1+retries
m, out, _ = _chain(True, retries=2)
n = {"c": 0}
m._call_one = lambda name, c, sysm, msgs: (
    (n.__setitem__("c", n["c"] + 1) or ("nope", [])) if name == "planner" else ("done", []))
m.clear_history()
res = m.run("plan", emit=lambda s: None)
assert res.startswith("[contract not met]") and n["c"] == 3, (res[:60], n)
shutil.rmtree(out, ignore_errors=True)
print("2. chain enforced, never valid: stops after 1+2 attempts ok")

# 3. NOT enforced (default): advisory only — no validation, no retry
m, out, _ = _chain(False)
assert "planner" not in m.CONTRACTS_OUT
c3 = {"n": 0}
m._call_one = lambda name, c, sysm, msgs: (c3.__setitem__("n", c3["n"] + 1) or ("prose", []))
m.clear_history(); m.run("plan", emit=lambda s: None)
assert c3["n"] == 2, c3            # planner + executor, no retry
shutil.rmtree(out, ignore_errors=True)
print("3. not enforced: advisory only, no retry ok")

# 4. GRAPH mode (run_graph): enforced contract on producer -> router also retries
g = gm.Graph()
p = g.new_node("agent", 0, 0); p.name = "producer"; _llm(g, p)
r = g.new_node("router", 250, 0); r.name = "route"; _llm(g, r)
x = g.new_node("agent", 500, -80); x.name = "x"; _llm(g, x)
y = g.new_node("agent", 500, 80); y.name = "y"; _llm(g, y)
g.add_edge(p.id, r.id); g.add_edge(r.id, x.id); g.add_edge(r.id, y.id)
pe = next(e for e in g.edges if e.src == p.id and e.dst == r.id)
pe.props["contract"] = [{"name": "topic", "type": "str", "description": "t"}]
pe.props["contract_enforce"] = True
pe.props["contract_max_retries"] = 2
m, out, _ = _load(g, "ce_graph")
assert m.PATTERN_MODE == "graph" and m.CONTRACTS_OUT.get("producer")
gc_calls = {"producer": 0}
def _s4(name, c, sysm, msgs):
    if name == "producer":
        gc_calls["producer"] += 1
        return ("free text", []) if gc_calls["producer"] == 1 else ('{"topic": "billing"}', [])
    if name == "route":
        return ("x", [])            # router picks branch x
    return ("handled", [])
m._call_one = _s4; m.clear_history()
res = m.run("hi", emit=lambda s: None)
assert gc_calls["producer"] == 2, ("run_graph should retry the producer too", gc_calls)
shutil.rmtree(out, ignore_errors=True)
print("4. graph mode: enforced producer contract retries in run_graph ok")

# 5. dialog round-trips enforce + max_retries (and won't enforce with no fields)
from PySide6.QtWidgets import QApplication
QApplication.instance() or QApplication([])
from canvas_qt.dialogs import EdgeContractDialog
g = gm.Graph(); a = g.new_node("agent", 0, 0); a.name = "A"
b = g.new_node("agent", 1, 0); b.name = "B"; g.add_edge(a.id, b.id)
ed = g.edges[0]
d = EdgeContractDialog(None, ed, g)
d._fields = [{"name": "x", "type": "int", "description": ""}]
d.enforce.setChecked(True); d.max_retries.setText("3")
assert d.apply() is None
assert ed.props["contract_enforce"] is True and ed.props["contract_max_retries"] == 3
d2 = EdgeContractDialog(None, ed, g); d2._fields = []          # no fields -> can't enforce
d2.enforce.setChecked(True); assert d2.apply() is None
assert ed.props["contract_enforce"] is False
print("5. dialog round-trips enforce + max_retries; no-fields can't enforce ok")

# 6. A HITL review gate on an ENFORCED handoff must NOT downgrade it to advisory.
#    producer -> review -> consumer is spliced to producer -> consumer at gen time;
#    the enforce flags (not just the fields) must ride onto the spliced edge, else
#    runtime JSON validation silently vanishes behind the gate.
g = gm.Graph()
p = g.new_node("agent", 0, 0); p.name = "producer"; _llm(g, p)
h = g.new_node("hitl", 250, 0); h.name = "review"
c = g.new_node("agent", 500, 0); c.name = "consumer"; _llm(g, c)
g.add_edge(p.id, h.id); g.add_edge(h.id, c.id)
ph = next(e for e in g.edges if e.src == p.id and e.dst == h.id)
ph.props["contract"] = [{"name": "draft", "type": "str", "description": "the draft"}]
ph.props["contract_enforce"] = True
ph.props["contract_max_retries"] = 4
m, out, _ = _load(g, "ce_hitl")
co = m.CONTRACTS_OUT.get("producer") or {}
assert co.get("fields") == [{"name": "draft", "type": "str"}], co
assert co.get("max_retries") == 4, co                       # flags survived the splice
assert "(ENFORCED)" in m.AGENTS["producer"]["system"], "downgraded to advisory across HITL gate"
shutil.rmtree(out, ignore_errors=True)
print("6. HITL gate preserves an enforced contract (no advisory downgrade) ok")

# 7. A producer with SEVERAL enforced out-edges (a supervisor delegating to two
#    workers) validates against the UNION of their fields in one pass, so the most
#    generous max_retries must win — not just the first edge's (order-dependent).
g = patterns.build_pattern_graph("supervisor_worker", LLM)
sup = next(n for n in g.agents() if n.props.get("role") == "supervisor")
w1 = next(n for n in g.agents() if n.props.get("role") == "worker")
w2 = g.new_node("agent", w1.x, w1.y + 200); w2.name = "worker2"
w2.props.update(role="worker", max_iterations=6, max_tool_calls=6,
                max_output_tokens=2000, max_wall_clock_s=30)
_llm(g, w2)
pr = g.new_node("prompt", w2.x - 200, w2.y + 90); pr.props["text"] = "You are worker2."
g.add_edge(pr.id, w2.id); g.add_edge(sup.id, w2.id)
e1 = next(e for e in g.edges if e.src == sup.id and e.dst == w1.id)
e2 = next(e for e in g.edges if e.src == sup.id and e.dst == w2.id)
e1.props.update(contract=[{"name": "a", "type": "str", "description": ""}],
                contract_enforce=True, contract_max_retries=1)
e2.props.update(contract=[{"name": "b", "type": "int", "description": ""}],
                contract_enforce=True, contract_max_retries=5)
m, out, _ = _load(g, "ce_multi")
co = m.CONTRACTS_OUT.get(sup.name) or {}
assert {f["name"] for f in co.get("fields", [])} == {"a", "b"}, co
assert co.get("max_retries") == 5, ("most generous retry budget must win", co)
shutil.rmtree(out, ignore_errors=True)
print("7. multi-edge producer: fields union + most-generous max_retries ok")

print("\nALL CONTRACT-ENFORCEMENT CHECKS PASSED")
