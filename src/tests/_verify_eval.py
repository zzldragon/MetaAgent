"""Verify the Eval node: model (kind/edges/props/eval_cases), analyze
validation (>1 target, empty cases), config['evals'] emission with the right
target, and the generated agent's run_evals engine — substring / regex / LLM
judge grading, whole-pipeline vs single-agent targeting (no real network: the
LLM is monkeypatched)."""

import importlib.util
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.chdir(BASE)

import graph_codegen
from graph_model import (ALLOWED_EDGES, AGENT_KINDS, Edge, Graph, Node,
                         default_props, eval_cases)

# ── 1. model: kind, edges, props, eval_cases filtering ──────────────────────
assert default_props("eval") == {"cases": []}
for a in AGENT_KINDS:
    assert ("eval", a) in ALLOWED_EDGES, f"eval→{a} must be allowed"
# eval may NOT receive links (it is a source-only test node)
assert ("llm", "eval") not in ALLOWED_EDGES
ev = Node(id="e", kind="eval", name="t", x=0, y=0, props={"cases": [
    {"id": "c1", "input": "hi", "expected_output": "hello"},
    {"id": "c2", "input": "re", "expected_regex": "\\d+"},
    {"id": "c3", "input": "j", "judge": "is polite"},
    {"id": "c4", "input": "", "expected_output": "x"},      # no input → dropped
    {"id": "c5", "input": "y", "expected_output": ""},      # no expectation → dropped
]})
ids = [c["id"] for c in eval_cases(ev)]
assert ids == ["c1", "c2", "c3"], ids
print("ok 1: eval kind, eval→agent edges, eval_cases keeps usable cases only")

# ── 2. analyze rejects >1 target and empty cases ────────────────────────────
g = Graph()
a1 = g.new_node("agent", 0, 0); a1.name = "alpha"
a2 = g.new_node("agent", 0, 0); a2.name = "beta"
l1 = g.new_node("llm", 0, 0); l1.props.update(api_key="sk", model="m")
l2 = g.new_node("llm", 0, 0); l2.props.update(api_key="sk", model="m")
g.add_edge(l1.id, a1.id); g.add_edge(l2.id, a2.id)
g.add_edge(a1.id, a2.id)                       # alpha → beta chain
ev2 = g.new_node("eval", 0, 0); ev2.name = "bad"
ev2.props["cases"] = [{"id": "c", "input": "q", "expected_output": "hello"}]
g.add_edge(ev2.id, a1.id)
# force a 2nd eval→agent edge (bypassing the canvas guard) to test analyze
g.edges.append(Edge(src=ev2.id, dst=a2.id))
errs = graph_codegen.analyze(g)["errors"]
assert any("more than one agent" in e for e in errs), errs
print("ok 2: analyze flags an Eval node linked to >1 agent")

g.edges = [e for e in g.edges if not (e.src == ev2.id and e.dst == a2.id)]
ev2.props["cases"] = []                        # now empty
errs = graph_codegen.analyze(g)["errors"]
assert not any("eval" in e.lower() for e in errs), errs
print("ok 3: an EMPTY Eval node is allowed (fill cases in from the GUI)")

# ── 3. config['evals'] emission: target = the linked agent, else None ───────
ev2.props["cases"] = [{"id": "c", "input": "q", "expected_output": "hello"}]
standalone = g.new_node("eval", 0, 0); standalone.name = "whole"
standalone.props["cases"] = [{"id": "w", "input": "ping", "judge": "polite"}]
out = graph_codegen.generate_from_graph(g, "demo_eval", gui=True)
cfg = json.load(open(os.path.join(out, "config.json"), encoding="utf-8"))
evals = {e["name"]: e for e in cfg["evals"]}
assert evals["bad"]["target"] == "alpha", evals["bad"]
assert evals["whole"]["target"] is None, evals["whole"]
assert evals["bad"]["cases"][0]["id"] == "c"
print("ok 4: config['evals'] — linked node targets the agent, standalone → None")

# ── 4. generated agent's run_evals engine (no network) ──────────────────────
spec = importlib.util.spec_from_file_location(
    "demo_eval_agent", os.path.join(out, "agent.py"))
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)

assert hasattr(mod, "run_evals") and hasattr(mod, "grade")
# substring / regex grading is pure (no LLM)
assert mod.grade({"expected_output": "hello"}, "well HELLO there") is True
assert mod.grade({"expected_output": "bye"}, "hello") is False
assert mod.grade({"expected_regex": "\\d+"}, "answer 42") is True
assert mod.grade({"expected_regex": "\\d+"}, "no number") is False
print("ok 5: grade() does case-insensitive substring + regex")

