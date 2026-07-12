"""Verify the autonomous (orchestrator) pattern: graph rules, codegen of the
built-in spawn_subagent tool, and the runtime spawning isolated sub-agents
(single + parallel) — all offline (the LLM is stubbed)."""

import importlib.util
import os
import py_compile
import shutil
import sys
import threading

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.chdir(BASE)

import graph_codegen
import patterns
from graph_model import Graph

LLM = {"provider": "siliconflow", "model": "deepseek-ai/DeepSeek-V4-Flash",
       "api_key": "sk-test", "base_url": "https://api.siliconflow.cn/v1"}


def _orchestrator_graph():
    g = Graph()
    orch = g.new_node("agent", 0, 0); orch.name = "orchestrator"
    orch.props["role"] = "orchestrator"
    lo = g.new_node("llm", 0, 0); lo.props.update(LLM); g.add_edge(lo.id, orch.id)
    writer = g.new_node("agent", 0, 0); writer.name = "writer"; writer.props["role"] = "worker"
    lw = g.new_node("llm", 0, 0); lw.props.update(LLM); g.add_edge(lw.id, writer.id)
    tw = g.new_node("tool", 0, 0); tw.props["files"] = ["csv_column_means.py"]
    g.add_edge(tw.id, writer.id)
    reader = g.new_node("agent", 0, 0); reader.name = "reader"; reader.props["role"] = "worker"
    lr = g.new_node("llm", 0, 0); lr.props.update(LLM); g.add_edge(lr.id, reader.id)
    tr = g.new_node("tool", 0, 0); tr.props["files"] = ["load_csv.py"]
    g.add_edge(tr.id, reader.id)
    g.add_edge(orch.id, writer.id)
    g.add_edge(orch.id, reader.id)
    return g, orch, writer, reader


# 1. Pattern preset builds and classifies as autonomous
pg = patterns.build_pattern_graph("orchestrator", llm_props=LLM)
info = graph_codegen.analyze(pg)
assert not info["errors"], info["errors"]
assert info["mode"] == "autonomous", info["mode"]
print("ok 1: orchestrator preset builds → mode 'autonomous'")

# 1b. tools attach PER sub-agent: writer and reader each get their OWN tool node
pg2 = patterns.build_pattern_graph("orchestrator", LLM, tool_files=["load_csv.py"])
by_name = {n.name: n for n in pg2.nodes.values()}
tool_nodes = [n for n in pg2.nodes.values() if n.kind == "tool"]
assert len(tool_nodes) == 2, [n.name for n in tool_nodes]
w_tools = pg2.inputs_of(by_name["writer"].id, "tool")
r_tools = pg2.inputs_of(by_name["reader"].id, "tool")
assert len(w_tools) == 1 and len(r_tools) == 1, (w_tools, r_tools)
assert w_tools[0].id != r_tools[0].id, "writer and reader must not share a tool node"
# the orchestrator itself (not a worker/single role) gets no tools node
assert not pg2.inputs_of(by_name["orchestrator"].id, "tool")
print("ok 1b: each sub-agent gets its own Tools node (writer ≠ reader)")

# 2. Graph rules / validation
g, orch, writer, reader = _orchestrator_graph()
info = graph_codegen.analyze(g)
assert not info["errors"] and info["mode"] == "autonomous", info
assert g.nodes[info["entry"]].name == "orchestrator"
# 2a. an orchestrator with no sub-agents is rejected
g_empty = Graph()
o2 = g_empty.new_node("agent", 0, 0); o2.name = "o"; o2.props["role"] = "orchestrator"
l2 = g_empty.new_node("llm", 0, 0); l2.props.update(LLM); g_empty.add_edge(l2.id, o2.id)
assert any("at least one linked sub-agent" in e
           for e in graph_codegen.analyze(g_empty)["errors"])
