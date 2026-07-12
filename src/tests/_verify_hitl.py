"""Verify the HITL checkpoint node: model/edges, the analyze-time splice into a
review gate, validation, and the runtime approve / edit / reject-stop /
reject-revise behavior (no network — _call_one and the review UI are stubbed)."""

import importlib.util
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.chdir(BASE)

import graph_codegen
from graph_model import FLOW_KINDS, Graph, default_props
from runtime_overlay import DONE, RuntimeOverlay

# ── 1. model: hitl node, defaults, allowed edges ────────────────────────────
d = default_props("hitl")
assert d["on_reject"] == "stop" and "prompt" in d
da = default_props("agent")
assert da["hitl_triggers"] == ["high_risk_tool"]   # default preserves behavior
assert da["hitl_confidence_threshold"] == 0.6
g = Graph()
a = g.new_node("agent", 0, 0); a.name = "planner"
h = g.new_node("hitl", 0, 0); h.name = "review"
b = g.new_node("agent", 0, 0); b.name = "executor"
assert g.add_edge(a.id, h.id) is None        # agent → hitl allowed
assert g.add_edge(h.id, b.id) is None        # hitl → agent allowed
assert "hitl" in FLOW_KINDS
print("ok 1: hitl node + agent→hitl→agent edges allowed")

# ── 2. splice: A→H→B becomes A→B with a review gate on B ────────────────────
for n in (a, h, b):
    llm = g.new_node("llm", 0, 0)
    if n.kind == "agent":
        g.add_edge(llm.id, n.id)
h.props.update(prompt="Approve the plan?", on_reject="revise")
eff, gates = graph_codegen._splice_hitl(g)
assert all(nn.kind != "hitl" for nn in eff.nodes.values()), "hitl removed"
# the spliced graph has a direct planner→executor edge
pe = [(eff.nodes[e.src].name, eff.nodes[e.dst].name) for e in eff.edges
      if eff.nodes[e.src].kind == "agent" and eff.nodes[e.dst].kind == "agent"]
assert ("planner", "executor") in pe, pe
gate = gates["executor"]
assert gate == {"node": "review", "source": "planner",
                "prompt": "Approve the plan?", "on_reject": "revise"}, gate
print("ok 2: splice removes hitl, adds A→B, records gate on downstream agent")

# analyze sees a clean 2-stage chain (no errors)
info = graph_codegen.analyze(g)
assert not info["errors"], info["errors"]
assert info["mode"] == "chain"
print("ok 3: analyze treats the spliced graph as a normal chain")

# ── 4. validation: a HITL node must sit BETWEEN two agents ──────────────────
bad = Graph()
ba = bad.new_node("agent", 0, 0); bl = bad.new_node("llm", 0, 0)
bad.add_edge(bl.id, ba.id)
bh = bad.new_node("hitl", 0, 0)
bad.add_edge(ba.id, bh.id)                    # terminal hitl (no outgoing)
assert any("must sit between two agents" in e
           for e in graph_codegen._validate_hitl(bad))
bad2 = Graph()
b2a = bad2.new_node("agent", 0, 0); b2l = bad2.new_node("llm", 0, 0)
bad2.add_edge(b2l.id, b2a.id)
b2h = bad2.new_node("hitl", 0, 0)
bad2.add_edge(b2h.id, b2a.id)                 # HITL → agent (no upstream) — now invalid
assert any("must sit between two agents" in e
           for e in graph_codegen._validate_hitl(bad2))
print("ok 4: HITL needs an upstream AND a downstream agent (entry-gate is a "
      "property now)")

# ── 5. runtime: generate planner→review→executor, then drive it ─────────────
gg = Graph()
p = gg.new_node("agent", 0, 0); p.name = "planner"
hh = gg.new_node("hitl", 0, 0); hh.name = "review"
e = gg.new_node("agent", 0, 0); e.name = "executor"
for n in (p, e):
    lm = gg.new_node("llm", 0, 0)
    lm.props.update(api_key="sk", model="deepseek-ai/DeepSeek-V4-Flash")
    gg.add_edge(lm.id, n.id)
