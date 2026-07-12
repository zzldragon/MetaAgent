"""Verify the runtime debug overlay: the pure state machine that turns trace
records into per-node status, plus the in-process trace sink + stage_start/
stage_end events the generated agent now emits (no network — _call_one stubbed)."""

import importlib.util
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.chdir(BASE)

import graph_codegen
from graph_model import DEFAULT_BUDGETS, Graph
from runtime_overlay import DONE, ERROR, IDLE, RUNNING, RuntimeOverlay

# ── 1. lifecycle: idle -> running -> done, with run_end ─────────────────────
ov = RuntimeOverlay(["planner", "executor", "critic"])
for rec in [
    {"kind": "run_start", "task": "t"},
    {"kind": "stage_start", "agent": "planner", "kind2": "agent"},
    {"kind": "llm_step", "agent": "planner", "step": 1},
    {"kind": "tool_call", "agent": "planner", "tool": "load_csv"},
    {"kind": "tool_result", "agent": "planner", "tool": "load_csv"},
    {"kind": "stage_end", "agent": "planner", "output": "plan"},
    {"kind": "stage_start", "agent": "executor"},
    {"kind": "llm_step", "agent": "executor", "step": 1},
    {"kind": "stage_end", "agent": "executor", "output": "done"},
    {"kind": "run_end", "result": "final answer"},
]:
    ov.consume(rec)
assert ov.status_of("planner") == DONE, ov.status_of("planner")
assert ov.status_of("executor") == DONE
assert ov.status_of("critic") == IDLE        # never ran
assert ov.finished and ov.result == "final answer"
assert ov.nodes["planner"]["tool_calls"] == 1
assert ov.nodes["planner"]["last_tool"] == "load_csv"
assert ov.badge("executor") == "✓"
# the planner→executor transition lit up; nothing is "flowing" after run_end
assert ov.is_edge_traversed("planner", "executor")
assert not ov.is_edge_traversed("executor", "critic")
assert ov.active_edge is None
print("ok 1: lifecycle, tool counts, result, and the traversed edge")

# ── 2. a fresh run_start resets everything ──────────────────────────────────
ov.consume({"kind": "run_start", "task": "again"})
assert all(n["status"] == IDLE for n in ov.nodes.values())
assert not ov.finished and ov.result == ""
print("ok 2: run_start resets all node state")

# ── 3. router: route choice recorded, node marked done ──────────────────────
ovr = RuntimeOverlay(["router", "a", "b"])
for rec in [
    {"kind": "run_start"},
    {"kind": "stage_start", "agent": "router", "kind2": "router"},
    {"kind": "route", "router": "router", "choice": "b", "routes": ["a", "b"]},
    {"kind": "stage_end", "agent": "router", "output": "b"},
    {"kind": "stage_start", "agent": "b"},
    {"kind": "stage_end", "agent": "b"},
    {"kind": "run_end", "result": "ok"},
]:
    ovr.consume(rec)
assert ovr.nodes["router"]["route"] == "b"
assert ovr.status_of("router") == DONE and ovr.status_of("b") == DONE
assert ovr.status_of("a") == IDLE
# only the chosen branch's edge lights up
assert ovr.is_edge_traversed("router", "b")
assert not ovr.is_edge_traversed("router", "a")
print("ok 3: router records its chosen route + lights only that edge")

# ── 4. errors mark the active node, not the whole graph ─────────────────────
ove = RuntimeOverlay(["planner", "executor"])
ove.consume({"kind": "run_start"})
ove.consume({"kind": "stage_start", "agent": "planner"})
ove.consume({"kind": "run_error", "error": "boom"})
assert ove.status_of("planner") == ERROR
assert ove.finished and ove.error == "boom"
print("ok 4: run_error marks the active node ERROR")

# ── 5. active inference when only llm_step events arrive ────────────────────
ova = RuntimeOverlay(["a", "b"])
ova.consume({"kind": "run_start"})
ova.consume({"kind": "llm_step", "agent": "a", "step": 1})
assert ova.status_of("a") == RUNNING
ova.consume({"kind": "llm_step", "agent": "b", "step": 1})  # switch → a is done
assert ova.status_of("a") == DONE and ova.status_of("b") == RUNNING
print("ok 5: switching active node finalizes the previous one")

# ── 5b. shared-state events: full snapshot on the overlay, delta as a note ───
ovs = RuntimeOverlay(["work", "gate"])
ovs.consume({"kind": "run_start"})
ovs.consume({"kind": "stage_start", "agent": "work"})
ovs.consume({"kind": "state", "agent": "work",
             "updates": {"attempts": 1},
             "state": {"attempts": 1, "score": 0.0}})
assert ovs.state == {"attempts": 1, "score": 0.0}, ovs.state   # full snapshot
assert ovs.nodes["work"]["note"] == "state: attempts=1"        # per-node delta
ovs.consume({"kind": "state", "agent": "work",
             "updates": {"score": 0.9},
             "state": {"attempts": 1, "score": 0.9}})
