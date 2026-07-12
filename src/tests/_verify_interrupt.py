"""Verify user interruption (Stop): the generated agent and the coding agent
both stop cooperatively at the next checkpoint when cancel is requested."""

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


# ── 1. generated pipeline agent: cancel mid-run → "[cancelled]" ─────────────
g = Graph()
a = g.new_node("agent", 0, 0); a.name = "agent"
llm = g.new_node("llm", 0, 0); llm.props.update(api_key="sk", model="m")
g.add_edge(llm.id, a.id)
out = graph_codegen.generate_from_graph(g, "demo_interrupt", gui=False)
spec = importlib.util.spec_from_file_location(
    "demo_interrupt_agent", os.path.join(out, "agent.py"))
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
assert hasattr(mod, "request_cancel")

calls = {"n": 0}
def stub_cancel(agent_name, cfg, system, messages):
    calls["n"] += 1
    mod.request_cancel()                       # user hits Stop during the call
    return "partial answer", []
mod._call_one = stub_cancel
mod.clear_history()
res = mod.run("do it", emit=lambda s: None)
assert "cancelled" in res.lower(), res
assert calls["n"] == 1, "should stop right after the first call, not loop"
print("ok 1: generated agent stops mid-run when cancelled →", res)

# a fresh run clears the cancel flag and completes normally
def stub_ok(agent_name, cfg, system, messages):
    return "final answer", []
mod._call_one = stub_ok
mod.clear_history()
res2 = mod.run("again", emit=lambda s: None)
assert res2 == "final answer", res2
print("ok 2: the next run clears the Stop flag and completes normally")

# ── 3. coding agent: cancel between tool rounds → "[cancelled]" ─────────────
import tempfile

import coding_agent
from coding_agent import CodingAgent

# isolate session storage so constructing the agent doesn't touch real files
_cad = tempfile.mkdtemp(prefix="ca_iso_")
coding_agent.HISTORY_PATH = os.path.join(_cad, "chat_history.json")
coding_agent.SUMMARY_PATH = os.path.join(_cad, "chat_summary.txt")
coding_agent.SESSIONS_DIR = os.path.join(_cad, "sessions")

agent = CodingAgent()
agent.history = []
agent._save_history = lambda: None


class FakeClient:
    def __init__(self):
        self.last_usage = None
        self.n = 0

    def chat(self, messages, max_tokens=4096, tools=None, should_cancel=None):
        self.n += 1
        if self.n == 1:                        # round 1: make a tool call...
            agent.cancel()                     # ...and the user hits Stop
            return NS(content="", tool_calls=[
                NS(id="c1", function=NS(name="list_tools", arguments="{}"))])
        return NS(content="should not reach here", tool_calls=None)

    def cancel(self):                          # CodingAgent.cancel() force-closes
        pass                                   # the active client stream


fc = FakeClient()
agent._client = lambda: fc
reply = agent.send("write a tool", emit=lambda s: None)
assert reply == "[cancelled] stopped by the user", reply
assert fc.n == 1, "the loop must not start another round after Stop"
print("ok 3: coding agent stops between tool rounds when cancelled")

# ── 4. a freshly generated GUI has the Stop button wired ────────────────────
out_gui = graph_codegen.generate_from_graph(g, "demo_interrupt_gui", gui=True)
gui_src = open(os.path.join(out_gui, "gui.py"), encoding="utf-8").read()
assert "self.stop_btn" in gui_src and "def on_stop" in gui_src
assert "core.request_cancel()" in gui_src
agent_src = open(os.path.join(out_gui, "agent.py"), encoding="utf-8").read()
assert "def request_cancel" in agent_src and "class RunCancelled" in agent_src
print("ok 4: generated GUI emits a Stop button wired to request_cancel()")

print("\nALL INTERRUPT CHECKS PASSED")