gg.add_edge(p.id, hh.id); gg.add_edge(hh.id, e.id)
out_dir = graph_codegen.generate_from_graph(gg, "demo_hitl", gui=False)
spec = importlib.util.spec_from_file_location(
    "demo_hitl_agent", os.path.join(out_dir, "agent.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
assert mod.AGENTS["executor"]["review"]["source"] == "planner"
assert "review" not in mod.AGENTS["planner"]    # gate only on the downstream
print("ok 5: generated agent carries the review gate on 'executor'")

calls = {"planner": 0, "executor": 0}
def stub(agent_name, cfg, system, messages):
    calls[agent_name] += 1
    if agent_name == "executor":
        return messages[0]["content"], []      # echo input so edits are visible
    return "planner answer", []
mod._call_one = stub

# approve → content flows through, both stages run, the checkpoint is traced
records = []
mod.set_trace_sink(records.append)
mod.set_review_handler(lambda prompt, content: {
    "decision": "approve", "content": content, "feedback": ""})
mod.clear_history(); calls.update(planner=0, executor=0)
res = mod.run("do it", emit=lambda s: None)
assert calls["planner"] == 1 and calls["executor"] == 1, calls
assert any(r["kind"] == "stage_start" and r.get("agent") == "review"
           for r in records), "checkpoint should emit a stage event"
ov = RuntimeOverlay(["planner", "review", "executor"])
for r in records:
    ov.consume(r)
assert ov.status_of("review") == DONE
assert ov.is_edge_traversed("planner", "review")
assert ov.is_edge_traversed("review", "executor")
print("ok 6: approve passes through; checkpoint node + its edges light up")

# edit → the downstream agent receives the edited text
mod.set_review_handler(lambda prompt, content: {
    "decision": "edit", "content": "EDITED PLAN", "feedback": ""})
mod.clear_history()
res = mod.run("do it", emit=lambda s: None)
assert res == "EDITED PLAN", res          # executor echoed the edited input
print("ok 7: edit replaces the content handed to the downstream agent")

# reject + on_reject='stop' → run ends with the rejection note, executor skipped
mod.AGENTS["executor"]["review"]["on_reject"] = "stop"
mod.set_review_handler(lambda prompt, content: {
    "decision": "reject", "content": content, "feedback": "not good enough"})
mod.clear_history(); calls.update(planner=0, executor=0)
res = mod.run("do it", emit=lambda s: None)
assert "rejected by human" in res and "not good enough" in res, res
assert calls["executor"] == 0, "executor must not run after a stop-rejection"
print("ok 8: reject + stop ends the run; downstream agent never runs")

# reject + on_reject='revise' → upstream re-runs with feedback, then approved
mod.AGENTS["executor"]["review"]["on_reject"] = "revise"
seq = ["reject", "approve"]
def review_then_approve(prompt, content):
    d = seq.pop(0) if seq else "approve"
    return {"decision": d, "content": content,
            "feedback": "add more detail" if d == "reject" else ""}
mod.set_review_handler(review_then_approve)
mod.clear_history(); calls.update(planner=0, executor=0)
res = mod.run("do it", emit=lambda s: None)
assert calls["planner"] == 2, f"revise should re-run the upstream agent: {calls}"
assert calls["executor"] == 1
print("ok 9: reject + revise re-runs the upstream agent, then continues")

# ── 10. the agent 'review before run' property (entry-gate replacement) ─────
pg = Graph()
solo = pg.new_node("agent", 0, 0); solo.name = "solo"
lm = pg.new_node("llm", 0, 0)
lm.props.update(api_key="sk", model="deepseek-ai/DeepSeek-V4-Flash")
pg.add_edge(lm.id, solo.id)
solo.props["hitl_review"] = True            # gate the entry stage via a property
solo.props["hitl_on_reject"] = "stop"
out2 = graph_codegen.generate_from_graph(pg, "demo_hitl_prop", gui=False)
spec2 = importlib.util.spec_from_file_location(
    "demo_hitl_prop_agent", os.path.join(out2, "agent.py"))
m2 = importlib.util.module_from_spec(spec2)
spec2.loader.exec_module(m2)
gate2 = m2.AGENTS["solo"]["review"]
assert gate2["node"] is None and gate2["source"] is None, gate2  # entry: no node/source
m2._call_one = lambda a, c, s, msgs: ("solo answer", [])

# approve → runs normally
m2.set_review_handler(lambda p, c: {"decision": "approve", "content": c,
                                    "feedback": ""})
m2.clear_history()
assert m2.run("go", emit=lambda s: None) == "solo answer"
# reject (stop) → ends the run, no canvas node needed
m2.set_review_handler(lambda p, c: {"decision": "reject", "content": c,
                                    "feedback": "nope"})
m2.clear_history()
assert "rejected by human" in m2.run("go", emit=lambda s: None)
print("ok 10: agent 'review before run' property gates the entry stage "
      "(no HITL node required)")

# ── 11. low-confidence trigger: self-rate, pause when below threshold ───────
cg = Graph()
ca = cg.new_node("agent", 0, 0); ca.name = "solo"
cl = cg.new_node("llm", 0, 0)
cl.props.update(api_key="sk", model="deepseek-ai/DeepSeek-V4-Flash")
cg.add_edge(cl.id, ca.id)
ca.props["hitl_triggers"] = ["low_confidence"]
ca.props["hitl_confidence_threshold"] = 0.6
ca.props["hitl_on_reject"] = "stop"
outc = graph_codegen.generate_from_graph(cg, "demo_hitl_conf", gui=False)
specc = importlib.util.spec_from_file_location(
    "demo_hitl_conf_agent", os.path.join(outc, "agent.py"))
mc = importlib.util.module_from_spec(specc); specc.loader.exec_module(mc)
assert mc.AGENTS["solo"]["hitl_triggers"] == ["low_confidence"]

score = {"v": 0.3}
def cstub(agent_name, cfg, system, messages):
    if "self-evaluator" in system:        # the confidence-rating call
        return str(score["v"]), []
    return "draft answer", []
mc._call_one = cstub

reviews = {"n": 0}
mc.set_review_handler(lambda p, c: (reviews.__setitem__("n", reviews["n"] + 1)
                                    or {"decision": "approve", "content": c,
                                        "feedback": ""}))
mc.clear_history()
assert mc.run("task", emit=lambda s: None) == "draft answer"
assert reviews["n"] == 1, "low confidence (0.3 < 0.6) should pause for review"

score["v"] = 0.9; reviews["n"] = 0      # confident → no pause
mc.clear_history()
assert mc.run("task", emit=lambda s: None) == "draft answer"
assert reviews["n"] == 0, "high confidence should not pause"

score["v"] = 0.2                         # low + edit → edited answer
mc.set_review_handler(lambda p, c: {"decision": "edit", "content": "FIXED",
                                    "feedback": ""})
mc.clear_history()
assert mc.run("task", emit=lambda s: None) == "FIXED"

mc.set_review_handler(lambda p, c: {"decision": "reject", "content": c,
                                    "feedback": "redo"})
mc.clear_history()
assert "rejected" in mc.run("task", emit=lambda s: None).lower()
print("ok 11: low-confidence self-rating gates review (approve / edit / reject)")

# ── 12. the high-risk-tool trigger is per-agent ─────────────────────────────
hg = Graph()
ha = hg.new_node("agent", 0, 0); ha.name = "w"
hl = hg.new_node("llm", 0, 0)
hl.props.update(api_key="sk", model="deepseek-ai/DeepSeek-V4-Flash")
hg.add_edge(hl.id, ha.id)
ht = hg.new_node("tool", 0, 0); ht.props["files"] = ["load_csv.py"]
hg.add_edge(ht.id, ha.id)
ha.props["hitl_triggers"] = ["high_risk_tool"]
outh = graph_codegen.generate_from_graph(hg, "demo_hitl_risk", gui=False)
spech = importlib.util.spec_from_file_location(
    "demo_hitl_risk_agent", os.path.join(outh, "agent.py"))
mh = importlib.util.module_from_spec(spech); spech.loader.exec_module(mh)
mh.TOOLS["save_report"] = lambda **k: "saved"        # a high-risk-named tool
mh.AGENTS["w"]["tools"].append("save_report")
confirms = {"n": 0}
def confirm_handler(prompt):
    confirms["n"] += 1
    return True
mh.set_confirm_handler(confirm_handler)
rounds = {"n": 0}
def hstub(agent_name, cfg, system, messages):
    rounds["n"] += 1
    if rounds["n"] == 1:
        return "", [{"id": "t1", "name": "save_report", "args": {}}]
    return "done", []
mh._call_one = hstub
mh.clear_history()
assert mh.run("save it", emit=lambda s: None) == "done"
assert confirms["n"] == 1, "high-risk tool must be confirmed when trigger on"

mh.AGENTS["w"]["hitl_triggers"] = []     # trigger off → no confirmation
confirms["n"] = 0; rounds["n"] = 0
mh.clear_history()
assert mh.run("save it", emit=lambda s: None) == "done"
assert confirms["n"] == 0, "no confirm when the high_risk_tool trigger is off"
print("ok 12: high-risk-tool confirmation is per-agent (trigger on/off)")

print("\nALL HITL CHECKS PASSED")
