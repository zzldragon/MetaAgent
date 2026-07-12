"""Verify the router/conditional node: graph rules, graph-mode analysis,
codegen, and runtime branch selection (stubbed LLM — no network)."""

import importlib.util
import os
import py_compile
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.chdir(BASE)

import graph_codegen
from graph_model import Graph

LLM = {"provider": "siliconflow", "model": "deepseek-ai/DeepSeek-V4-Flash",
       "api_key": "sk-test", "base_url": "https://api.siliconflow.cn/v1"}


def _stage(g, name, kind="agent", **props):
    n = g.new_node(kind, 0, 0)
    n.name = name
    n.props.update(props)
    llm = g.new_node("llm", 0, 0)
    llm.props.update(LLM)
    assert g.add_edge(llm.id, n.id) is None
    return n


# 1. router -> {billing, tech}; router is the entry
g = Graph()
router = _stage(g, "triage", kind="router",
                instructions="Route billing questions to billing, technical "
                             "ones to tech.")
billing = _stage(g, "billing")
billing.props["role"] = "single"
tech = _stage(g, "tech")
assert g.add_edge(router.id, billing.id) is None
assert g.add_edge(router.id, tech.id) is None, "router must allow 2+ branches"

info = graph_codegen.analyze(g)
assert not info["errors"], info["errors"]
assert info["mode"] == "graph", info["mode"]
assert g.nodes[info["entry"]].name == "triage"
names = {g.nodes[a].name for a in info["pipeline"]}
assert names == {"triage", "billing", "tech"}, names
print("router graph rules ok: entry=triage, graph mode, 3 stages")

# a non-router agent still may not branch
bad = Graph()
a = _stage(bad, "a"); b = _stage(bad, "b"); c = _stage(bad, "c")
bad.add_edge(a.id, b.id); bad.add_edge(a.id, c.id)
assert any("may branch" in e
           for e in graph_codegen.analyze(bad)["errors"])
print("branch rule ok: plain agent can't fan out")

# router with no branches is rejected
lonely = Graph()
r = _stage(lonely, "r", kind="router")
errs = graph_codegen.analyze(lonely)["errors"]
assert any("at least one outgoing" in e for e in errs), errs
print("router validation ok: needs >=1 branch")

