"""Verify the TradingAgents port (graphs/TradingAgents.mta): the multi-agent
topology — coordinator -> FAN-OUT to 4 PARALLEL analysts -> join -> bull/bear debate
loop -> research manager -> trader -> risky/safe/neutral risk debate loop -> portfolio
manager — loads, generates, compiles, and runs end to end (stubbed LLM), with the
analysts' reports merged from the fan-out and shared state flowing through. All offline."""
import importlib.util
import os
import py_compile
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.chdir(BASE)

import graph_codegen
from graph_model import load_mta

MTA = os.path.join(BASE, "graphs", "TradingAgents.mta")
TOOLS = os.path.join(BASE, "tools")

# 1. Load the bundled graph (restores the tool file if missing) + analyze cleanly.
g, _info = load_mta(MTA, TOOLS)
agents = [n for n in g.nodes.values() if n.kind == "agent"]
whiles = [n for n in g.nodes.values() if n.kind == "while"]
fanouts = [n for n in g.nodes.values() if n.kind == "fanout"]
joins = [n for n in g.nodes.values() if n.kind == "join"]
assert len(agents) == 13, ("expected 13 agents (coordinator + 12)", len(agents))
assert len(whiles) == 2, ("expected 2 debate loops (while nodes)", len(whiles))
assert len(fanouts) == 1 and len(joins) == 1, ("expected 1 fanout + 1 join",
                                               len(fanouts), len(joins))
a = graph_codegen.analyze(g)
assert not a["errors"], a["errors"]
assert a["mode"] == "graph", a["mode"]
assert not a.get("warnings"), a["warnings"]      # disjoint report fields -> clean
print("trading graph ok: 13 agents, analyst fan-out+join, 2 debate loops, graph mode, no warnings")

# 2. Generate + compile (agent.py + the custom dashboard gui.py).
out = graph_codegen.generate_from_graph(g, "verify_trading_agents", gui=None)
py_compile.compile(os.path.join(out, "agent.py"), doraise=True)
py_compile.compile(os.path.join(out, "gui.py"), doraise=True)
with open(os.path.join(out, "requirements.txt"), encoding="utf-8") as f:
    reqs = f.read()
for pkg in ("yfinance", "stockstats", "pandas"):
    assert pkg in reqs, (pkg, reqs)
print("trading codegen ok: agent.py + custom gui.py compile; data deps in requirements")

# 3. Run the whole pipeline with a stubbed LLM: correct order + state flow.
spec = importlib.util.spec_from_file_location("verify_ta_agent",
                                              os.path.join(out, "agent.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

ORDER, SEEN = [], {}

def stub(agent_name, cfg, system, messages):
    ORDER.append(agent_name)
    SEEN[agent_name] = system + "\n" + "\n".join(
        m.get("content", "") for m in messages if isinstance(m.get("content"), str))
    writes = (mod.AGENTS.get(agent_name) or {}).get("writes") or []
    block = ""
    if writes:
        body = "\n".join(f'{w} = "{agent_name} wrote {w}"' for w in writes)
        block = f"\n\n```state\n{body}\n```"
    return (f"[{agent_name}] done." + block, [])

mod._call_one = stub
mod.clear_history()
result = mod.run("Analyze NVDA as of 2026-07-01.", emit=lambda s: None)

analysts = {"market_analyst", "social_analyst", "news_analyst", "fundamentals_analyst"}
downstream = ["bull_researcher", "bear_researcher", "research_manager", "trader",
              "risky_analyst", "safe_analyst", "neutral_analyst", "portfolio_manager"]
assert ORDER[0] == "coordinator", ("coordinator must be the entry", ORDER)
assert set(ORDER[1:5]) == analysts, ("the 4 analysts run in parallel (any order)", ORDER)
assert ORDER[5:] == downstream, ("debate/decision sequence after the join", ORDER)
# the fan-out merged EVERY analyst's report into shared state (research manager reads them)
for rep in ("market_analyst wrote market_report", "social_analyst wrote sentiment_report",
            "news_analyst wrote news_report", "fundamentals_analyst wrote fundamentals_report"):
    assert rep in SEEN["research_manager"], ("fan-out lost an analyst report: " + rep)
# debate wiring: each judge/decider saw its upstream state
assert "bull_researcher wrote bull_case" in SEEN["bear_researcher"]
assert ("bull_researcher wrote bull_case" in SEEN["research_manager"]
        and "bear_researcher wrote bear_case" in SEEN["research_manager"])
for v in ("risky_analyst wrote risky_view", "safe_analyst wrote safe_view",
          "neutral_analyst wrote neutral_view", "trader wrote trader_plan"):
    assert v in SEEN["portfolio_manager"], ("portfolio_manager missing " + v)
assert result.startswith("[portfolio_manager]"), result
print("trading run ok: coordinator -> 4 PARALLEL analysts (reports merged) -> both "
      "debates + state flow -> final decision")

# 4. EVERY tool in EVERY agent's spec must resolve — this is the exact path that
#    broke live ('_load', a phantom tool from a top-level `def` in a tool file, was
#    listed in portfolio_manager.tools but never @tool-registered -> KeyError in
#    tool_schema). A stubbed _call_one skips schema assembly, so guard it directly.
BUILTIN_TOOLS = {"route_to", "spawn_subagent", "write_todos", "set_state",
                 "run_python", "web_search", "read_offload"}
mcp_names = set(getattr(mod, "_MCP", {}).get("schemas", {}))
for aname, aspec in mod.AGENTS.items():
    for t in aspec.get("tools", []):
        assert t in mod.TOOLS or t in BUILTIN_TOOLS or t in mcp_names, \
            f"agent '{aname}' lists unregistered tool '{t}' (phantom def-as-tool?)"
        if t not in BUILTIN_TOOLS and t not in mcp_names:
            mod.tool_schema(t)          # the exact call that raised KeyError('_load')
# the tool files must expose ONLY their @tool functions (no phantom top-level defs)
assert set(graph_codegen._tool_names("trading_memory_tools.py")) == {
    "record_decision", "get_past_decisions", "get_realized_return"}, \
    graph_codegen._tool_names("trading_memory_tools.py")
assert set(graph_codegen._tool_names("trading_agents_tools.py")) == {
    "get_price_history", "get_stock_quote", "get_technical_indicators",
    "get_fundamentals", "get_company_news", "get_macro_news", "get_social_sentiment"}, \
    graph_codegen._tool_names("trading_agents_tools.py")
print("trading tools ok: every agent tool resolves; no phantom (def-as-tool) tools")

print("\nALL TRADINGAGENTS CHECKS PASSED")
