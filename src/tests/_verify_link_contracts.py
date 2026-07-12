"""Verify per-link (edge) data contracts: an agent→agent link can carry a
structured {name,type,description} contract that (1) survives save/load, and
(2) is injected into BOTH endpoints' system prompts at generation — an Output
contract on the producer, an Input contract on the consumer. Also confirms it's
inert on non-agent links and generates/compiles."""

import importlib.util
import json
import os
import py_compile
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.chdir(BASE)

import graph_codegen
import graph_model as gm
from graph_model import contract_fields

LLM = {"provider": "siliconflow", "model": "deepseek-ai/DeepSeek-V4-Flash",
       "api_key": "sk-test", "base_url": "https://api.siliconflow.cn/v1"}


def _agent(g, name, x):
    a = g.new_node("agent", x, 0)
    a.name = name
    a.props.update(max_iterations=6, max_tool_calls=6, max_output_tokens=2000,
                   max_wall_clock_s=30)
    llm = g.new_node("llm", x - 200, 0)
    llm.props.update(LLM)
    assert g.add_edge(llm.id, a.id) is None
    p = g.new_node("prompt", x - 200, 90)
    p.props["text"] = f"You are {name}."
    assert g.add_edge(p.id, a.id) is None
    return a


# 1. A contract survives to_dict → JSON → from_dict, and contract_fields
#    normalizes it (drops bad names, coerces unknown types, dedups).
g = gm.Graph()
a1 = _agent(g, "planner", 0)
a2 = _agent(g, "executor", 400)
assert g.add_edge(a1.id, a2.id) is None
edge = next(e for e in g.edges if e.src == a1.id and e.dst == a2.id)
edge.props["contract"] = [
    {"name": "steps", "type": "list", "description": "numbered plan, best first"},
    {"name": "notes", "type": "str", "description": ""},
    {"name": "bad name", "type": "str"},           # invalid identifier → dropped
    {"name": "steps", "type": "int"},              # duplicate → dropped
    {"name": "weird", "type": "nope"},             # unknown type → coerced to str
]
fields = contract_fields(edge)
assert [f["name"] for f in fields] == ["steps", "notes", "weird"], fields
assert fields[2]["type"] == "str", "unknown type should coerce to str"
reloaded = gm.Graph.from_dict(json.loads(json.dumps(g.to_dict())))
re_edge = next(e for e in reloaded.edges if e.src == a1.id and e.dst == a2.id)
assert contract_fields(re_edge) == fields, "contract lost across save/load"
# an OLD edge dict (no props key) still deserializes
assert gm.Edge(**{"src": a1.id, "dst": a2.id}).props == {}
print("1. contract validation + save/load round-trip + backward-compat ok")

# 2. Codegen injects the contract into BOTH prompts, and generate+compile works.
info = graph_codegen.analyze(g)
assert not info["errors"], info["errors"]
out = graph_codegen.generate_from_graph(g, "demo_contract", gui=False)
py_compile.compile(os.path.join(out, "agent.py"), doraise=True)
spec = importlib.util.spec_from_file_location("demo_contract_agent",
                                              os.path.join(out, "agent.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
psys = mod.AGENTS["planner"]["system"]
esys = mod.AGENTS["executor"]["system"]
assert "Output contract" in psys and "executor" in psys, psys[-400:]
assert "steps (list)" in psys and "numbered plan" in psys
assert "notes (str)" in psys                       # description-less field still listed
assert "Input contract" in esys and "planner" in esys, esys[-400:]
assert "steps (list)" in esys
# the consumer with no contract downstream gets no Output contract block
assert "Output contract" not in esys
print("2. codegen injects Output contract (producer) + Input contract (consumer) ok")

# 3. A contract on a NON-agent link (llm→agent) is inert — no prompt block.
g2 = gm.Graph()
solo = _agent(g2, "solo", 0)
llm_edge = next(e for e in g2.edges if g2.nodes[e.src].kind == "llm"
                and e.dst == solo.id)
llm_edge.props["contract"] = [{"name": "ignored", "type": "str", "description": "x"}]
out2 = graph_codegen.generate_from_graph(g2, "demo_contract_inert", gui=False)
spec2 = importlib.util.spec_from_file_location("demo_contract_inert_agent",
                                               os.path.join(out2, "agent.py"))
mod2 = importlib.util.module_from_spec(spec2)
spec2.loader.exec_module(mod2)
assert "contract" not in mod2.AGENTS["solo"]["system"].lower(), \
    "a contract on a resource link must be ignored"
print("3. contract on a non-agent link is inert ok")

import shutil
shutil.rmtree(out, ignore_errors=True)
shutil.rmtree(out2, ignore_errors=True)


def _gen_agents(g, name):
    o = graph_codegen.generate_from_graph(g, name, gui=False)
    sp = importlib.util.spec_from_file_location(name + "_a", os.path.join(o, "agent.py"))
    m = importlib.util.module_from_spec(sp)
    sp.loader.exec_module(m)
    shutil.rmtree(o, ignore_errors=True)
    return m.AGENTS


# 4. A contract on a ROUTER→worker link is NOT injected — a router forwards the
#    same text and picks a branch; it produces no output to contract about, so
#    both the router's Output block and the worker's false Input block are skipped.
g4 = gm.Graph()
r = g4.new_node("router", 0, 0)
r.name = "router"
llm_r = g4.new_node("llm", -200, 0)
llm_r.props.update(LLM)
assert g4.add_edge(llm_r.id, r.id) is None
w1 = _agent(g4, "w1", 300)
w2 = _agent(g4, "w2", 600)
assert g4.add_edge(r.id, w1.id) is None
assert g4.add_edge(r.id, w2.id) is None
re = next(e for e in g4.edges if e.src == r.id and e.dst == w1.id)
re.props["contract"] = [{"name": "task", "type": "str", "description": "the task"}]
ags = _gen_agents(g4, "demo_router_contract")
assert "Output contract" not in ags["router"]["system"], "router must not get an output contract"
assert "Input contract" not in ags["w1"]["system"], "worker must not get a false input contract"
print("4. contract on a router edge is not injected ok")

# 5. A contract on a handoff bridged by a HITL gate survives the hitl splice —
#    a review gate and a data contract can coexist on the same handoff.
g5 = gm.Graph()
p = _agent(g5, "producer", 0)
h = g5.new_node("hitl", 200, 0)
h.name = "review"
c = _agent(g5, "consumer", 400)
assert g5.add_edge(p.id, h.id) is None
assert g5.add_edge(h.id, c.id) is None
ph = next(e for e in g5.edges if e.src == p.id and e.dst == h.id)
ph.props["contract"] = [{"name": "draft", "type": "str", "description": "the draft"}]
ags = _gen_agents(g5, "demo_hitl_contract")
assert "Output contract" in ags["producer"]["system"] and "draft (str)" in ags["producer"]["system"]
assert "Input contract" in ags["consumer"]["system"] and "draft (str)" in ags["consumer"]["system"]
print("5. contract survives a HITL-gated handoff ok")

print("\nALL LINK-CONTRACT CHECKS PASSED")