# 2. generate + inspect
out_dir = graph_codegen.generate_from_graph(g, "demo_router", gui=False)
py_compile.compile(os.path.join(out_dir, "agent.py"), doraise=True)
spec = importlib.util.spec_from_file_location(
    "demo_router_agent", os.path.join(out_dir, "agent.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
assert mod.PATTERN_MODE == "graph"
assert mod.ENTRY == "triage"
assert mod.STAGE_KINDS["triage"] == "router"
assert set(mod.SUCCESSORS["triage"]) == {"billing", "tech"}
assert "Route billing" in mod.AGENTS["triage"]["system"]
print("router codegen ok: graph mode, SUCCESSORS, STAGE_KINDS, ENTRY")

# 3. _match_route
assert mod._match_route("I think tech", ["billing", "tech"]) == "tech"
assert mod._match_route("definitely BILLING here", ["billing", "tech"]) == "billing"
assert mod._match_route("no idea", ["billing", "tech"]) == "billing"  # fallback
print("route matching ok")

# 4. runtime: router picks a branch; only that agent runs
calls = []
def stub(agent_name, cfg, system, messages):
    calls.append(agent_name)
    if agent_name == "triage":
        return "route to tech", []          # the router's decision
    return f"handled by {agent_name}", []   # the chosen agent's answer
mod._call_one = stub
mod.clear_history()
result = mod.run("my screen is broken", emit=lambda s: None)
assert result == "handled by tech", result
assert "triage" in calls and "tech" in calls and "billing" not in calls, calls
print("router runtime ok: triage -> tech ran, billing skipped")

# route the other way
calls.clear()
def stub2(agent_name, cfg, system, messages):
    calls.append(agent_name)
    if agent_name == "triage":
        return "billing please", []
    return f"handled by {agent_name}", []
mod._call_one = stub2
mod.clear_history()
assert mod.run("charge on my card?", emit=lambda s: None) == "handled by billing"
assert "tech" not in calls, calls
mod.clear_history()
print("router runtime ok: triage -> billing ran, tech skipped")

# 5. Router INSIDE a PEC graph: the critic→planner revise loop still works
g2 = Graph()
planner = _stage(g2, "planner", role="planner")
rt = _stage(g2, "rt", kind="router")
exec_a = _stage(g2, "exec_a", role="worker")
exec_b = _stage(g2, "exec_b", role="worker")
critic = _stage(g2, "critic", role="critic")
g2.add_edge(planner.id, rt.id)
g2.add_edge(rt.id, exec_a.id)
g2.add_edge(rt.id, exec_b.id)
g2.add_edge(exec_a.id, critic.id)
g2.add_edge(exec_b.id, critic.id)
g2.add_edge(critic.id, planner.id)        # revise back-edge

info2 = graph_codegen.analyze(g2)
assert not info2["errors"], info2["errors"]
assert info2["mode"] == "graph"
# the revise edge is detected even in graph mode (fixes the blue-line bug:
# the canvas dashes whatever analyze() reports as revise_edge)
assert info2["revise_edge"] == (critic.id, planner.id), info2["revise_edge"]
print("pec+router revise edge ok: detected (critic -> planner), canvas will dash it")

out2 = graph_codegen.generate_from_graph(g2, "demo_pec_router", gui=False)
py_compile.compile(os.path.join(out2, "agent.py"), doraise=True)
spec2 = importlib.util.spec_from_file_location(
    "demo_pec_router_agent", os.path.join(out2, "agent.py"))
m2 = importlib.util.module_from_spec(spec2)
spec2.loader.exec_module(m2)
assert m2.PATTERN_MODE == "graph"
assert m2.REVISE_EDGE == ["critic", "planner"], m2.REVISE_EDGE
assert m2.SUCCESSORS["critic"] == [], m2.SUCCESSORS["critic"]  # back-edge excluded
assert "REVISE" in m2.AGENTS["critic"]["system"]
print("pec+router codegen ok: REVISE_EDGE set, critic terminal, REVISE persona")

# runtime: plan → route → exec → critic REVISEs once → loop to planner → done
critic_calls = {"n": 0}
def pec_stub(agent_name, cfg, system, messages):
    if agent_name == "planner":
        return "plan: 1. do the thing", []
    if agent_name == "rt":
        return "exec_a", []
    if agent_name == "exec_a":
        return "executed the plan", []
    if agent_name == "critic":
        critic_calls["n"] += 1
        if critic_calls["n"] == 1:
            return "REVISE: add more detail", []
        return "final polished answer", []
    return "?", []
m2._call_one = pec_stub
m2.clear_history()
res = m2.run("do a task", emit=lambda s: None)
assert res == "final polished answer", res
assert critic_calls["n"] == 2, critic_calls   # revised once, then accepted
print("pec+router runtime ok: critic REVISE looped back to planner, then done")

# 6. Self-routing planner with quick_response: the planner may decline to hand
#    off (route_to __none__) and end the run with its own answer — skipping the
#    workers/critic for trivial input (a greeting, a directly-answerable question).
g3 = Graph()
planner3 = _stage(g3, "planner", role="planner", route_self=True,
                  quick_response=True)
wa = _stage(g3, "wa", role="worker")
wb = _stage(g3, "wb", role="worker")
crit = _stage(g3, "crit", role="critic")
g3.add_edge(planner3.id, wa.id)
g3.add_edge(planner3.id, wb.id)
g3.add_edge(wa.id, crit.id)
g3.add_edge(wb.id, crit.id)

info3 = graph_codegen.analyze(g3)
assert not info3["errors"], info3["errors"]
assert info3["mode"] == "graph", info3["mode"]

out3 = graph_codegen.generate_from_graph(g3, "demo_quick", gui=False)
py_compile.compile(os.path.join(out3, "agent.py"), doraise=True)
spec3 = importlib.util.spec_from_file_location(
    "demo_quick_agent", os.path.join(out3, "agent.py"))
m3 = importlib.util.module_from_spec(spec3)
spec3.loader.exec_module(m3)
assert m3.AGENTS["planner"].get("quick_response") is True
assert "__none__" in m3.AGENTS["planner"]["system"], m3.AGENTS["planner"]["system"][-300:]
# the route_to schema gains the sentinel only when quick is on
en_on = m3._route_tool_schema(["wa", "wb"], True)["parameters"]["properties"]["agent"]["enum"]
en_off = m3._route_tool_schema(["wa", "wb"], False)["parameters"]["properties"]["agent"]["enum"]
assert "__none__" in en_on and "__none__" not in en_off, (en_on, en_off)
print("quick_response codegen ok: flag + sentinel in prompt/schema")

# runtime (tool path): planner declines via route_to -> ends with planner's answer
seen = []
def quick_stub(agent_name, cfg, system, messages):
    seen.append(agent_name)
    if agent_name == "planner":
        return ("Hi! How can I help?",
                [{"name": "route_to", "args": {"agent": "__none__"}}])
    return (f"handled by {agent_name}", [])
m3._call_one = quick_stub
m3.clear_history()
assert m3.run("hi", emit=lambda s: None) == "Hi! How can I help?"
assert seen == ["planner"], seen          # no worker, no critic ran
print("quick_response runtime ok (tool): planner answered directly, workers skipped")

# runtime (text fallback): a final 'ROUTE: __none__' line means the same
seen.clear()
def quick_text_stub(agent_name, cfg, system, messages):
    seen.append(agent_name)
    if agent_name == "planner":
        return ("Hello there!\nROUTE: __none__", [])
    return (f"handled by {agent_name}", [])
m3._call_one = quick_text_stub
m3.clear_history()
assert m3.run("hi again", emit=lambda s: None) == "Hello there!"   # ROUTE line stripped
assert seen == ["planner"], seen
print("quick_response runtime ok (text): 'ROUTE: __none__' answered directly")

# runtime (the real 'Hi' case): planner just greets — NO route_to call, NO ROUTE
# line, names no branch — so it falls through to a direct answer, NOT the first
# worker. This is the bug the feature must fix.
seen.clear()
def quick_greet_stub(agent_name, cfg, system, messages):
    seen.append(agent_name)
    if agent_name == "planner":
        return ("Hi! How can I help you today?", [])   # no routing decision at all
    return (f"handled by {agent_name}", [])
m3._call_one = quick_greet_stub
m3.clear_history()
assert m3.run("Hi", emit=lambda s: None) == "Hi! How can I help you today?"
assert seen == ["planner"], seen          # NOT routed to the first worker
print("quick_response runtime ok (greet): no branch named -> direct answer, not wa")

# a real route still runs the worker -> critic chain
seen.clear()
def quick_route_stub(agent_name, cfg, system, messages):
    seen.append(agent_name)
    if agent_name == "planner":
        return ("plan: do it", [{"name": "route_to", "args": {"agent": "wa"}}])
    if agent_name == "crit":
        return ("final answer", [])
    return ("did the work", [])
m3._call_one = quick_route_stub
m3.clear_history()
assert m3.run("a real task", emit=lambda s: None) == "final answer"
assert "wa" in seen and "crit" in seen and "wb" not in seen, seen
print("quick_response runtime ok: a real route still runs worker -> critic")

# default (quick_response off): '__none__' is not a sentinel — _self_route still
# falls back to the first worker, so existing graphs are unchanged.
g4 = Graph()
planner4 = _stage(g4, "planner", role="planner", route_self=True)
wc = _stage(g4, "wc", role="worker")
wd = _stage(g4, "wd", role="worker")
g4.add_edge(planner4.id, wc.id)
g4.add_edge(planner4.id, wd.id)
out4 = graph_codegen.generate_from_graph(g4, "demo_noquick", gui=False)
spec4 = importlib.util.spec_from_file_location(
    "demo_noquick_agent", os.path.join(out4, "agent.py"))
m4 = importlib.util.module_from_spec(spec4)
spec4.loader.exec_module(m4)
assert not m4.AGENTS["planner"].get("quick_response")
assert "__none__" not in m4.AGENTS["planner"]["system"]
def noquick_stub(agent_name, cfg, system, messages):
    if agent_name == "planner":
        return ("hi\nROUTE: __none__", [])   # not honored -> first worker
    return (f"handled by {agent_name}", [])
m4._call_one = noquick_stub
m4.clear_history()
assert m4.run("hi", emit=lambda s: None) == "handled by wc"
print("quick_response off ok: '__none__' not honored, falls back to first worker")

print("\nALL ROUTER CHECKS PASSED")
