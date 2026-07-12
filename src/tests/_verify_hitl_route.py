"""Verify ROUTE-MODE HITL: a HITL node with 2+ outgoing edges becomes a human-driven
branch (the human mirror of a Router) — the reviewer picks WHICH successor runs next,
including branching to End. A 1-outgoing HITL keeps the classic spliced review-gate
(byte-identical: no HITL_NODES table, no 'hitl' stage kind, a review gate on its agent).
Offline — _call_one and the review handler are stubbed."""
import importlib.util
import os
import py_compile
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.chdir(BASE)

import graph_codegen
from graph_model import DEFAULT_BUDGETS, Graph

LLM = dict(provider="siliconflow", model="deepseek-ai/DeepSeek-V4-Flash",
           api_key="", base_url="https://api.siliconflow.cn/v1")


def _agent(g, name, y):
    a = g.new_node("agent", 0, y); a.name = name; a.props["role"] = "single"
    for k in DEFAULT_BUDGETS:
        a.props[k] = DEFAULT_BUDGETS[k]
    return a


def _load(out_dir, modname):
    spec = importlib.util.spec_from_file_location(modname, os.path.join(out_dir, "agent.py"))
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    return mod


# ── 1. route mode: triage -> HITL(approval) -> {ship, rework, stop(End)} ──────
g = Graph()
llm = g.new_node("llm", 400, 0); llm.name = "m"; llm.props.update(LLM)
triage = _agent(g, "triage", -80)
ship = _agent(g, "ship", 80)
rework = _agent(g, "rework", 160)
for n in (triage, ship, rework):
    g.add_edge(llm.id, n.id)
end = g.new_node("end", 0, 240); end.name = "stop"
h = g.new_node("hitl", 0, 0); h.name = "approval"
h.props["prompt"] = "Approve to ship, send back for rework, or stop."
h.props["default_route"] = "rework"
g.add_edge(triage.id, h.id)
g.add_edge(h.id, ship.id)          # branch order: ship, rework, stop
g.add_edge(h.id, rework.id)
g.add_edge(h.id, end.id)

info = graph_codegen.analyze(g)
assert not info["errors"], info["errors"]
assert info["mode"] == "graph", info["mode"]
print("route hitl ok: analyze clean, graph mode")

out = graph_codegen.generate_from_graph(g, "verify_hitl_route", gui=False)
py_compile.compile(os.path.join(out, "agent.py"), doraise=True)
mod = _load(out, "vhr")

# topology tables
assert mod.STAGE_KINDS["approval"] == "hitl", mod.STAGE_KINDS
assert set(mod.SUCCESSORS["approval"]) == {"ship", "rework", "stop"}, mod.SUCCESSORS["approval"]
assert mod.HITL_NODES["approval"]["default_route"] == "rework", mod.HITL_NODES
assert mod.HITL_NODES["approval"]["prompt"].startswith("Approve to ship"), mod.HITL_NODES
assert "approval" not in mod.AGENTS, "a route-mode HITL is NOT an agent spec"
print("route hitl codegen ok: STAGE_KINDS/SUCCESSORS/HITL_NODES emitted")

# the desktop GUI (choices-aware _ReviewDialog) must still compile with route mode
out_gui = graph_codegen.generate_from_graph(g, "verify_hitl_route_gui", gui=True)
py_compile.compile(os.path.join(out_gui, "gui.py"), doraise=True)
print("route hitl gui ok: gui.py (branch-picker review dialog) compiles")

# stub the LLM (record which agents run + the input each saw) + a branch-picking handler
calls = []
seen = {}


def _stub(agent_name, cfg, system, messages):
    calls.append(agent_name)
    seen[agent_name] = "\n".join(m.get("content", "") for m in messages)
    return (f"{agent_name} out", [])


mod._call_one = _stub
pick = {"b": "ship"}
mod.set_review_handler(lambda prompt, content, choices=None: {
    "decision": pick["b"], "content": content})

