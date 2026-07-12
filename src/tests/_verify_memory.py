"""Verify the MEMORY node: a persistent cross-run store linked to an agent that gives
it remember(content, tags) + recall(query, k) tools, backed by a JSON store + BM25
retrieval (reusing the RAG ranker). Proves: analyze rules, tool wiring, BM25 ranking,
CROSS-RUN persistence (a fresh module load sees prior memories), and package code-style.
Offline — no network, no LLM calls (the tools are pure Python)."""
import importlib.util
import os
import py_compile
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.chdir(BASE)

import graph_codegen
from graph_model import DEFAULT_BUDGETS, Graph

LLM = dict(provider="siliconflow", model="deepseek-ai/DeepSeek-V4-Flash",
           api_key="", base_url="https://api.siliconflow.cn/v1")


def _mem_graph():
    g = Graph()
    llm = g.new_node("llm", 300, 0); llm.name = "m"; llm.props.update(LLM)
    a = g.new_node("agent", 0, 0); a.name = "assistant"; a.props["role"] = "single"
    for k in DEFAULT_BUDGETS:
        a.props[k] = DEFAULT_BUDGETS[k]
    mem = g.new_node("memory", -200, 0); mem.name = "lessons"; mem.props["top_k"] = 3
    mem.props["description"] = "past support-ticket resolutions and user preferences"
    g.add_edge(llm.id, a.id); g.add_edge(mem.id, a.id)
    return g, a, mem


def _load(out_dir, name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(out_dir, "agent.py"))
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m


# ── 1. analyze: linked memory node is clean; the node forces no special mode ──
g, a, mem = _mem_graph()
info = graph_codegen.analyze(g)
assert not info["errors"], info["errors"]
print("memory analyze ok: linked memory node is clean")

# an UNLINKED memory node is an error (its tools would go nowhere)
g2 = Graph()
_l = g2.new_node("llm", 0, 0); _l.name = "m"; _l.props.update(LLM)
_a = g2.new_node("agent", 0, 0); _a.name = "solo"
for k in DEFAULT_BUDGETS:
    _a.props[k] = DEFAULT_BUDGETS[k]
g2.add_edge(_l.id, _a.id)
_m = g2.new_node("memory", -200, 0); _m.name = "orphan"   # not linked to any agent
assert any("not linked" in e for e in graph_codegen.analyze(g2)["errors"]), \
    graph_codegen.analyze(g2)["errors"]
print("memory analyze ok: an unlinked memory node is rejected")

# ── 2. codegen: remember/recall wired onto the agent + registered as tools ────
out = graph_codegen.generate_from_graph(g, "verify_memory", gui=False)
py_compile.compile(os.path.join(out, "agent.py"), doraise=True)
sp = os.path.join(out, "memory_store.json")
if os.path.exists(sp):
    os.remove(sp)                       # start from an empty store (deterministic)
mod = _load(out, "vmem1")
assert "remember" in mod.AGENTS["assistant"]["tools"], mod.AGENTS["assistant"]["tools"]
assert "recall" in mod.AGENTS["assistant"]["tools"]
assert "remember" in mod.TOOLS and "recall" in mod.TOOLS
# tool schema is generated from the docstring (has the content/query params)
_sch = mod.tool_schema("remember")
assert "content" in repr(_sch) and "recall" in repr(mod.tool_schema("recall"))
# the node's description (routing hint) is woven into BOTH tool docs
assert "support-ticket resolutions" in repr(mod.tool_schema("recall")), "description → recall doc"
assert "support-ticket resolutions" in repr(mod.tool_schema("remember")), "description → remember doc"
print("memory codegen ok: remember/recall on the agent + in TOOLS + schema built + description wired")

# ── 3. remember + recall: BM25 ranks the relevant memory first ────────────────
mod.remember("The user prefers metric units (kilometres, celsius).", tags="prefs")
mod.remember("Always double-check the currency before sending any money.", tags="finance")
mod.remember("Deploys must run the smoke test suite first.", tags="ops")
r_units = mod.recall("what measurement units does the user want?")
assert "metric" in r_units, r_units
r_money = mod.recall("payment currency")
assert "currency" in r_money.splitlines()[1], r_money   # top hit is the finance memory
# a query that matches nothing lexically still returns recent memories (never empty)
assert mod.recall("zzzz-nonsense-token").strip(), "recall falls back to recent, not empty"
print("memory run ok: remember stores, recall BM25-ranks the right memory first")

# ── 4. CROSS-RUN persistence: a FRESH module load sees the earlier memories ───
mod2 = _load(out, "vmem2")               # same out_dir -> same memory_store.json
assert "currency" in mod2.recall("money"), "memory persisted across a fresh load"
assert "smoke test" in mod2.recall("deploy"), "all prior memories survived"
mod2.remember("Prefer email over phone for this account.", tags="prefs")
assert "email" in _load(out, "vmem3").recall("contact method"), "3rd load sees the 2nd's write"
print("memory run ok: memories PERSIST across separate runs (Reflexion-style)")

# ── 5. package code style compiles + memory.py reuses the rag sibling ─────────
outp = graph_codegen.generate_from_graph(g, "verify_memory_pkg", gui=False,
                                         code_style="package")
for rel in ("agent.py", os.path.join("runtime", "memory.py"),
            os.path.join("runtime", "rag.py"), os.path.join("runtime", "_core.py")):
    py_compile.compile(os.path.join(outp, rel), doraise=True)
with open(os.path.join(outp, "runtime", "memory.py"), encoding="utf-8") as _f:
    _msrc = _f.read()
assert "def remember(" in _msrc and "def recall(" in _msrc, "memory fragment emitted"
assert "from .rag import *" in _msrc, "memory reuses the rag BM25 sibling in package mode"
print("memory package ok: agent + runtime/memory.py compile; reuses rag sibling")

print("\nALL MEMORY-NODE CHECKS PASSED")