assert ovs.state == {"attempts": 1, "score": 0.9}, ovs.state   # snapshot advances
assert ovs.last == "work: state score=0.9", ovs.last
ovs.consume({"kind": "run_start"})                             # reset clears state
assert ovs.state == {}, ovs.state
print("ok 5b: state events keep a live full snapshot + per-node delta; reset clears")

# ── 5c. fan-out: concurrent branches don't cross-finish, no phantom sibling edges ─
# mirrors the record stream the runtime emits: coordinator -> fanout -> {a1,a2,a3
# CONCURRENTLY, interleaved} -> join -> reducer. The single-cursor inference must be
# suspended for the duration so siblings show RUNNING at once and no sibling edge.
ovf = RuntimeOverlay(["coord", "a1", "a2", "a3", "reducer"])
ovf.consume({"kind": "run_start"})
ovf.consume({"kind": "stage_start", "agent": "coord"})
ovf.consume({"kind": "stage_end", "agent": "coord", "output": "framed"})
ovf.consume({"kind": "fanout", "agent": "fo", "branches": ["a1", "a2", "a3"], "join": "jn"})
# interleaved branch activity (as three worker threads would produce)
ovf.consume({"kind": "stage_start", "agent": "a1"})
ovf.consume({"kind": "stage_start", "agent": "a2"})
ovf.consume({"kind": "llm_step", "agent": "a1", "step": 1})
ovf.consume({"kind": "stage_start", "agent": "a3"})
ovf.consume({"kind": "llm_step", "agent": "a3", "step": 1})
# at this instant ALL three branches must be RUNNING at once (the whole point) —
# none spuriously marked DONE just because a sibling became active
assert ovf.status_of("a1") == RUNNING, ovf.status_of("a1")
assert ovf.status_of("a2") == RUNNING, ovf.status_of("a2")
assert ovf.status_of("a3") == RUNNING, ovf.status_of("a3")
# and NO phantom edge between two concurrent siblings
assert not ovf.is_edge_traversed("a1", "a2")
assert not ovf.is_edge_traversed("a2", "a1")
assert not ovf.is_edge_traversed("a1", "a3")
assert not ovf.is_edge_traversed("a3", "a2")
# the real fan-out edges DID light: coord→fanout and fanout→each branch
assert ovf.is_edge_traversed("coord", "fo"), sorted(ovf.edges)
assert ovf.is_edge_traversed("fo", "a1") and ovf.is_edge_traversed("fo", "a2") \
    and ovf.is_edge_traversed("fo", "a3")
# branches finish on their OWN stage_end, in any order; a later sibling event must
# NOT resurrect an already-finished branch
ovf.consume({"kind": "stage_end", "agent": "a1", "output": "r1"})
ovf.consume({"kind": "llm_step", "agent": "a2", "step": 1})   # a2 still going
assert ovf.status_of("a1") == DONE, "a1 finished on its own stage_end"
assert ovf.status_of("a2") == RUNNING and ovf.status_of("a3") == RUNNING
ovf.consume({"kind": "stage_end", "agent": "a3", "output": "r3"})
ovf.consume({"kind": "stage_end", "agent": "a2", "output": "r2"})
# the join barrier: leaves concurrent mode, lights each branch→join edge
ovf.consume({"kind": "join", "agent": "jn", "branches": ["a1", "a2", "a3"],
             "state": {"reports": 3}})
assert ovf.state == {"reports": 3}, ovf.state
assert ovf.is_edge_traversed("a1", "jn") and ovf.is_edge_traversed("a2", "jn") \
    and ovf.is_edge_traversed("a3", "jn")
assert not ovf._concurrent, "fan-out closed at the join"
# after the join the walk is sequential again: join→reducer lights normally
ovf.consume({"kind": "stage_start", "agent": "reducer"})
ovf.consume({"kind": "stage_end", "agent": "reducer", "output": "final"})
ovf.consume({"kind": "run_end", "result": "final"})
assert ovf.is_edge_traversed("jn", "reducer"), sorted(ovf.edges)
assert all(ovf.status_of(x) == DONE for x in ("coord", "a1", "a2", "a3", "reducer"))
# fanout/join are control nodes → no ▶/✓ badge, only their edges light
assert ovf.badge("fo") == "" and ovf.badge("jn") == ""
print("ok 5c: fan-out — concurrent branches, no phantom sibling edges, clean join")

# ── 6. end-to-end: the generated agent emits stage_start/end to a live sink ─
g = Graph()
p = g.new_node("agent", 0, 0); p.name = "planner"
e = g.new_node("agent", 0, 0); e.name = "executor"
for a in (p, e):
    llm = g.new_node("llm", 0, 0)
    llm.props.update(api_key="sk-test", model="deepseek-ai/DeepSeek-V4-Flash")
    g.add_edge(llm.id, a.id)
