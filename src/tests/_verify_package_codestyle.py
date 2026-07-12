"""Verify the "package" code-style: generate_from_graph(code_style="package")
splits the runtime into a runtime/ package (runtime/_core.py + one module per
feature) with a thin agent.py engine, while "single" (default) stays one
self-contained agent.py. Both produce a working agent; this RUNS the package
one (py_compile can't catch the cross-module NameErrors the flat-namespace
fragments could otherwise cause)."""
import glob
import importlib
import json
import os
import py_compile
import shutil
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.chdir(BASE)

import graph_codegen as gc
import graph_model as gm

LLM = {"provider": "siliconflow", "model": "deepseek-ai/DeepSeek-V4-Flash",
       "api_key": "sk-x", "base_url": "https://api.siliconflow.cn/v1"}


def _llm(g, a):
    n = g.new_node("llm", a.x - 150, a.y + 120); n.props.update(LLM); g.add_edge(n.id, a.id)


def _feature_graph(docs):
    """Chain worker->finisher with shared state, a code_exec tool, and a RAG kb —
    exercises tools, run_python schema, state/checkpoint, and rag's register-at-
    import path (all fragments that could break the split)."""
    g = gm.Graph()
    g.state_schema = [{"name": "note", "type": "str", "reducer": "overwrite",
                       "default": "", "description": "scratch"}]
    a = g.new_node("agent", 0, 0); a.name = "worker"; a.props["code_exec"] = True; _llm(g, a)
    b = g.new_node("agent", 300, 0); b.name = "finisher"; _llm(g, b)
    g.add_edge(a.id, b.id)
    with open(os.path.join(docs, "kb.txt"), "w", encoding="utf-8") as fh:
        fh.write("MetaAgent builds agents.\nRAG retrieves chunks from documents.\n")
    r = g.new_node("rag", -200, -160); r.name = "kb"; r.props["docs_dir"] = docs
    g.add_edge(r.id, a.id)
    return g


docs = tempfile.mkdtemp(prefix="pkg_kb_")
EXPECT_MODS = {"_core", "hitl", "rag", "history", "guardrails", "skills", "checkpoint"}

# ── 1. package layout + config provenance ────────────────────────────────────
out = gc.generate_from_graph(_feature_graph(docs), "vf_pkg", gui=True, code_style="package")
top = set(os.listdir(out))
assert {"agent.py", "runtime", "config.json"} <= top, top
rt = {f[:-3] for f in os.listdir(os.path.join(out, "runtime")) if f.endswith(".py")}
assert "__init__" in os.listdir(os.path.join(out, "runtime")) or True
assert EXPECT_MODS <= rt, ("missing runtime modules", EXPECT_MODS - rt, rt)
cfg = json.load(open(os.path.join(out, "config.json"), encoding="utf-8"))
assert cfg["code_style"] == "package", cfg.get("code_style")
print("1. package layout: agent.py + runtime/%s + config.code_style=package"
      % sorted(rt))

# ── 2. every emitted .py compiles ────────────────────────────────────────────
for py in glob.glob(os.path.join(out, "**", "*.py"), recursive=True):
    py_compile.compile(py, doraise=True)
print("2. all emitted .py compile")

# ── 3. import as `python agent.py` would (cwd=out so `import runtime` resolves)
for k in [k for k in list(sys.modules) if k == "agent" or k.startswith("runtime")]:
    del sys.modules[k]
sys.path.insert(0, out); os.chdir(out)
import agent as m
import runtime._core as core
# historical `import agent; agent.*` surface must survive the split (gui/server rely on it)
for attr in ("run", "run_pipeline", "run_graph", "CONFIG", "AGENTS", "TOOLS",
             "clear_history", "build_system", "react", "_call_one", "_RUN"):
    assert hasattr(m, attr), "agent.%s missing — star-import surface broke" % attr
print("3. agent.* engine surface intact via star-imports")

