"""Verify parallel tool execution: when the LLM returns several tool calls in
one turn, they run concurrently, results stay in order, usage is correct, and
the parallel_tools=false / single-call paths stay sequential."""

import importlib.util
import json
import os
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

# a single agent with one tool; the LLM opts the agent into parallel tools
g = Graph()
a = g.new_node("agent", 0, 0); a.name = "agent"
llm = g.new_node("llm", 0, 0); llm.props.update(LLM)
llm.props["parallel_tools"] = True
g.add_edge(llm.id, a.id)
tool = g.new_node("tool", 0, 0); tool.props["files"] = ["load_csv.py"]
g.add_edge(tool.id, a.id)
out_dir = graph_codegen.generate_from_graph(g, "demo_partool", gui=False)
spec = importlib.util.spec_from_file_location(
    "demo_partool_agent", os.path.join(out_dir, "agent.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

# parallel_tools is per-agent (from the primary LLM); the global default is off
cfg = json.load(open(os.path.join(out_dir, "config.json"), encoding="utf-8"))
assert cfg.get("parallel_tools") is False, "global default should be off"
assert mod.AGENTS["agent"]["parallel_tools"] is True, mod.AGENTS["agent"]
print("config ok: per-agent parallel_tools on, global default off")

# a second agent whose LLM leaves parallel_tools off stays sequential
g0 = Graph()
a0 = g0.new_node("agent", 0, 0); a0.name = "agent"
l0 = g0.new_node("llm", 0, 0); l0.props.update(LLM)  # parallel_tools defaults off
g0.add_edge(l0.id, a0.id)
out0 = graph_codegen.generate_from_graph(g0, "demo_partool_off", gui=False)
spec0 = importlib.util.spec_from_file_location(
    "demo_partool_off_agent", os.path.join(out0, "agent.py"))
mod0 = importlib.util.module_from_spec(spec0); spec0.loader.exec_module(mod0)
assert mod0.AGENTS["agent"]["parallel_tools"] is False, mod0.AGENTS["agent"]
print("default ok: an LLM without the flag yields a sequential agent")

# instrument a slow tool that records concurrency
conc = {"now": 0, "max": 0}
clock = threading.Lock()

def slow_tool(path, max_rows=20):
    with clock:
        conc["now"] += 1
        conc["max"] = max(conc["max"], conc["now"])
    time.sleep(0.15)
    with clock:
        conc["now"] -= 1
    return f"loaded {path}"

mod.TOOLS["load_csv"] = slow_tool

# the model asks for 3 tools in one turn, then finishes
rounds = {"n": 0}
def stub(agent_name, cfg_, system, messages):
    rounds["n"] += 1
    if rounds["n"] == 1:
        return "", [
            {"id": "c1", "name": "load_csv", "args": {"path": "a.csv"}},
            {"id": "c2", "name": "load_csv", "args": {"path": "b.csv"}},
            {"id": "c3", "name": "load_csv", "args": {"path": "c.csv"}},
        ]
    # the tool results must be present, in order, before the final answer
    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    assert [m["tool_call_id"] for m in tool_msgs] == ["c1", "c2", "c3"], tool_msgs
    assert "loaded a.csv" in tool_msgs[0]["content"]
    return "done", []

mod._call_one = stub
mod.clear_history()
result = mod.run("load the three files", emit=lambda s: None)
assert result == "done", result
assert conc["max"] >= 2, f"tools did not run concurrently (max={conc['max']})"
assert mod.USAGE["agent"]["tool_calls"] == 3, mod.USAGE["agent"]
print(f"parallel ok: {conc['max']} tools ran at once, ordered results, "
      "3 tool_calls counted")

# parallel_tools off for the agent -> sequential (max concurrency 1)
conc["max"] = 0; rounds["n"] = 0
mod.AGENTS["agent"]["parallel_tools"] = False
mod.USAGE["agent"]["tool_calls"] = 0
mod.clear_history()
assert mod.run("again", emit=lambda s: None) == "done"
assert conc["max"] == 1, f"should be sequential (max={conc['max']})"
mod.AGENTS["agent"]["parallel_tools"] = True
print("toggle ok: agent parallel_tools=false runs tools sequentially")

# single tool call -> sequential path, still correct
conc["max"] = 0; rounds["n"] = 0
def stub1(agent_name, cfg_, system, messages):
    rounds["n"] += 1
    if rounds["n"] == 1:
        return "", [{"id": "s1", "name": "load_csv", "args": {"path": "x.csv"}}]
    return "ok", []
mod._call_one = stub1
mod.USAGE["agent"]["tool_calls"] = 0
mod.clear_history()
assert mod.run("one file", emit=lambda s: None) == "ok"
assert conc["max"] == 1
print("single-call ok")

# a high-risk / side-effecting tool in the batch forces sequential
mod.AGENTS["agent"]["parallel_tools"] = True
assert mod.is_parallel_safe("load_csv") is True
assert mod.is_parallel_safe("save_report") is False   # 'save' = high-risk
assert mod.is_parallel_safe("delete_rows") is False
mod.CONFIG["sequential_tools"] = ["load_csv"]
assert mod.is_parallel_safe("load_csv") is False       # explicit override
mod.CONFIG["sequential_tools"] = []
mod.CONFIG["parallel_safe_tools"] = ["save_report"]
assert mod.is_parallel_safe("save_report") is True     # explicit allow
mod.CONFIG["parallel_safe_tools"] = []
print("parallel-safety classification ok: writes serial, lists override")

# a mixed batch (read + write) runs sequentially even with parallel_tools on
mod.TOOLS["save_thing"] = slow_tool   # 'save' → not parallel-safe
conc["max"] = 0; rounds["n"] = 0
def stub_mixed(agent_name, cfg_, system, messages):
    rounds["n"] += 1
    if rounds["n"] == 1:
        return "", [
            {"id": "m1", "name": "load_csv", "args": {"path": "a.csv"}},
            {"id": "m2", "name": "save_thing", "args": {"path": "b.csv"}},
        ]
    return "done", []
mod._call_one = stub_mixed
mod.USAGE["agent"]["tool_calls"] = 0
mod.clear_history()
assert mod.run("read then save", emit=lambda s: None) == "done"
assert conc["max"] == 1, f"unsafe batch must be sequential (max={conc['max']})"
print("mixed-batch ok: read+write batch ran sequentially")

print("\nALL PARALLEL-TOOL CHECKS PASSED")