g.add_edge(p.id, e.id)                       # planner → executor pipeline
out_dir = graph_codegen.generate_from_graph(g, "demo_overlay", gui=False)
spec = importlib.util.spec_from_file_location(
    "demo_overlay_agent", os.path.join(out_dir, "agent.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

records = []
mod.set_trace_sink(records.append)
# each stage answers in one step with no tool calls
mod._call_one = lambda agent_name, cfg, system, messages: (
    f"{agent_name} answer", [])
mod.clear_history()
result = mod.run("do the thing", emit=lambda s: None)
assert result == "executor answer", result

kinds = [r["kind"] for r in records]
assert "run_start" in kinds and "run_end" in kinds
assert kinds.count("stage_start") == 2, kinds   # planner + executor
assert kinds.count("stage_end") == 2
starts = [r["agent"] for r in records if r["kind"] == "stage_start"]
assert starts == ["planner", "executor"], starts

# replaying the captured trace through a fresh overlay lights both nodes
live = RuntimeOverlay(["planner", "executor"])
for r in records:
    live.consume(r)
assert live.status_of("planner") == DONE and live.status_of("executor") == DONE
assert live.finished and live.result == "executor answer"
assert live.is_edge_traversed("planner", "executor")  # the real edge lit up
print("ok 6: generated agent feeds a live sink; nodes + edge light up")

# ── 7. end-to-end fan-out: a REAL generated agent emits fanout/join records that ─
# the overlay consumes correctly (branches run on real threads → interleaved trace).
gf = Graph()
gf.state_schema = [
    {"name": "ra", "type": "str", "reducer": "overwrite", "default": "", "description": "A"},
    {"name": "rb", "type": "str", "reducer": "overwrite", "default": "", "description": "B"},
]


def _fagent(g, name, reads, writes):
    a = g.new_node("agent", 0, 0); a.name = name; a.props["role"] = "single"
    a.props["reads"] = reads; a.props["writes"] = writes
    for k in DEFAULT_BUDGETS:
        a.props[k] = DEFAULT_BUDGETS[k]
    return a


llmf = gf.new_node("llm", 0, 0); llmf.name = "m"
llmf.props.update(api_key="sk-test", model="deepseek-ai/DeepSeek-V4-Flash")
dispatch = _fagent(gf, "dispatch", ["user_input"], [])
fa = _fagent(gf, "fa", ["user_input"], ["ra"])
fb = _fagent(gf, "fb", ["user_input"], ["rb"])
tail = _fagent(gf, "tail", ["ra", "rb"], [])
for a in (dispatch, fa, fb, tail):
    gf.add_edge(llmf.id, a.id)
fo = gf.new_node("fanout", 0, 0); fo.name = "fo"
jn = gf.new_node("join", 0, 0); jn.name = "jn"
gf.add_edge(dispatch.id, fo.id)
gf.add_edge(fo.id, fa.id); gf.add_edge(fo.id, fb.id)
gf.add_edge(fa.id, jn.id); gf.add_edge(fb.id, jn.id)
gf.add_edge(jn.id, tail.id)

out_dir = graph_codegen.generate_from_graph(gf, "demo_overlay_fanout", gui=False)
spec = importlib.util.spec_from_file_location(
    "demo_overlay_fanout_agent", os.path.join(out_dir, "agent.py"))
fmod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fmod)

frecords = []
fmod.set_trace_sink(frecords.append)
fmod._call_one = lambda agent_name, cfg, system, messages: (f"{agent_name} out", [])
fmod.clear_history()
fmod.run("go", emit=lambda s: None)

fkinds = [r["kind"] for r in frecords]
assert "fanout" in fkinds and "join" in fkinds, fkinds
fo_rec = next(r for r in frecords if r["kind"] == "fanout")
assert fo_rec["agent"] == "fo" and set(fo_rec["branches"]) == {"fa", "fb"}, fo_rec
assert fo_rec["join"] == "jn"

# replay the REAL (interleaved) trace through a fresh overlay — no cross-talk
rov = RuntimeOverlay(["dispatch", "fa", "fb", "tail"])
for r in frecords:
    rov.consume(r)
assert rov.finished
assert all(rov.status_of(x) == DONE for x in ("dispatch", "fa", "fb", "tail"))
# real fan-out topology lit; no phantom sibling edge between fa and fb
assert rov.is_edge_traversed("dispatch", "fo")
assert rov.is_edge_traversed("fo", "fa") and rov.is_edge_traversed("fo", "fb")
assert rov.is_edge_traversed("fa", "jn") and rov.is_edge_traversed("fb", "jn")
assert rov.is_edge_traversed("jn", "tail")
assert not rov.is_edge_traversed("fa", "fb") and not rov.is_edge_traversed("fb", "fa")
print("ok 7: real generated agent emits fanout/join records; overlay reconstructs "
      "the concurrent topology with no phantom sibling edges")

print("\nALL RUNTIME-OVERLAY CHECKS PASSED")
