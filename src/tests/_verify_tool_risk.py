"""Verify explicit per-tool HITL risk (roadmap item 6): a tool's own
@tool(risk="high"|"safe") declaration is AUTHORITATIVE over the name-substring
heuristic — fixing both failure modes of the old guess (a read-only
"update_dashboard" no longer needlessly prompts; a destructive innocuously-named
"refresh_cache" now does). Codegen AST-parses the decorator into config's
high_risk_tools / safe_tools; the runtime is_high_risk honors them first."""

import importlib.util
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.chdir(BASE)

import app_config
import coding_agent
import graph_codegen
from graph_model import Graph

LLM = {"provider": "siliconflow", "model": "deepseek-ai/DeepSeek-V4-Flash",
       "api_key": "sk-test", "base_url": "https://api.siliconflow.cn/v1"}

TOOL_FILE = "risk_test_tools.py"
TOOL_SRC = (
    "from tool_registry import tool\n\n\n"
    '@tool(risk="high")\n'
    "def refresh_cache(key: str) -> str:\n"
    '    """Refresh the cache (secretly wipes data) — innocuous name, destructive."""\n'
    '    return "ok"\n\n\n'
    '@tool(risk="safe")\n'
    "def update_dashboard(view: str) -> str:\n"
    '    """Read-only: render a dashboard view (matches the \'update\' marker)."""\n'
    '    return "ok"\n\n\n'
    "@tool\n"
    "def delete_file(path: str) -> str:\n"
    '    """Delete a file (no risk= → falls back to the name heuristic)."""\n'
    '    return "ok"\n')

tool_path = os.path.join(app_config.TOOLS_DIR, TOOL_FILE)
with open(tool_path, "w", encoding="utf-8") as f:
    f.write(TOOL_SRC)
try:
    # 1. _tool_risk AST extraction
    high, safe = graph_codegen._tool_risk([TOOL_FILE])
    assert high == ["refresh_cache"], high
    assert safe == ["update_dashboard"], safe
    print("ok 1: _tool_risk reads @tool(risk=...) per function (high/safe)")

    # 2. Codegen emits the risk lists into config.json
    g = Graph()
    a = g.new_node("agent", 0, 0); a.name = "agent"
    llm = g.new_node("llm", 0, 0); llm.props.update(LLM)
    g.add_edge(llm.id, a.id)
    t = g.new_node("tool", 0, 0); t.props["files"] = [TOOL_FILE]
    g.add_edge(t.id, a.id)
    out = graph_codegen.generate_from_graph(g, "demo_tool_risk", gui=False)
    cfg = json.load(open(os.path.join(out, "config.json"), encoding="utf-8"))
    assert cfg["high_risk_tools"] == ["refresh_cache"], cfg["high_risk_tools"]
    assert cfg["safe_tools"] == ["update_dashboard"], cfg["safe_tools"]
    print("ok 2: generated config.json carries high_risk_tools + safe_tools")

    # 3. Runtime is_high_risk honors the explicit lists over the heuristic
    spec = importlib.util.spec_from_file_location(
        "demo_tool_risk_agent", os.path.join(out, "agent.py"))
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    assert mod.is_high_risk("refresh_cache") is True, "explicit high must confirm"
    assert mod.is_high_risk("update_dashboard") is False, "explicit safe must not"
    assert mod.is_high_risk("delete_file") is True, "no risk= → name heuristic"
    assert mod.is_high_risk("read_report") is False, "no risk=, no marker → safe"
    print("ok 3: is_high_risk — explicit high/safe authoritative; heuristic fallback")

    # 3b. tie-break: a tool in BOTH lists must resolve to high (fail-safe). Pins
    #     the order of the two if-blocks in is_high_risk.
    mod.CONFIG["high_risk_tools"] = list(mod.CONFIG.get("high_risk_tools", [])) + ["update_dashboard"]
    assert mod.is_high_risk("update_dashboard") is True, "high must win when a tool is in both lists"
    mod.CONFIG["high_risk_tools"] = [t for t in mod.CONFIG["high_risk_tools"] if t != "update_dashboard"]
    print("ok 3b: high wins over safe when a tool is in both lists (fail-safe)")

    # 4. is_parallel_safe follows is_high_risk: a safe-flagged 'update_*' is
    #    parallel-safe; a high-flagged tool is serial
    assert mod.is_parallel_safe("update_dashboard") is True
    assert mod.is_parallel_safe("refresh_cache") is False
    print("ok 4: parallel-safety follows the explicit risk classification")
finally:
    if os.path.exists(tool_path):
        os.remove(tool_path)

# 5. The coding agent instructs the model to declare risk on the decorator
assert 'risk="high"' in coding_agent.CODING_SYSTEM
assert 'risk="safe"' in coding_agent.CODING_SYSTEM
print("ok 5: coding agent prompts the model to declare @tool(risk=...)")

# 6. Backward compatibility: an empty/old config (no lists) still uses the
#    heuristic with no crash
import importlib.util as _il
spec2 = _il.spec_from_file_location("demo_tool_risk_agent2",
                                    os.path.join(out, "agent.py"))
mod2 = _il.module_from_spec(spec2); spec2.loader.exec_module(mod2)
mod2.CONFIG.pop("high_risk_tools", None)
mod2.CONFIG.pop("safe_tools", None)
assert mod2.is_high_risk("delete_file") is True       # heuristic intact
assert mod2.is_high_risk("read_file") is False
print("ok 6: missing risk lists → heuristic still applies (backward compatible)")

print("\nTOOL-RISK CHECKS PASSED")
