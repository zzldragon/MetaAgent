"""Verify the graph Designer agent (parallel to the Tool Generator).

Covers: the injectable CodingAgent core (default = byte-identical tool agent; a
second instance gets ISOLATED storage + its own brain/tools), and the designer's
graph tools (check_graph validates, write_graph saves+renders valid graphs and
REJECTS invalid ones, read/list). No LLM — the tools are exercised directly."""
import json
import os
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.chdir(BASE)

import coding_agent
import designer_agent as da

# ── 1. injectable core: default agent unchanged; designer isolated ───────────
from coding_agent import CodingAgent, SESSIONS_DIR, HISTORY_PATH

d = da.make_designer_agent()
assert d._sessions_dir != SESSIONS_DIR and d._sessions_dir.endswith(os.path.join("designer", "sessions"))
assert d._history_path != HISTORY_PATH
assert "GRAPH DESIGNER" in d._system and "write_graph" in d._local_tools
assert "save_tool" not in d._local_tools          # designer doesn't write tools
# the default coding agent still uses the module (tool-generator) storage
c = CodingAgent()
assert c._sessions_dir == SESSIONS_DIR and "save_tool" in c._local_tools
print("core ok: designer isolated (own sessions/brain/tools); tool agent unchanged")

# ── 2. graph tools ───────────────────────────────────────────────────────────
da.GRAPHS_DIR = os.path.join(tempfile.gettempdir(), "verify_designer_graphs")

VALID = {"nodes": [
    {"id": "llm_1", "kind": "llm", "name": "m", "x": 0, "y": 0,
     "props": {"provider": "siliconflow", "model": "deepseek-ai/DeepSeek-V4-Flash",
               "api_key": "", "base_url": "u"}},
    {"id": "agent_1", "kind": "agent", "name": "assistant", "x": 0, "y": 0,
     "props": {"role": "single"}}],
    "edges": [{"src": "llm_1", "dst": "agent_1", "props": {}}]}

# check_graph: valid -> no errors
review = json.loads(da._check_graph(json.dumps(VALID)))
assert review["errors"] == [], review
print("check_graph ok: validates a candidate graph (errors/warnings/metrics)")

# write_graph: valid -> saved + render hook fired
rendered = {}
da.set_graph_handler(lambda g, name: rendered.update(name=name, n=len(g.nodes)))
out = da._write_graph("verify_asst", json.dumps(VALID))
assert out.startswith("Saved and rendered"), out
assert rendered == {"name": "verify_asst", "n": 2}, rendered
assert os.path.isfile(os.path.join(da.GRAPHS_DIR, "verify_asst.mta"))
print("write_graph ok: valid graph saved + rendered on canvas (hook)")

# write_graph: invalid (agent with no LLM) -> rejected, not saved
bad = da._write_graph("verify_bad", json.dumps(
    {"nodes": [{"id": "agent_1", "kind": "agent", "name": "a", "x": 0, "y": 0, "props": {}}],
     "edges": []}))
assert bad.startswith("[not saved]") and "LLM" in bad, bad
assert not os.path.isfile(os.path.join(da.GRAPHS_DIR, "verify_bad.mta"))
print("write_graph ok: invalid graph REJECTED with errors (not saved)")

# read_graph round-trips a saved graph back to graph.json
rj = json.loads(da._read_graph("verify_asst"))
assert {n["name"] for n in rj["nodes"]} == {"m", "assistant"}, rj
assert "verify_asst.mta" in da._list_graphs()
print("read_graph / list_graphs ok")

# tool schemas are well-formed for native function calling
names = {t["function"]["name"] for t in da.DESIGNER_TOOL_DEFS}
assert names == {"list_graphs", "read_graph", "check_graph", "write_graph", "read_config_table"}
assert da.DESIGNER_HIGH_RISK == {"write_graph"}     # HITL-gated (writes disk + canvas)
print("tool schemas ok: 5 designer tools; write_graph is high-risk (HITL)")

# the designer prompt tells it to generate prompts in the user's language
_dsp = da.designer_system_prompt()
assert all(w in _dsp for w in ("Chinese", "Japanese", "French")), \
    "designer prompt must carry the language-matching rule for generated prompts"
print("language rule ok: generated prompts follow the user's language")

print("\nALL DESIGNER-AGENT CHECKS PASSED")
