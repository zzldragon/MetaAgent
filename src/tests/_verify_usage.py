"""Verify token-usage + context reporting in the generated agent (pipeline) and
the coding agent. Cost was removed (token is what matters), so the [usage] line
must show tokens + context and NO '$' figure. No network — LLM calls are stubbed."""

import importlib.util
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.chdir(BASE)

import graph_codegen
from graph_model import Graph


class NS:
    def __init__(self, **k):
        self.__dict__.update(k)


# ── 1. generated pipeline agent: run() emits a usage line; total_usage() ────
g = Graph()
a = g.new_node("agent", 0, 0); a.name = "agent"
llm = g.new_node("llm", 0, 0)
llm.props.update(provider="siliconflow", model="deepseek-ai/DeepSeek-V4-Flash",
                 api_key="sk")
g.add_edge(llm.id, a.id)
out = graph_codegen.generate_from_graph(g, "demo_usage", gui=False)
spec = importlib.util.spec_from_file_location(
    "demo_usage_agent", os.path.join(out, "agent.py"))
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)

# stub the LLM: record usage via _track (as the real _call_one would), no tools
def stub(agent_name, cfg, system, messages):
    mod._track(agent_name, cfg, 120, 30)
    return "final answer", []
mod._call_one = stub
mod.clear_history()

lines = []
res = mod.run("do it", emit=lines.append)
assert res == "final answer"
u = mod.total_usage()
assert u["input_tokens"] == 120 and u["output_tokens"] == 30, u
assert "cost_usd" not in u, "cost must be removed from total_usage()"
usage_lines = [l for l in lines if l.startswith("[usage]")]
assert usage_lines, "run() should emit a [usage] line"
assert "120 in" in usage_lines[0] and "30 out" in usage_lines[0], usage_lines[0]
assert "$" not in usage_lines[0], "cost must be gone from the usage line"
assert "context:" in usage_lines[0], "context must appear in the usage line"
print("ok 1: generated pipeline agent reports token usage + context, no cost")
print("     ", usage_lines[0])

# a second run accumulates into the session total, per-run delta resets
lines2 = []
mod.clear_history()
mod.run("again", emit=lines2.append)
assert mod.total_usage()["input_tokens"] == 240   # 120 + 120 session
u_line = [l for l in lines2 if l.startswith("[usage]")][0]
assert "this run: 120 in" in u_line and "session: 240 in" in u_line, u_line
assert "$" not in u_line
print("ok 2: session total accumulates; per-run delta is reported separately")

# the generated config carries no price knobs anymore
import json as _json
cfg_json = _json.load(open(os.path.join(out, "config.json"), encoding="utf-8"))
for _agent, _cfgs in cfg_json["llms"].items():
    for _c in _cfgs:
        assert "price_per_1m_input_usd" not in _c and "price_per_1m_output_usd" not in _c, _c
print("ok 2b: generated config.json has no price_per_1m_* knobs")

# ── 3. coding agent: accumulates token usage + context, emits a usage line ──
import tempfile

import coding_agent
from coding_agent import CodingAgent

# isolate session storage so constructing the agent doesn't touch real files
_cad = tempfile.mkdtemp(prefix="ca_iso_")
coding_agent.HISTORY_PATH = os.path.join(_cad, "chat_history.json")
coding_agent.SUMMARY_PATH = os.path.join(_cad, "chat_summary.txt")
coding_agent.SESSIONS_DIR = os.path.join(_cad, "sessions")


class FakeClient:
    def __init__(self):
        self.last_usage = None

    def chat(self, messages, max_tokens=4096, tools=None, should_cancel=None):
        self.last_usage = NS(prompt_tokens=100, completion_tokens=20)
        return NS(content="here you go", tool_calls=None)


agent = CodingAgent()
agent.history = []
agent._save_history = lambda: None        # don't touch the real chat history
agent._client = lambda: FakeClient()
clines = []
reply = agent.send("write a tool", emit=clines.append)
assert reply == "here you go"
assert agent.usage == {"input_tokens": 100, "output_tokens": 20}, agent.usage
assert not hasattr(agent, "cost_usd"), "cost_usd() must be removed"
cu = [l for l in clines if l.startswith("[usage]")]
assert cu and "100 in" in cu[0] and "20 out" in cu[0], cu
assert "$" not in cu[0], "cost must be gone from the coding agent usage line"
assert "context:" in cu[0], "context must appear in the coding agent usage line"
print("ok 3: coding agent reports token usage + context, no cost")
print("     ", cu[0])

print("\nALL USAGE CHECKS PASSED")