# 2b. a sub-agent that itself branches further is rejected (leaves only, v1)
g_deep, o3, w3, r3 = _orchestrator_graph()
g_deep.add_edge(w3.id, r3.id)   # writer → reader: writer is no longer a leaf
assert any("must not have outgoing agent links" in e
           for e in graph_codegen.analyze(g_deep)["errors"])
print("ok 2: validation — entry, ≥1 sub-agent, sub-agents must be leaves")

# 2c. an orchestrator that is NOT the entry agent is rejected (entry-only role)
g_mid = Graph()
lead = g_mid.new_node("agent", 0, 0); lead.name = "lead"; lead.props["role"] = "single"
la = g_mid.new_node("llm", 0, 0); la.props.update(LLM); g_mid.add_edge(la.id, lead.id)
mid = g_mid.new_node("agent", 0, 0); mid.name = "mid"; mid.props["role"] = "orchestrator"
lm = g_mid.new_node("llm", 0, 0); lm.props.update(LLM); g_mid.add_edge(lm.id, mid.id)
wk = g_mid.new_node("agent", 0, 0); wk.name = "wk"; wk.props["role"] = "worker"
lwk = g_mid.new_node("llm", 0, 0); lwk.props.update(LLM); g_mid.add_edge(lwk.id, wk.id)
g_mid.add_edge(lead.id, mid.id)   # lead (entry) → orchestrator (mid-chain)
g_mid.add_edge(mid.id, wk.id)
assert any("is not the main (entry) agent" in e
           for e in graph_codegen.analyze(g_mid)["errors"])
# defense-in-depth: even if analyze were bypassed, the mid-chain orchestrator
# must not receive spawn_subagent during spec building.
specs, *_ = graph_codegen._build_agent_specs(
    g_mid, [lead.id, mid.id, wk.id], None, {})
assert "spawn_subagent" not in specs["mid"]["tools"], specs["mid"]["tools"]

# 2d. a Router (non-agent kind) cannot be a spawnable sub-agent
g_rt, o4, w4, r4 = _orchestrator_graph()
rt = g_rt.new_node("router", 0, 0); rt.name = "rt"
lrt = g_rt.new_node("llm", 0, 0); lrt.props.update(LLM); g_rt.add_edge(lrt.id, rt.id)
leaf = g_rt.new_node("agent", 0, 0); leaf.name = "leaf"; leaf.props["role"] = "worker"
llf = g_rt.new_node("llm", 0, 0); llf.props.update(LLM); g_rt.add_edge(llf.id, leaf.id)
g_rt.add_edge(o4.id, rt.id)       # orchestrator → router (illegal sub-agent)
g_rt.add_edge(rt.id, leaf.id)     # router needs a branch
assert any("must be a plain Agent node" in e
           for e in graph_codegen.analyze(g_rt)["errors"])

# 2e. no nested orchestration (a sub-agent can't itself be an orchestrator)
g_nest, o5, w5, r5 = _orchestrator_graph()
sub_o = g_nest.new_node("agent", 0, 0); sub_o.name = "suborch"
sub_o.props["role"] = "orchestrator"
lso = g_nest.new_node("llm", 0, 0); lso.props.update(LLM); g_nest.add_edge(lso.id, sub_o.id)
g_nest.add_edge(o5.id, sub_o.id)
assert any("can't itself be an orchestrator" in e
           for e in graph_codegen.analyze(g_nest)["errors"])
print("ok 2b: orchestrator is entry-only; sub-agents must be plain leaf agents")