# 1a. human picks 'ship' -> only ship runs downstream
mod.clear_history(); calls.clear(); seen.clear()
r = mod.run("do it", emit=lambda s: None)
assert "triage" in calls and "ship" in calls, calls
assert "rework" not in calls, calls
assert r == "ship out", r
print("route hitl run ok: human picks 'ship' -> ship runs, rework skipped")

# 1b. human picks 'rework'
pick["b"] = "rework"; mod.clear_history(); calls.clear(); seen.clear()
r = mod.run("do it", emit=lambda s: None)
assert "rework" in calls and "ship" not in calls, calls
print("route hitl run ok: human picks 'rework' -> rework runs, ship skipped")

# 1c. human picks the End branch -> finish early, neither downstream agent runs
pick["b"] = "stop"; mod.clear_history(); calls.clear(); seen.clear()
r = mod.run("do it", emit=lambda s: None)
assert "ship" not in calls and "rework" not in calls, calls
assert r == "triage out", r          # End returns the carried payload unchanged
print("route hitl run ok: human picks End branch -> finish early")

# 1d. out-of-set / legacy decision -> falls back to default_route ('rework')
pick["b"] = "nonsense-not-a-branch"; mod.clear_history(); calls.clear()
r = mod.run("do it", emit=lambda s: None)
assert "rework" in calls and "ship" not in calls, calls
print("route hitl run ok: unknown decision -> default_route branch taken")

# 1e. the reviewer may EDIT the payload the chosen branch receives
mod.set_review_handler(lambda prompt, content, choices=None: {
    "decision": "ship", "content": "HUMAN-EDITED PAYLOAD"})
mod.clear_history(); calls.clear(); seen.clear()
r = mod.run("do it", emit=lambda s: None)
assert "ship" in calls, calls
assert "HUMAN-EDITED PAYLOAD" in seen.get("ship", ""), seen.get("ship")
print("route hitl run ok: reviewer edits payload -> chosen branch sees the edit")

# 1f. (review-fix B) no handler + non-interactive/EOF stdin -> default_route, NOT the
#     first branch. The console fallback must ABSTAIN (return "") so _human_route applies
#     the configured default; returning choices[0] would silently invert an operator's
#     unattended-safety default.
import io  # noqa: E402
mod.set_review_handler(None)                 # force the _console_review fallback
_saved_stdin = sys.stdin
sys.stdin = io.StringIO("")                  # input() -> EOFError -> abstain
try:
    mod.clear_history(); calls.clear()
    r = mod.run("do it", emit=lambda s: None)
finally:
    sys.stdin = _saved_stdin
assert "rework" in calls and "ship" not in calls, calls   # default_route ('rework') taken
print("route hitl run ok: no-handler + EOF stdin -> default_route (not first branch)")

# 1g. (review-fix C) a KEYWORD-ONLY `choices` handler must work, not TypeError-abort
def _kwonly(prompt, content, *, choices=None):
    return {"decision": "ship", "content": content}


mod.set_review_handler(_kwonly)
mod.clear_history(); calls.clear()
r = mod.run("do it", emit=lambda s: None)
assert "ship" in calls and r == "ship out", (calls, r)
print("route hitl run ok: keyword-only choices handler works (no TypeError abort)")

# 1h. (review-fix A) a routing HITL whose NAME collides with an agent is REJECTED by
#     analyze (else it silently shadows the agent in SUCCESSORS/STAGE_KINDS).
gdup = Graph()
_ld = gdup.new_node("llm", 0, 0); _ld.name = "m"; _ld.props.update(LLM)
_A = _agent(gdup, "A", -80); _B = _agent(gdup, "B", 40); _C = _agent(gdup, "C", 120)
for n in (_A, _B, _C):
    gdup.add_edge(_ld.id, n.id)
_hd = gdup.new_node("hitl", 0, 0); _hd.name = "B"      # collides with agent B
gdup.add_edge(_A.id, _hd.id); gdup.add_edge(_hd.id, _B.id); gdup.add_edge(_hd.id, _C.id)
_idup = graph_codegen.analyze(gdup)
assert any("unique" in e.lower() for e in _idup["errors"]), _idup["errors"]
print("route hitl analyze ok: name collision with an agent is rejected")

