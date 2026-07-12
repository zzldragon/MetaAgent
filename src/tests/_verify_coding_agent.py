"""Offline verification of the MainFrame coding agent's native function
calling: library tools + the agentic send() loop with a stubbed LLM client."""

import os
import shutil
import sys
import tempfile
from types import SimpleNamespace as NS

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.chdir(BASE)

import coding_agent
from coding_agent import CodingAgent, _list_tools, _read_tool, _save_tool

# 1. Library tools work directly
listing = _list_tools()
assert "load_csv.py" in listing, listing
good_code = (
    "from langchain_core.tools import tool\n\n\n"
    "@tool\n"
    "def temp_test_tool(x: str) -> str:\n"
    '    """Echo the input."""\n'
    "    return x\n"
)
r = _save_tool("temp_test_tool", good_code)
assert r.startswith("Saved"), r
assert "temp_test_tool.py" in _list_tools()
assert "def temp_test_tool" in _read_tool("temp_test_tool")
assert _read_tool("no_such_thing").startswith("[ERROR]")
bad = _save_tool("bad_tool", "def broken(:\n    pass")
assert bad.startswith("[ERROR]") and "syntax" in bad.lower(), bad
assert not os.path.exists(os.path.join(coding_agent.TOOLS_DIR, "bad_tool.py"))
print("library tools ok: list / read / save / syntax guard")

# 1b. GUI "Save Tool(s)" path (CodingAgent.save_tool) goes through the SAME
#     validation as the LLM-driven _save_tool — no verbatim writes.
gui_ok = CodingAgent.save_tool("gui_test_tool", good_code)
assert gui_ok.startswith("Saved"), gui_ok
assert "gui_test_tool.py" in _list_tools()
# broken code is rejected, not written
gui_bad = CodingAgent.save_tool("gui_bad_tool", "def broken(:\n    pass")
assert gui_bad.startswith("[ERROR]") and "syntax" in gui_bad.lower(), gui_bad
assert not os.path.exists(os.path.join(coding_agent.TOOLS_DIR, "gui_bad_tool.py"))
# non-identifier name is sanitized to the same file the LLM path would use
gui_name = CodingAgent.save_tool("weird name!", good_code.replace("temp_test_tool", "weird_name"))
assert gui_name.startswith("Saved") and "weird_name.py" in gui_name, gui_name
for _f in ("gui_test_tool.py", "weird_name.py"):
    _p = os.path.join(coding_agent.TOOLS_DIR, _f)
    if os.path.exists(_p):
        os.remove(_p)
print("gui save path ok: validates name + syntax (no verbatim write)")

# 1c. Edge cases shared by both paths (LLM-driven _save_tool and GUI save_tool).
_created = []


def _cleanup(*files):
    for f in files:
        p = os.path.join(coding_agent.TOOLS_DIR, f)
        if os.path.exists(p):
            os.remove(p)


# (a) empty-after-sanitize name -> error, nothing written. "!!!" -> "_" -> "".
for _writer in (_save_tool, CodingAgent.save_tool):
    r = _writer("!!!", good_code)
    assert r == "[ERROR] Invalid tool name.", (_writer, r)
# stray ".py" name would only be created if the guard failed
assert not os.path.exists(os.path.join(coding_agent.TOOLS_DIR, ".py"))

# (b) a ".py" suffix is stripped, not turned into "_py" — file is "suffixed.py".
sfx = good_code.replace("temp_test_tool", "suffixed")
r = CodingAgent.save_tool("suffixed.py", sfx)
assert r.startswith("Saved") and os.path.basename(r.split("Saved to ")[1]) == "suffixed.py", r
assert os.path.exists(os.path.join(coding_agent.TOOLS_DIR, "suffixed.py"))
assert not os.path.exists(os.path.join(coding_agent.TOOLS_DIR, "suffixed_py.py"))
_created.append("suffixed.py")

# (c) trailing newline is guaranteed even when the source lacks one.
no_nl = good_code.replace("temp_test_tool", "no_newline").rstrip("\n")
assert not no_nl.endswith("\n")
CodingAgent.save_tool("no_newline", no_nl)
with open(os.path.join(coding_agent.TOOLS_DIR, "no_newline.py"), encoding="utf-8") as f:
    assert f.read().endswith("\n")
_created.append("no_newline.py")