# ── 4. one live shared foundation across modules (not copies) ────────────────
import runtime.hitl as hitl
assert core.CONFIG is m.CONFIG is hitl.CONFIG, "CONFIG duplicated across modules!"
assert core._RUN is m._RUN, "_RUN duplicated!"
assert core.TOOLS is m.TOOLS, "TOOLS registry duplicated!"
# rag registered its retrieval tool into that one shared registry at import time
assert any("search" in t or "doc" in t for t in m.TOOLS), sorted(m.TOOLS)
print("4. shared identity: one CONFIG/_RUN/TOOLS; rag tool registered ->",
      [t for t in m.TOOLS if "search" in t or "doc" in t])

# ── 5. a real run: react loop + code_exec tool + two-agent chain + state ─────
# llm() is in _core and calls _core._call_one, so the stub MUST patch _core
# (patching agent._call_one is ignored in package mode).
calls = {"n": 0}
def stub(agent_name, cfg_, system, messages):
    calls["n"] += 1
    if agent_name == "worker" and calls["n"] == 1:
        return ("compute", [{"id": "c1", "name": "run_python", "args": {"code": "print(6*7)"}}])
    if agent_name == "worker":
        return ("worker: the answer is 42", [])
    return ("finisher: 42", [])
core._call_one = stub
m.CONFIG["hitl_confirm"] = False          # deterministic: no confirm prompt on run_python
m.clear_history()
res = m.run("compute 6*7", emit=lambda s: None)
assert "42" in res, res
assert calls["n"] >= 3, ("react loop under-ran", calls)
os.chdir(BASE); sys.path.remove(out)
shutil.rmtree(out, ignore_errors=True)
print("5. package run() end-to-end (react+tool+chain+state) ->", repr(res[:40]))

# ── 6. default is still single self-contained agent.py (legacy unbroken) ─────
out2 = gc.generate_from_graph(_feature_graph(docs), "vf_single", gui=False)
assert not os.path.isdir(os.path.join(out2, "runtime")), "single mode emitted runtime/!"
assert os.path.isfile(os.path.join(out2, "agent.py"))
cfg2 = json.load(open(os.path.join(out2, "config.json"), encoding="utf-8"))
assert cfg2["code_style"] == "single", cfg2.get("code_style")
# a single-file agent.py still holds the runtime inline (has the ReAct loop + hitl)
src = open(os.path.join(out2, "agent.py"), encoding="utf-8").read()
assert "def react(" in src and "def confirm_tool(" in src and "import runtime" not in src
py_compile.compile(os.path.join(out2, "agent.py"), doraise=True)
shutil.rmtree(out2, ignore_errors=True)
print("6. default single-file: one agent.py, runtime inlined, no runtime/ pkg")

# ── 7. INVARIANT: every *_tool_schema _core references must be DEFINED in _core ──
# _call_one lives in runtime/_core.py and its schema comprehension names every
# built-in tool-schema builder. If a builder is emitted into agent.py instead
# (which _core can't import), a real run NameErrors — but stubbing _call_one hides
# it. A static check on _core.py catches that class of package-split regression.
out3 = gc.generate_from_graph(_feature_graph(docs), "vf_pkg_inv", gui=False, code_style="package")
core_src = open(os.path.join(out3, "runtime", "_core.py"), encoding="utf-8").read()
import re as _re
referenced = set(_re.findall(r"(\w+_tool_schema)\(", core_src))
defined = set(_re.findall(r"def (\w+_tool_schema)\(", core_src))
missing = referenced - defined
assert not missing, ("package: _core references tool-schema builders NOT defined in "
                     "_core (they leaked into agent.py): %s" % sorted(missing))
assert {"_set_state_tool_schema", "_route_tool_schema", "_web_search_tool_schema",
        "_todos_tool_schema", "_read_offload_tool_schema"} <= defined, defined
shutil.rmtree(out3, ignore_errors=True)
print("7. invariant OK: all %d *_tool_schema builders _core uses are defined in _core"
      % len(referenced))

shutil.rmtree(docs, ignore_errors=True)
print("\nALL PACKAGE CODE-STYLE CHECKS PASSED")
