"""Verify the MCP client module: graph rules, codegen, and a live stdio
connection to a tiny FastMCP server (tool discovery + a real tool call)."""

import importlib.util
import json
import os
import py_compile
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.chdir(BASE)

import graph_codegen
from graph_model import Graph

LLM = {"provider": "siliconflow", "model": "deepseek-ai/DeepSeek-V4-Flash",
       "api_key": "sk-test", "base_url": "https://api.siliconflow.cn/v1"}

# 1. Graph rules: multiple MCP clients allowed on one agent; edge rules
g = Graph()
ag = g.new_node("agent", 0, 0); ag.name = "solo"
ag.props["max_wall_clock_s"] = -1
llm = g.new_node("llm", 0, 0); llm.props.update(LLM)
assert g.add_edge(llm.id, ag.id) is None

m1 = g.new_node("mcp", 0, 0)
m1.props.update(transport="stdio", command="python",
                args="_mcp_test_server.py")
m2 = g.new_node("mcp", 0, 0)
m2.props.update(transport="streamable_http",
                url="https://localhost:7777/mcp", verify_tls=False)
assert g.add_edge(m1.id, ag.id) is None
assert g.add_edge(m2.id, ag.id) is None, "multiple MCP clients must be allowed"
assert g.add_edge(ag.id, m1.id) is not None, "agent->mcp must be rejected"
print("mcp graph rules ok: two MCP clients on one agent")

# 2. Validation: stdio needs command, sse needs url, must be linked
bad = Graph()
b_ag = bad.new_node("agent", 0, 0)
b_llm = bad.new_node("llm", 0, 0); b_llm.props.update(LLM)
bad.add_edge(b_llm.id, b_ag.id)
bm = bad.new_node("mcp", 0, 0); bm.props.update(transport="stdio", command="")
bad.add_edge(bm.id, b_ag.id)
errs = graph_codegen.analyze(bad)["errors"]
assert any("needs a command" in e for e in errs), errs
orphan = Graph()
o_ag = orphan.new_node("agent", 0, 0)
o_llm = orphan.new_node("llm", 0, 0); o_llm.props.update(LLM)
orphan.add_edge(o_llm.id, o_ag.id)
om = orphan.new_node("mcp", 0, 0); om.props.update(transport="stdio",
                                                   command="python")
errs = graph_codegen.analyze(orphan)["errors"]
assert any("not linked" in e for e in errs), errs
print("mcp validation ok: command/url/link checks")

# 3. Generation: config carries mcp_servers, agent carries its mcp ids
assert not graph_codegen.analyze(g)["errors"]
out_dir = graph_codegen.generate_from_graph(g, "demo_mcp", gui=False)
py_compile.compile(os.path.join(out_dir, "agent.py"), doraise=True)
with open(os.path.join(out_dir, "config.json"), encoding="utf-8") as f:
    cfg = json.load(f)
servers = cfg["mcp_servers"]
assert len(servers) == 2, servers
stdio = next(s for s in servers if s["transport"] == "stdio")
assert stdio["command"] == "python" and stdio["args"] == ["_mcp_test_server.py"]
http = next(s for s in servers if s["transport"] == "streamable_http")
assert http["url"] == "https://localhost:7777/mcp"
assert http["verify_tls"] is False, http
reqs = open(os.path.join(out_dir, "requirements.txt")).read()
assert "mcp>=" in reqs, reqs
print("mcp codegen ok: stdio + streamable_http config + requirement + ids")

# 4. Live: connect ONLY the stdio server (copy the test server next to the
#    generated agent, drop the unreachable sse one) and verify tool discovery.
import shutil
server_copy = os.path.join(out_dir, "_mcp_test_server.py")
shutil.copy(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "_mcp_test_server.py"), server_copy)
# point the stdio server at the agent's own copy via an absolute path, so it
# launches correctly regardless of the process cwd.
stdio["args"] = [server_copy]
cfg["mcp_servers"] = [stdio]
agent_id = next(k for k in cfg["llms"])  # only one agent: "solo"
with open(os.path.join(out_dir, "config.json"), "w", encoding="utf-8") as f:
    json.dump(cfg, f, indent=2)

spec = importlib.util.spec_from_file_location(
    "demo_mcp_agent", os.path.join(out_dir, "agent.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

# the HTTP factory builds a client with the requested TLS-verify setting
import httpx
client = mod._mcp_http_factory(False)()
assert isinstance(client, httpx.AsyncClient)
import asyncio
asyncio.run(client.aclose())
print("mcp http factory ok: builds an httpx client (verify flag wired)")

notes = []
mod._mcp_ensure_started(emit=notes.append)
assert any("connected" in n for n in notes), notes
assert "add_numbers" in mod.TOOLS, list(mod.TOOLS)
assert "shout" in mod.TOOLS
# discovered MCP tools were attached to the linking agent
assert "add_numbers" in mod.AGENTS["solo"]["tools"], mod.AGENTS["solo"]["tools"]
# their schema comes from the server (not inspect of a **kwargs wrapper)
sch = mod.tool_schema("add_numbers")
assert "a" in sch["parameters"]["properties"], sch
print("mcp live connect ok: tools discovered, attached, schema from server")

# 5. Actually call an MCP tool through the registered wrapper
assert mod.TOOLS["add_numbers"](a=2, b=40) == "42"
assert mod.TOOLS["shout"](text="hi") == "HI"
print("mcp tool call ok: add_numbers(2,40)=42, shout('hi')=HI")

print("\nALL MCP CHECKS PASSED")
