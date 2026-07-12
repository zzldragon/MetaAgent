"""Verify L1: Adaptive-RAG-style adaptive retrieval (opt-in prompt affordance).

An agent with `adaptive_retrieval` AND a retrieval tool (a linked RAG KB and/or
web_search) gets an "## Adaptive retrieval" prompt tail (decide whether to search
at all, then route to the best source). With the flag on but NO retrieval tool it
has no effect (no tail) and analyze() warns. Off -> no tail.
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
_docs = tempfile.mkdtemp(prefix="ma_docs_")
TAIL = "## Adaptive retrieval"


def _gen(build, name):
    g = Graph()
    a = g.new_node("agent", 0, 0); a.name = "A"
    llm = g.new_node("llm", 0, 0); llm.props.update(LLM); g.add_edge(llm.id, a.id)
    build(g, a)
    info = graph_codegen.analyze(g)
    assert not info["errors"], info["errors"]
    out = os.path.join(GENERATED_DIR, name)
    if os.path.exists(out):
        shutil.rmtree(out)
    out = graph_codegen.generate_from_graph(g, name, gui=False)
    src = open(os.path.join(out, "system_prompts.json"), encoding="utf-8").read()
    return info, src


def _with_rag(g, a):
    r = g.new_node("rag", 0, 0); r.props["docs_dir"] = _docs
    r.props["description"] = "the docs"; g.add_edge(r.id, a.id)


try:
    # A. RAG + adaptive on -> tail present.
    _info, src = _gen(lambda g, a: (_with_rag(g, a), a.props.update(adaptive_retrieval=True)),
                      "verify_adaptive_rag")
    assert TAIL in src, "adaptive tail missing for RAG agent"
    assert not _info["warnings"], _info["warnings"]
    print("ok 1: RAG + adaptive_retrieval -> adaptive-retrieval prompt tail")

    # B. web_search + adaptive on -> tail present.
    _info, src = _gen(lambda g, a: a.props.update(web_search=True, adaptive_retrieval=True),
                      "verify_adaptive_web")
    assert TAIL in src, "adaptive tail missing for web_search agent"
    print("ok 2: web_search + adaptive_retrieval -> tail present")

    # C. adaptive on but NO retrieval tool -> no tail + analyze warning.
    _info, src = _gen(lambda g, a: a.props.update(adaptive_retrieval=True),
                      "verify_adaptive_none")
    assert TAIL not in src, "adaptive tail should NOT appear without a retrieval tool"
    assert any("nothing to route" in w for w in _info["warnings"]), _info["warnings"]
    print("ok 3: adaptive_retrieval without a retrieval tool -> no tail + warning")

    # D. RAG but adaptive OFF -> no tail (default behaviour unchanged).
    _info, src = _gen(lambda g, a: _with_rag(g, a), "verify_adaptive_off")
    assert TAIL not in src, "adaptive tail must be opt-in"
    print("ok 4: retrieval tool but adaptive_retrieval off -> no tail (opt-in)")
finally:
    shutil.rmtree(_docs, ignore_errors=True)

print("ALL ADAPTIVE-RETRIEVAL CHECKS PASSED")