# monkeypatch the pipeline LLM so react()/judge run offline; whole-pipeline run
# replies "hello world", and the judge says YES.
def fake_llm(name, system, messages, on_token=None):
    if "strict grader" in system.lower():
        return "YES", []
    return "hello world", []

mod.llm = fake_llm
# the eval engine also references llm via globals() in eval_judge → patch there
results = mod.run_evals(emit=lambda s: None)
by_name = {r["name"]: r for r in results}
# 'bad' targets 'alpha' (substring 'a' in 'hello world') → pass
assert by_name["bad"]["passed"] == 1, by_name["bad"]
# 'whole' uses the judge → YES → pass
assert by_name["whole"]["passed"] == 1 and by_name["whole"]["total"] == 1
print("ok 6: run_evals grades substring + LLM-judge offline; targets resolved")

# the run must not pollute conversation history nor leave HITL hung
assert mod.HISTORY == [], "eval cleared/restored HISTORY"
print("ok 7: run_evals isolates history")

# ── 4b. runtime-editable eval sets: CRUD persists to evals.json + run picks
#        up edits; an empty Eval node can be filled in here. ──────────────────
assert mod.eval_targets() == list(mod.PIPELINE)          # alpha, beta
n0 = len(mod.EVAL_SETS)
mod.add_eval_set("added", "beta")
assert mod.EVAL_SETS[-1] == {"name": "added", "target": "beta", "cases": []}
mod.add_eval_case(len(mod.EVAL_SETS) - 1,
                  {"id": "n1", "input": "go", "expected_output": "world"})
mod.update_eval_set(len(mod.EVAL_SETS) - 1, "renamed", None)  # → whole pipeline
assert os.path.exists(os.path.join(out, "evals.json")), "persisted"
saved = json.load(open(os.path.join(out, "evals.json"), encoding="utf-8"))
added = [s for s in saved if s["name"] == "renamed"][0]
assert added["target"] is None and added["cases"][0]["id"] == "n1"
# a fresh import reads the override (GUI edits survive a restart)
spec_b = importlib.util.spec_from_file_location(
    "demo_eval_agent_b", os.path.join(out, "agent.py"))
mod_b = importlib.util.module_from_spec(spec_b); spec_b.loader.exec_module(mod_b)
assert any(s["name"] == "renamed" for s in mod_b.EVAL_SETS), "evals.json loaded"
mod_b.llm = fake_llm
res_b = {r["name"]: r for r in mod_b.run_evals(emit=lambda s: None)}
# 'renamed' (whole pipeline) → "hello world" contains "world" → pass
assert res_b["renamed"]["passed"] == 1, res_b["renamed"]
# tidy: remove the case + set, evals.json reflects it
mod_b.remove_eval_case(
    next(i for i, s in enumerate(mod_b.EVAL_SETS) if s["name"] == "renamed"), 0)
mod_b.remove_eval_set(
    next(i for i, s in enumerate(mod_b.EVAL_SETS) if s["name"] == "renamed"))
assert not any(s["name"] == "renamed" for s in
               json.load(open(os.path.join(out, "evals.json"), encoding="utf-8")))
print("ok 8: eval sets/cases are editable + persist to evals.json; runs use them")

# ── 5. an agent with no Eval node emits no config['evals'] (no GUI menu data) ─
g3 = Graph()
a3 = g3.new_node("agent", 0, 0); a3.name = "solo"
l3 = g3.new_node("llm", 0, 0); l3.props.update(api_key="sk", model="m")
g3.add_edge(l3.id, a3.id)
out3 = graph_codegen.generate_from_graph(g3, "demo_no_eval", gui=False)
cfg3 = json.load(open(os.path.join(out3, "config.json"), encoding="utf-8"))
assert "evals" not in cfg3
# but run_evals still exists and falls back to the evals/ jsonl files
mod3_spec = importlib.util.spec_from_file_location(
    "demo_no_eval_agent", os.path.join(out3, "agent.py"))
mod3 = importlib.util.module_from_spec(mod3_spec)
mod3_spec.loader.exec_module(mod3)
assert hasattr(mod3, "run_evals")
# EVAL_SETS falls back to evals/evalset.example.jsonl as one whole-pipeline set
assert mod3.EVAL_SETS and mod3.EVAL_SETS[0]["target"] is None
print("ok 9: no Eval node → no config['evals'], engine falls back to evals/ files")

print("\nALL EVAL CHECKS PASSED")