# 1i. (review-fix A) a routing HITL named like a @MARKER@ is REJECTED (else codegen
#     crashes on assert_substituted or silently corrupts the emitted tables).
gmk = Graph()
_lm = gmk.new_node("llm", 0, 0); _lm.name = "m"; _lm.props.update(LLM)
_A2 = _agent(gmk, "A", -80); _S = _agent(gmk, "ship", 40); _R = _agent(gmk, "rework", 120)
for n in (_A2, _S, _R):
    gmk.add_edge(_lm.id, n.id)
_hm = gmk.new_node("hitl", 0, 0); _hm.name = "@AGENT_NAME@"
gmk.add_edge(_A2.id, _hm.id); gmk.add_edge(_hm.id, _S.id); gmk.add_edge(_hm.id, _R.id)
_imk = graph_codegen.analyze(gmk)
assert any("marker" in e.lower() for e in _imk["errors"]), _imk["errors"]
print("route hitl analyze ok: @MARKER@-shaped name is rejected")

# ── 2. gate mode preserved: triage -> HITL(1 out) -> finalize (byte-identical) ─
g2 = Graph()
llm2 = g2.new_node("llm", 400, 0); llm2.name = "m"; llm2.props.update(LLM)
t2 = _agent(g2, "triage", -80)
f2 = _agent(g2, "finalize", 80)
for n in (t2, f2):
    g2.add_edge(llm2.id, n.id)
h2 = g2.new_node("hitl", 0, 0); h2.name = "gate"
g2.add_edge(t2.id, h2.id)
g2.add_edge(h2.id, f2.id)          # exactly ONE outgoing -> classic gate

info2 = graph_codegen.analyze(g2)
assert not info2["errors"], info2["errors"]
out2 = graph_codegen.generate_from_graph(g2, "verify_hitl_gate", gui=False)
mod2 = _load(out2, "vhg")
assert mod2.HITL_NODES == {}, mod2.HITL_NODES               # no routing table
assert "hitl" not in mod2.STAGE_KINDS.values(), mod2.STAGE_KINDS
assert mod2.AGENTS["finalize"].get("review"), "1-out HITL spliced onto its agent"
assert mod2.AGENTS["finalize"]["review"]["node"] == "gate"
print("gate hitl ok: 1-outgoing HITL still spliced to a review gate (byte-identical)")

# ── 3. package code style: route-mode HITL compiles + carries HITL_NODES/_human_route
#     (exec'ing a generated PACKAGE in-process clashes with the repo's own runtime/
#      package, so validate by compile + source text — same as other package checks.)
out_pkg = graph_codegen.generate_from_graph(g, "verify_hitl_route_pkg", gui=False,
                                            code_style="package")
py_compile.compile(os.path.join(out_pkg, "agent.py"), doraise=True)
py_compile.compile(os.path.join(out_pkg, "runtime", "hitl.py"), doraise=True)
py_compile.compile(os.path.join(out_pkg, "runtime", "_core.py"), doraise=True)
with open(os.path.join(out_pkg, "agent.py"), encoding="utf-8") as _f:
    _asrc = _f.read()
with open(os.path.join(out_pkg, "runtime", "_core.py"), encoding="utf-8") as _f:
    _csrc = _f.read()
# run_graph + the route runner live inline in agent.py; the topology table (like
# FANOUT/JOIN) lands in runtime/_core.py, re-exported via `from runtime._core import *`.
assert "def _human_route(" in _asrc and 'kind == "hitl"' in _asrc, "route arm in agent.py"
assert "HITL_NODES = {" in _csrc and "'approval'" in _csrc, "HITL_NODES table in _core.py"
print("package hitl ok: route-mode HITL compiles + wires in package code style")

print("\nALL HITL-ROUTE CHECKS PASSED")
