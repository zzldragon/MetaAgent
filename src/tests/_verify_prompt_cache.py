"""Verify prompt-cache-friendly system-prompt layout (roadmap item 5): the
VOLATILE workspace file listing is appended AFTER a byte-stable prefix
(persona + skills + guidance), instead of in the middle. The decisive property:
the no-workspace system prompt is a PREFIX of the with-workspace one, and the
stable prefix does NOT shift when the workspace listing changes — so a provider
can cache the long stable prefix across calls even as the agent writes files.
(Under the OLD order — workspace before guidance — these assertions fail.)"""

import importlib.util
import os
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.chdir(BASE)

import graph_codegen
from graph_model import Graph

LLM = {"provider": "siliconflow", "model": "deepseek-ai/DeepSeek-V4-Flash",
       "api_key": "sk-test", "base_url": "https://api.siliconflow.cn/v1"}

# single agent WITH a tool (so guidance is the "Use the provided tools" variant)
g = Graph()
a = g.new_node("agent", 0, 0); a.name = "agent"
llm = g.new_node("llm", 0, 0); llm.props.update(LLM)
g.add_edge(llm.id, a.id)
tool = g.new_node("tool", 0, 0); tool.props["files"] = ["load_csv.py"]
g.add_edge(tool.id, a.id)
out = graph_codegen.generate_from_graph(g, "demo_prompt_cache", gui=False)
spec = importlib.util.spec_from_file_location("demo_prompt_cache_agent",
                                              os.path.join(out, "agent.py"))
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)

# start from a clean (no workspace) state
wj = os.path.join(out, "workspace.json")
if os.path.exists(wj):
    os.remove(wj)
mod.set_workspace([])

GUIDE = "Use the provided tools"        # stable guidance marker (tools branch)

s_empty = mod.build_system("agent")
assert "# Workspace" not in s_empty, "no workspace → no workspace section"
assert GUIDE in s_empty, "guidance must be in the stable prefix"
print("ok 1: empty-workspace system prompt = stable persona+skills+guidance only")

# add a workspace folder with a file → the listing must be a pure SUFFIX
tmp = tempfile.mkdtemp(prefix="pc_ws_")
open(os.path.join(tmp, "alpha.txt"), "w").close()
mod.set_workspace([tmp])
s1 = mod.build_system("agent")
assert "# Workspace" in s1 and "alpha.txt" in s1, "workspace listing missing"
assert s1.startswith(s_empty), "stable prefix shifted — workspace not appended last"
assert s1.index(GUIDE) < s1.index("# Workspace"), "guidance must precede the listing"
print("ok 2: workspace listing is appended AFTER the stable prefix (guidance first)")

# mutate the workspace (agent 'writes' a file) → stable prefix stays byte-identical
open(os.path.join(tmp, "beta.txt"), "w").close()
s2 = mod.build_system("agent")
assert "beta.txt" in s2, "new file not reflected"
assert s2.startswith(s_empty), "stable prefix shifted after a file change"
assert s1[:len(s_empty)] == s_empty == s2[:len(s_empty)], "prefix not byte-stable"
# only the volatile suffix differs between the two workspace states
assert s1[:s1.index("# Workspace")] == s2[:s2.index("# Workspace")], \
    "everything before the listing must be identical across calls"
print("ok 3: workspace change touches only the trailing listing; prefix is byte-stable")

# no-tools agent: guidance differs but is still in the stable prefix, listing last
g0 = Graph()
a0 = g0.new_node("agent", 0, 0); a0.name = "solo"
l0 = g0.new_node("llm", 0, 0); l0.props.update(LLM)
g0.add_edge(l0.id, a0.id)
out0 = graph_codegen.generate_from_graph(g0, "demo_prompt_cache_notool", gui=False)
m0spec = importlib.util.spec_from_file_location("demo_prompt_cache_notool_agent",
                                                os.path.join(out0, "agent.py"))
mod0 = importlib.util.module_from_spec(m0spec); m0spec.loader.exec_module(mod0)
wj0 = os.path.join(out0, "workspace.json")
if os.path.exists(wj0):
    os.remove(wj0)
e0 = mod0.build_system("solo")
mod0.set_workspace([tmp])
f0 = mod0.build_system("solo")
assert "answer directly" in e0, "no-tools guidance missing"
assert f0.startswith(e0) and f0.index("answer directly") < f0.index("# Workspace")
print("ok 4: no-tools agent also keeps guidance in the prefix, listing last")

# cleanup
mod.set_workspace([]); mod0.set_workspace([])
for f in ("alpha.txt", "beta.txt"):
    p = os.path.join(tmp, f)
    if os.path.exists(p):
        os.remove(p)
os.rmdir(tmp)
for w in (wj, wj0):
    if os.path.exists(w):
        os.remove(w)

print("\nPROMPT-CACHE LAYOUT CHECKS PASSED")
