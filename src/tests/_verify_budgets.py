"""Verify budgets default to UNLIMITED (0) and that an explicit cap still works.
0 must mean: no iteration / tool-call / wall-clock limit (so demos run to completion),
while a positive value still bounds the run. Offline — _call_one is stubbed."""
import importlib.util
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.chdir(BASE)

import graph_codegen
from graph_model import DEFAULT_BUDGETS, Graph

assert all(v == 0 for v in DEFAULT_BUDGETS.values()), DEFAULT_BUDGETS
print("defaults ok: every budget is 0 (unlimited)")

g = Graph()
llm = g.new_node("llm", 300, 0); llm.name = "m"
llm.props.update(provider="siliconflow", model="deepseek-ai/DeepSeek-V4-Flash",
                 api_key="", base_url="https://api.siliconflow.cn/v1")
a = g.new_node("agent", 0, 0); a.name = "a"; a.props["role"] = "single"
for k in DEFAULT_BUDGETS:
    a.props[k] = DEFAULT_BUDGETS[k]
g.add_edge(llm.id, a.id)

out = graph_codegen.generate_from_graph(g, "verify_budgets", gui=False)
spec = importlib.util.spec_from_file_location("vbud", os.path.join(out, "agent.py"))
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
assert mod.AGENTS["a"]["budgets"] == {"max_iterations": 0, "max_tool_calls": 0,
                                      "max_output_tokens": 0, "max_wall_clock_s": 0}, \
    mod.AGENTS["a"]["budgets"]
print("codegen ok: generated agent carries all-0 budgets")

# a tool the stubbed model will call; count real executions
_exec = {"n": 0}


def _noop(**kw):
    _exec["n"] += 1
    return "done"


mod.TOOLS["noop"] = _noop
mod.AGENTS["a"]["tools"].append("noop")

# stub: call the tool for `_upto` steps, then answer
_state = {"upto": 25}
_calls = {"n": 0}


def _stub(agent_name, cfg, system, messages):
    _calls["n"] += 1
    if _calls["n"] <= _state["upto"]:
        return ("", [{"id": f"c{_calls['n']}", "name": "noop", "args": {}}])
    return ("final answer", [])


mod._call_one = _stub

# ── 1. UNLIMITED: 25 tool calls (past the OLD 10-iter / 20-tool caps) complete ─
_calls["n"] = 0; _exec["n"] = 0; mod.clear_history()
r = mod.run("go", emit=lambda s: None)
assert r == "final answer", r                      # no early "[budget]" stop
assert _exec["n"] == 25, _exec["n"]                # all 25 tool calls actually ran
assert "[budget]" not in r
print("run ok: 0 budgets = unlimited — 25 iterations / tool calls ran to completion")

# ── 2. an explicit tool-call cap still bounds the run ────────────────────────
mod.AGENTS["a"]["budgets"]["max_tool_calls"] = 2
_calls["n"] = 0; _exec["n"] = 0; mod.clear_history()
mod.run("go", emit=lambda s: None)
assert _exec["n"] == 2, _exec["n"]                 # only 2 tools executed; rest budget-blocked
mod.AGENTS["a"]["budgets"]["max_tool_calls"] = 0   # restore unlimited
print("run ok: an explicit max_tool_calls cap still limits execution")

# ── 3. an explicit iteration cap still trips ─────────────────────────────────
mod.AGENTS["a"]["budgets"]["max_iterations"] = 3
_state["upto"] = 10_000                             # never answers -> hits the cap
_calls["n"] = 0; mod.clear_history()
r = mod.run("go", emit=lambda s: None)
assert "[budget] Iteration limit" in r, r
mod.AGENTS["a"]["budgets"]["max_iterations"] = 0
print("run ok: an explicit max_iterations cap still trips")

# ── 4. wall-clock: 0 = no check, -1 still trips (the offline-test trick) ──────
mod.AGENTS["a"]["budgets"]["max_wall_clock_s"] = -1
_calls["n"] = 0; mod.clear_history()
r = mod.run("go", emit=lambda s: None)
assert "[budget] Wall-clock" in r, r
print("run ok: max_wall_clock_s=-1 still trips (0 = no wall-clock check)")

print("\nALL BUDGET CHECKS PASSED")
