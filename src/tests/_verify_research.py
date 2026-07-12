"""Verify M3: context-quarantined retrieval via a research sub-agent.

A SPAWNABLE sub-agent equipped with retrieval tools (a linked RAG KB and/or
web_search) gets a "research contract" prompt tail and runs in an ISOLATED react
loop (spawn_subagent). The key property: the sub-agent's RAW search results stay
in ITS context — the orchestrator receives only the sub-agent's final summary.
"""

import importlib.util
import os
import shutil
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.chdir(BASE)

import graph_codegen  # noqa: E402
from app_config import GENERATED_DIR  # noqa: E402
from graph_model import Graph  # noqa: E402

LLM = {"provider": "siliconflow", "model": "deepseek-ai/DeepSeek-V4-Flash",
       "api_key": "sk-test", "base_url": "https://api.siliconflow.cn/v1"}

_docs = tempfile.mkdtemp(prefix="ma_docs_")     # empty docs dir (KB uses a manual chunk)
g = Graph()
coord = g.new_node("agent", 0, 0); coord.name = "Coordinator"; coord.props["role"] = "orchestrator"
lc = g.new_node("llm", 0, 0); lc.props.update(LLM); lc.props["parallel_tools"] = True
g.add_edge(lc.id, coord.id)
res = g.new_node("agent", 0, 0); res.name = "Researcher"
lr = g.new_node("llm", 0, 0); lr.props.update(LLM); g.add_edge(lr.id, res.id)
rag = g.new_node("rag", 0, 0); rag.props["docs_dir"] = _docs
rag.props["description"] = "the project knowledge base"; g.add_edge(rag.id, res.id)
g.add_edge(coord.id, res.id)                     # Coordinator -> Researcher (spawnable)

info = graph_codegen.analyze(g)
assert not info["errors"], info["errors"]
assert info["mode"] == "autonomous", info["mode"]

out = os.path.join(GENERATED_DIR, "verify_research")
if os.path.exists(out):
    shutil.rmtree(out)
out = graph_codegen.generate_from_graph(g, "verify_research", gui=False)
mod = importlib.util.module_from_spec(
    importlib.util.spec_from_file_location("vr_agent", os.path.join(out, "agent.py")))
sys.path.insert(0, out); os.chdir(out)
mod.__loader__.exec_module(mod)
os.chdir(BASE)

# 1. structure: researcher is spawnable, owns the search tool + research contract;
#    the orchestrator only spawns (it does NOT own the search tool).
assert mod.SPAWNABLE == ["Researcher"], mod.SPAWNABLE
assert "search_docs" in mod.AGENTS["Researcher"]["tools"], mod.AGENTS["Researcher"]["tools"]
assert "## Research contract" in mod.AGENTS["Researcher"]["system"], "research tail missing"
assert "spawn_subagent" in mod.AGENTS["Coordinator"]["tools"]
assert "search_docs" not in mod.AGENTS["Coordinator"]["tools"], "orchestrator must not search itself"
print("ok 1: researcher = spawnable + owns search + research contract; orchestrator only spawns")

# 2. quarantine: the researcher's RAW chunk stays in ITS context; the orchestrator
#    only ever sees the researcher's SUMMARY.
MARK = "QUARANTINEDMARKER42"
mod.rag_add_chunk(f"The secret is {MARK} and it lives only in the knowledge base.",
                  source="kb")
mod.rag_invalidate()
seen = {"Coordinator": [], "Researcher": []}


def _stub(agent_name, cfg, system, messages):
    seen.setdefault(agent_name, []).append(
        " || ".join(str(m.get("content") or "") for m in messages))
    has_tool = any(m.get("role") == "tool" for m in messages)
    if agent_name == "Coordinator":
        if not has_tool:
            return "", [{"id": "s1", "name": "spawn_subagent",
                         "args": {"name": "Researcher", "task": f"find {MARK}"}}]
        return "Final answer based on the researcher's summary.", []
    # Researcher: search once, then summarize (WITHOUT echoing the raw chunk).
    if not has_tool:
        return "", [{"id": "r1", "name": "search_docs", "args": {"query": MARK}}]
    return "SUMMARY: the requested item was found in the knowledge base [kb].", []


mod._call_one = _stub
result = mod.run("look up the secret", emit=lambda s: None)

coord_ctx = " ".join(seen["Coordinator"])
res_ctx = " ".join(seen["Researcher"])
assert MARK in res_ctx, "the researcher should have retrieved the raw chunk into its own context"
assert MARK not in coord_ctx, "QUARANTINE BROKEN: raw chunk leaked into the orchestrator's context"
assert "SUMMARY:" in coord_ctx, "the orchestrator should receive the researcher's summary"
print("ok 2: context quarantine holds — raw chunk stays with the researcher, "
      "orchestrator gets only the summary")

shutil.rmtree(_docs, ignore_errors=True)
print("ALL RESEARCH-SUBAGENT CHECKS PASSED")
