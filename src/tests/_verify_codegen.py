"""Offline verification of codegen + the generated single agent (no API key).

Single agents are generated as a 1-node pipeline (#2 unification), so this
exercises the unified runtime API: AGENTS/PIPELINE, build_system(agent_name),
per-agent budgets, _call_one, tool_schema, trace, and history.
"""

import importlib.util
import os
import py_compile
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.chdir(BASE)

# 1. Syntax-check every module
for fname in ["app_config.py", "llm_client.py", "coding_agent.py",
              "codegen.py", "tool_registry.py", "main.py",
              os.path.join("canvas_qt", "welcome.py"),
              os.path.join("canvas_qt", "tool_generator.py")]:
    py_compile.compile(os.path.join(BASE, fname), doraise=True)
    print(f"compile ok: {fname}")

# 1b. Tool dependency detection (keeps requirements.txt honest)
import codegen

deps = codegen.tool_requirements(
    "import pandas as pd\nimport csv\nfrom PIL import Image\n"
    "from tool_registry import tool\nfrom langchain_core.tools import tool\n"
    "import os\n")
# neither the tool_registry nor the (legacy) langchain import is a pip dep
assert deps == ["Pillow", "pandas"], deps
assert codegen.tool_requirements("import csv\nimport io\n") == []
# regression: docstring/comment prose that merely begins with "from"/"import" must
# NOT leak as a requirement (a naive line regex produced bogus 'the'/'still' deps).
_prose = ('def f():\n'
          '    """Names resolve\n'
          '    from the module globals; guarded so a standalone\n'
          '    import still works."""\n'
          '    import reportlab\n')
assert codegen.tool_requirements(_prose) == ["reportlab"], \
    codegen.tool_requirements(_prose)
# nested (indented) real imports are still counted
assert codegen.tool_requirements(
    "def g():\n    from concurrent.futures import ThreadPoolExecutor\n"
    "    import numpy\n") == ["numpy"], "nested imports must count (concurrent is stdlib)"
# the inliner strips BOTH the current tool_registry import and legacy langchain
# imports (the generated runtime supplies its own `tool` stand-in)
stripped = codegen.TOOL_IMPORT_STRIP_RE.sub(
    "", "from tool_registry import tool\nimport tool_registry\n"
        "import langchain_core.tools\nfrom langchain.tools import tool\nx = 1")
assert "tool_registry" not in stripped and "langchain" not in stripped, stripped
assert "x = 1" in stripped
print("tool deps ok:", deps, "| tool_registry + langchain imports stripped")

# the shipped tool library is on the lightweight registry, not langchain
import tool_registry
for fn in os.listdir(os.path.join(BASE, "tools")):
    if fn.endswith(".py"):
        src = open(os.path.join(BASE, "tools", fn), encoding="utf-8").read()
        assert "langchain" not in src, f"{fn} still imports langchain"

@tool_registry.tool
def _probe(x: str) -> str:
    return x
assert tool_registry.TOOLS.get("_probe") is _probe   # decorator registers + returns
print("tool_registry ok: library is langchain-free; @tool registers by name")

# 2. Generate a ReAct agent with the seeded load_csv tool
NAME = "csv_helper"
settings = {
    "name": NAME,
    "provider": "siliconflow",
    "model": "deepseek-ai/DeepSeek-V4-Flash",
    "api_key": "sk-test-not-real",
    "base_url": "https://api.siliconflow.cn/v1",
    "pattern": "react",
    "gui": True,
    "system_prompt": "You are a data-analysis assistant for local CSV files.",
    "tools": ["load_csv.py"],
    "budgets": {
        "max_iterations": 10, "max_tool_calls": 20,
        "max_output_tokens": 8_000,
        "max_wall_clock_s": 60,
    },
}
out_dir = codegen.generate_agent(settings)
print(f"generated: {out_dir}")
for f in sorted(os.listdir(out_dir)):
    print("  -", f)

# 3. Syntax-check and import the generated agent (no API call yet)
agent_path = os.path.join(out_dir, "agent.py")
py_compile.compile(agent_path, doraise=True)
print("compile ok: generated agent.py")
gui_path = os.path.join(out_dir, "gui.py")
assert os.path.exists(gui_path), "gui.py was not generated"
py_compile.compile(gui_path, doraise=True)
print("compile ok: generated gui.py")

spec = importlib.util.spec_from_file_location("generated_agent", agent_path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
print("import ok; registered tools:", list(mod.TOOLS))

# 3b. Unified 1-node pipeline shape: exactly one agent, named after the app
assert mod.PIPELINE == [NAME], mod.PIPELINE
assert NAME in mod.AGENTS and mod.AGENTS[NAME]["tools"] == ["load_csv"]

# 4. Exercise the generated agent's pieces offline
print("---SYSTEM PROMPT---")
print(mod.build_system(NAME))
print("---TOOL CALL---")
print(mod.TOOLS["load_csv"](
    path=os.path.join("..", "react_agent", "sample_data.csv"), max_rows=2))
print("---TOOL SCHEMA (native function calling)---")
schema = mod.tool_schema("load_csv")
assert schema["name"] == "load_csv"
assert schema["parameters"]["properties"]["path"]["type"] == "string"
assert schema["parameters"]["properties"]["max_rows"]["type"] == "integer"
assert schema["parameters"]["required"] == ["path"]
assert "CSV" in schema["description"]
print("schema ok:", schema["parameters"])

# 5. Budget guard sanity: zero wall clock should trip immediately. Budgets are
#    per-agent in the unified runtime.
mod.clear_history()
mod.AGENTS[NAME]["budgets"]["max_wall_clock_s"] = -1
print("budget check:", mod.run("anything", emit=lambda s: None))

# 5b. Trace: structured JSONL with consistent trace_id
import glob as _glob
import json as _json
trace_files = sorted(_glob.glob(os.path.join(out_dir, "traces", "*.jsonl")))
assert trace_files, "no trace file written"
with open(trace_files[-1], encoding="utf-8") as f:
    records = [_json.loads(line) for line in f]
assert records[0]["kind"] == "run_start" and records[-1]["kind"] == "run_end"
assert len({r["trace_id"] for r in records}) == 1
print("trace ok:", [r["kind"] for r in records])

# 6. History: a completed exchange is recorded, context built, clearable.
#    Restore the budget and stub the model call so the turn finishes offline.
mod.clear_history()
mod.AGENTS[NAME]["budgets"]["max_wall_clock_s"] = 60
mod._call_one = lambda agent_name, cfg, system, messages: ("final answer", [])
assert "final answer" in str(mod.run("anything", emit=lambda s: None))
assert mod.HISTORY and mod.HISTORY[0]["content"] == "anything"
assert any(f.endswith(".json")
           for f in os.listdir(os.path.join(out_dir, "sessions")))   # session persisted
assert "Conversation so far" in mod.history_context()
mod.clear_history()
assert mod.HISTORY == [] and mod.history_context() == ""
print("history ok: stored, context built, cleared")

print("\nALL CHECKS PASSED")