# (d) both paths produce a BYTE-IDENTICAL file for the same (name, code) input.
eq = good_code.replace("temp_test_tool", "equiv_tool")
_save_tool("equiv_tool", eq)
with open(os.path.join(coding_agent.TOOLS_DIR, "equiv_tool.py"), "rb") as f:
    from_llm = f.read()
CodingAgent.save_tool("equiv_tool", eq)          # overwrite via the GUI path
with open(os.path.join(coding_agent.TOOLS_DIR, "equiv_tool.py"), "rb") as f:
    from_gui = f.read()
assert from_llm == from_gui, "paths diverge on identical input"
_created.append("equiv_tool.py")

# (e) overwrite updates content (idempotent path, last write wins).
v2 = eq.replace("return x", "return x.upper()")
CodingAgent.save_tool("equiv_tool", v2)
assert "x.upper()" in _read_tool("equiv_tool")

_cleanup(*_created)
print("edge cases ok: empty-name guard / .py suffix / trailing nl / path equivalence / overwrite")

# 2. Agentic loop with a stubbed client (session storage redirected to a temp dir
#    so the agent's sessions/ persistence never touches the real project files)
_sess_tmp = tempfile.mkdtemp(prefix="ca_verify_")
coding_agent.HISTORY_PATH = os.path.join(_sess_tmp, "chat_history.json")
coding_agent.SUMMARY_PATH = os.path.join(_sess_tmp, "chat_summary.txt")
coding_agent.SESSIONS_DIR = os.path.join(_sess_tmp, "sessions")


class FakeClient:
    def __init__(self):
        self.round = 0

    def chat(self, messages, max_tokens=4096, tools=None, should_cancel=None):
        self.round += 1
        if self.round == 1:
            assert tools, "TOOL_DEFS must be passed to the LLM"
            assert messages[0]["role"] == "system"
            return NS(content="", tool_calls=[
                NS(id="c1", function=NS(name="list_tools", arguments="{}"))])
        # round 2: the tool result must have come back as a tool message
        assert messages[-1]["role"] == "tool", messages[-1]
        assert "load_csv.py" in messages[-1]["content"]
        return NS(content="The library contains load_csv and more.",
                  tool_calls=None)


agent = CodingAgent()
agent.history = []
agent._client = lambda: FakeClient()
traces = []
reply = agent.send("what tools do we have?", emit=traces.append)
assert reply == "The library contains load_csv and more.", reply
assert any(t.startswith("[tool] list_tools") for t in traces), traces
assert any(t.startswith("[tool result]") for t in traces), traces
assert agent.history[-1]["content"] == reply
print("agentic loop ok: tool called, result fed back, reply persisted")

# 3. Unknown tool name is handled gracefully
class FakeClient2:
    def __init__(self):
        self.round = 0

    def chat(self, messages, max_tokens=4096, tools=None, should_cancel=None):
        self.round += 1
        if self.round == 1:
            return NS(content="", tool_calls=[
                NS(id="c2", function=NS(name="bogus", arguments="{}"))])
        assert "[ERROR] Unknown tool" in messages[-1]["content"]
        return NS(content="sorry, no such tool", tool_calls=None)


agent2 = CodingAgent()
agent2.history = []
agent2._client = lambda: FakeClient2()
assert agent2.send("x", emit=lambda s: None) == "sorry, no such tool"
print("unknown-tool handling ok")

# 4. HITL: save_tool requires confirmation; denial blocks the disk write
import json as _json

GOOD2 = good_code.replace("temp_test_tool", "temp_hitl_tool")
SAVE_ARGS = _json.dumps({"name": "temp_hitl_tool", "code": GOOD2})


class FakeSaveClient:
    def __init__(self):
        self.round = 0

    def chat(self, messages, max_tokens=4096, tools=None, should_cancel=None):
        self.round += 1
        if self.round == 1:
            return NS(content="", tool_calls=[
                NS(id="s1", function=NS(name="save_tool", arguments=SAVE_ARGS))])
        return NS(content="acknowledged: " + messages[-1]["content"][:20],
                  tool_calls=None)


hitl_path = os.path.join(coding_agent.TOOLS_DIR, "temp_hitl_tool.py")
asked = []

# 4a. read-only tools never prompt
coding_agent.set_confirm_handler(
    lambda name, args: asked.append(name) or False)
assert coding_agent.confirm_tool("list_tools", {}) is True
assert coding_agent.confirm_tool("read_tool", {"name": "x"}) is True
assert asked == [], "read-only tools must not prompt"

