"""Verify the worker pool: graph rules, subtask splitting, parallel execution
with thread-safe usage accounting, ordered merge, and fault isolation."""

import importlib.util
import os
import py_compile
import sys
import threading
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.chdir(BASE)

import graph_codegen
from graph_model import Graph

LLM = {"provider": "siliconflow", "model": "deepseek-ai/DeepSeek-V4-Flash",
       "api_key": "sk-test", "base_url": "https://api.siliconflow.cn/v1"}


def _agent(g, name, kind="agent", **props):
    n = g.new_node(kind, 0, 0)
    n.name = name
    n.props.update(props)
    llm = g.new_node("llm", 0, 0)
    llm.props.update(LLM)
    assert g.add_edge(llm.id, n.id) is None
    return n


# 1. Graph: planner -> pool -> critic, pool fans out
g = Graph()
planner = _agent(g, "planner", role="planner")
pool = _agent(g, "pool", kind="workerpool", role="worker", max_workers=3)
critic = _agent(g, "critic", role="critic")
assert g.add_edge(planner.id, pool.id) is None
assert g.add_edge(pool.id, critic.id) is None

info = graph_codegen.analyze(g)
assert not info["errors"], info["errors"]
assert [g.nodes[a].name for a in info["pipeline"]] == ["planner", "pool", "critic"]
print("pool graph rules ok: planner -> pool -> critic")

# bad max_workers is rejected
pool.props["max_workers"] = 0
assert any("max_workers" in e for e in graph_codegen.analyze(g)["errors"])
pool.props["max_workers"] = 3
print("pool validation ok: max_workers >= 1")

# 2. Generate + inspect spec
out_dir = graph_codegen.generate_from_graph(g, "demo_pool", gui=False)
py_compile.compile(os.path.join(out_dir, "agent.py"), doraise=True)
spec = importlib.util.spec_from_file_location(
    "demo_pool_agent", os.path.join(out_dir, "agent.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
assert mod.AGENTS["pool"]["pool"] is True
assert mod.AGENTS["pool"]["max_workers"] == 3
assert mod.AGENTS["planner"].get("pool") is None
print("pool codegen ok: pool flag + max_workers in spec")

# 3. Subtask splitting
assert mod._pool_split_tasks("1. alpha\n2. beta\n3. gamma") == ["alpha", "beta", "gamma"]
assert mod._pool_split_tasks('Plan: ["search X", "compute Y"]') == ["search X", "compute Y"]
assert mod._pool_split_tasks("- a\n- b") == ["a", "b"]
assert mod._pool_split_tasks("just one prose paragraph") == ["just one prose paragraph"]
print("subtask split ok: numbered / json / bullet / single")

# 4. Parallel execution + thread-safe usage + ordered merge
conc = {"now": 0, "max": 0}
clock = threading.Lock()

def stub(agent_name, cfg, system, messages):
    with clock:
        conc["now"] += 1
        conc["max"] = max(conc["max"], conc["now"])
    time.sleep(0.15)
    mod._track(agent_name, cfg, 100, 50)   # exercises the usage lock
    with clock:
        conc["now"] -= 1
    first = messages[0]["content"].splitlines()[0]
    return "RESULT(" + first + ")", []

mod._call_one = stub
mod.USAGE["pool"] = {"input_tokens": 0, "output_tokens": 0, "tool_calls": 0}
merged = mod.run_pool("pool", "1. alpha\n2. beta\n3. gamma", emit=lambda s: None)
assert conc["max"] >= 2, f"workers did not run in parallel (max={conc['max']})"
# ordered merge: alpha before beta before gamma
assert merged.index("alpha") < merged.index("beta") < merged.index("gamma"), merged
assert merged.count("RESULT(") == 3
# usage merged with no lost updates (3 workers x 100/50)
assert mod.USAGE["pool"]["input_tokens"] == 300, mod.USAGE["pool"]
assert mod.USAGE["pool"]["output_tokens"] == 150
print(f"parallel ok: max {conc['max']} concurrent, ordered merge, usage=300/150")

# 5. Fault isolation: one failing subtask doesn't sink the batch
def flaky(agent_name, cfg, system, messages):
    if "Subtask 2 of 3" in messages[0]["content"]:
        raise RuntimeError("boom")
    return "ok", []

mod._call_one = flaky
merged = mod.run_pool("pool", "1. a\n2. b\n3. c", emit=lambda s: None)
assert "[ERROR] subtask 2 failed" in merged, merged
assert merged.count("\nok") >= 2 or merged.count("ok") >= 2, merged
print("fault isolation ok: subtask 2 failed, others completed")

# 6. run_stage routes pool vs plain agent
calls = []
mod._call_one = lambda an, c, s, m: (calls.append(an) or ("x", []))
mod.run_stage("planner", "hello", emit=lambda s: None)   # single agent -> 1 call
n_after_planner = len(calls)
mod.run_stage("pool", "1. a\n2. b", emit=lambda s: None)  # pool -> 2 calls
assert n_after_planner == 1 and len(calls) == 3, calls
print("run_stage ok: agent runs once, pool fans out")

print("\nALL POOL CHECKS PASSED")