# 3. Codegen: built-in spawn_subagent on the orchestrator only; sub-agents isolated
out = graph_codegen.generate_from_graph(g, "demo_orchestrator")
try:
    py_compile.compile(os.path.join(out, "agent.py"), doraise=True)
    spec = importlib.util.spec_from_file_location("demo_orch_agent",
                                                  os.path.join(out, "agent.py"))
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    assert mod.PATTERN_MODE == "autonomous" and mod.ENTRY == "orchestrator"
    assert sorted(mod.SPAWNABLE) == ["reader", "writer"], mod.SPAWNABLE
    assert "spawn_subagent" in mod.AGENTS["orchestrator"]["tools"]
    assert mod.AGENTS["orchestrator"]["parallel_tools"] is True
    # isolation: sub-agents have ONLY their own tool, never spawn_subagent
    assert mod.AGENTS["writer"]["tools"] == ["csv_column_means"]
    assert mod.AGENTS["reader"]["tools"] == ["load_csv"]
    assert "spawn_subagent" not in mod.AGENTS["writer"]["tools"]
    assert "spawn_subagent" not in mod.AGENTS["reader"]["tools"]
    # spawn_subagent is a special-cased built-in, NOT a global tool a sub-agent
    # could reach by name
    assert "spawn_subagent" not in mod.TOOLS
    assert "Your sub-agents" in mod.AGENTS["orchestrator"]["system"]
    print("ok 3: spawn_subagent on orchestrator only; sub-agents tool-isolated")

    # 4. Runtime: a single spawn runs the sub-agent in isolation
    ran = set(); lock = threading.Lock()

    def stub_single(agent_name, cfg, system, messages):
        with lock:
            ran.add(agent_name)
        if agent_name == "orchestrator":
            if not any(m.get("role") == "tool" for m in messages):
                return "", [{"id": "c1", "name": "spawn_subagent",
                             "args": {"name": "writer", "task": "draft a haiku"}}]
            return "FINAL: " + messages[-1]["content"], []
        if agent_name == "writer":
            assert "haiku" in messages[0]["content"], messages[0]["content"]
            # the sub-agent must NOT see the orchestrator's framing
            assert "spawn" not in messages[0]["content"].lower()
            return "WROTE-HAIKU", []
        return "?", []
    mod._call_one = stub_single
    res = mod.run("write me a haiku", emit=lambda s: None)
    assert "WROTE-HAIKU" in res and res.startswith("FINAL:"), res
    assert "reader" not in ran, "orchestrator should not have spawned the reader"
    print("ok 4: single spawn — writer ran in isolation, result synthesized")

    # 5. Runtime: two spawns in one turn run both sub-agents (parallel-safe)
    ran.clear()

    def stub_parallel(agent_name, cfg, system, messages):
        with lock:
            ran.add(agent_name)
        if agent_name == "orchestrator":
            if not any(m.get("role") == "tool" for m in messages):
                return "", [
                    {"id": "a", "name": "spawn_subagent",
                     "args": {"name": "writer", "task": "write part A"}},
                    {"id": "b", "name": "spawn_subagent",
                     "args": {"name": "reader", "task": "read part B"}}]
            tool_msgs = [m["content"] for m in messages if m.get("role") == "tool"]
            return "FINAL combined: " + " | ".join(tool_msgs), []
        if agent_name == "writer":
            return "WROTE", []
        if agent_name == "reader":
            return "READ", []
        return "?", []
    mod._call_one = stub_parallel
    res = mod.run("do A and B", emit=lambda s: None)
    assert {"orchestrator", "writer", "reader"} <= ran, ran
    assert "WROTE" in res and "READ" in res, res
    print("ok 5: parallel spawn — writer + reader both ran, results merged")

    # 6. spawn_subagent rejects an unknown sub-agent (no escalation/typos)
    err = mod._spawn_subagent({"name": "ghost", "task": "x"}, emit=lambda s: None)
    assert err.startswith("[ERROR] Unknown sub-agent 'ghost'"), err
    blank = mod._spawn_subagent({"name": "writer", "task": "   "}, emit=lambda s: None)
    assert blank.startswith("[ERROR]") and "non-empty" in blank, blank
    print("ok 6: spawn_subagent guards unknown name + empty task")
finally:
    shutil.rmtree(out, ignore_errors=True)

print("\nALL ORCHESTRATOR CHECKS PASSED")