# 4b. deny -> no file written, [denied] fed back to the model
agent3 = CodingAgent()
agent3.history = []
agent3._client = lambda: FakeSaveClient()
reply = agent3.send("save it", emit=lambda s: None)
assert asked == ["save_tool"], asked
assert not os.path.exists(hitl_path), "denied save must not write the file"
assert reply.startswith("acknowledged: [denied]"), reply
print("hitl deny ok: prompt shown, file NOT written")

# 4c. allow -> file written
coding_agent.set_confirm_handler(lambda name, args: True)
agent4 = CodingAgent()
agent4.history = []
agent4._client = lambda: FakeSaveClient()
agent4.send("save it", emit=lambda s: None)
assert os.path.exists(hitl_path), "allowed save must write the file"
os.remove(hitl_path)
print("hitl allow ok: file written after confirmation")

# 4d. hitl_confirm=False in config bypasses the prompt
asked2 = []
coding_agent.set_confirm_handler(lambda name, args: asked2.append(name) or False)
coding_agent.load_config = lambda: {"hitl_confirm": False}
assert coding_agent.confirm_tool("save_tool", {}) is True
assert asked2 == [], "disabled HITL must not prompt"
print("hitl toggle ok: config can disable confirmation")

# 5. sessions: a sent conversation is recoverable; New Session starts fresh
sid = agent4.current_session()
assert any(s["id"] == sid for s in agent4.list_sessions()), "active session listed"
agent4.new_session()
assert agent4.history == [] and agent4.current_session() != sid
assert agent4.load_session(sid) and agent4.history, "recover the prior session"
print("sessions ok: list / new / recover")

# 5b. Clear History wipes ALL sessions and starts a fresh empty one
agent4.load_session(sid)
_before = len(agent4.list_sessions())
_old = agent4.current_session()
_removed = agent4.clear_all_sessions()
assert _removed >= 1 and agent4.history == [] and agent4.current_session() != _old
assert all(s["turns"] == 0 for s in agent4.list_sessions()), "no old session survives"
print("clear-history ok: all sessions removed, fresh empty session started")

# 5c. the system prompt tells the agent to match the user's language
assert all(w in coding_agent.CODING_SYSTEM for w in ("Chinese", "Japanese", "French")), \
    "CODING_SYSTEM must carry the language-matching rule"
print("language rule ok: generated text follows the user's language")

# 6. per-instance tool-round budget: default is the module cap; <=0 is UNLIMITED.
#    The graph Designer + Tool Generator run unlimited (multi-step work), so an
#    agent must be able to run PAST MAX_TOOL_ROUNDS when uncapped.
assert CodingAgent()._max_tool_rounds == coding_agent.MAX_TOOL_ROUNDS
assert CodingAgent(max_tool_rounds=0)._max_tool_rounds <= 0        # unlimited
import designer_agent as _da
assert _da.make_designer_agent()._max_tool_rounds <= 0, "Designer must be unlimited"


class _ManyRoundsClient:
    """Calls a harmless real tool (list_tools) for N rounds, then answers."""
    def __init__(self, n):
        self.round = 0
        self.n = n

    def chat(self, messages, max_tokens=4096, tools=None, should_cancel=None):
        self.round += 1
        if self.round <= self.n:
            return NS(content="", tool_calls=[
                NS(id="r%d" % self.round, function=NS(name="list_tools", arguments="{}"))])
        return NS(content="done after many rounds", tool_calls=None)


_N = coding_agent.MAX_TOOL_ROUNDS + 3        # deliberately beyond the default cap
# capped (default) stops early with the round-exhausted sentinel
_capped = CodingAgent()
_capped.history = []
_capped._client = lambda: _ManyRoundsClient(_N)
assert _capped.send("go", emit=lambda s: None) == "(stopped after too many tool rounds)"
# unlimited runs all the way to the real answer
_unl = CodingAgent(max_tool_rounds=0)
_unl.history = []
_unl._client = lambda: _ManyRoundsClient(_N)
assert _unl.send("go", emit=lambda s: None) == "done after many rounds"
print("budget ok: default caps at MAX_TOOL_ROUNDS; unlimited (Designer/Tool Generator) "
      "runs past it to completion")

# cleanup
os.remove(os.path.join(coding_agent.TOOLS_DIR, "temp_test_tool.py"))
shutil.rmtree(_sess_tmp, ignore_errors=True)
print("\nCODING AGENT CHECKS PASSED")
