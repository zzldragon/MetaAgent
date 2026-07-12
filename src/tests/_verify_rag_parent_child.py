"""Verify parent-child / small-to-big retrieval (learnRAG_cn §4.3): with
retrieval_granularity='parent_child' a KB indexes small CHILD chunks (precise
matching) but returns their bigger PARENT block (full context), de-duplicated;
'chunk' (default) is unchanged. Also that the config/codegen carry the new fields."""

import importlib.util
import json
import os
import py_compile
import shutil
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.chdir(BASE)

import graph_codegen as gc
import graph_model as gm

LLM = {"provider": "siliconflow", "model": "deepseek-ai/DeepSeek-V4-Flash",
       "api_key": "sk-x", "base_url": "https://api.siliconflow.cn/v1"}

_docs = tempfile.mkdtemp(prefix="pc_docs_")
_filler = "This handbook explains company policies in exhaustive detail. " * 18
_doc = (_filler[:900]
        + "\nMAGICWORD42: a refund is processed within exactly 14 days.\n"
        + _filler[:1100])
with open(os.path.join(_docs, "policy.txt"), "w", encoding="utf-8") as _f:
    _f.write(_doc)


def _build(granularity):
    g = gm.Graph(); a = g.new_node("agent", 0, 0); a.name = "A"
    l = g.new_node("llm", -200, 0); l.props.update(LLM); g.add_edge(l.id, a.id)
    r = g.new_node("rag", -200, 150); r.name = "knowledge"
    r.props.update(docs_dir=_docs, retrieval_algorithm="bm25", top_k=2,
                   chunk_chars=800, parent_chunk_chars=2400,
                   retrieval_granularity=granularity)
    g.add_edge(r.id, a.id)
    out = gc.generate_from_graph(g, "vpc_" + granularity, gui=False)
    py_compile.compile(os.path.join(out, "agent.py"), doraise=True)
    cfg = json.load(open(os.path.join(out, "config.json"), encoding="utf-8"))["rag"][0]
    assert cfg["retrieval_granularity"] == granularity and cfg["parent_chunk_chars"] == 2400, cfg
    sp = importlib.util.spec_from_file_location("vpc_" + granularity,
                                                os.path.join(out, "agent.py"))
    m = importlib.util.module_from_spec(sp); sp.loader.exec_module(m)
    return m, out


# 1. parent_child: a matched small child returns the big PARENT block, de-duplicated
m, out = _build("parent_child")
res = m._rag_search("knowledge", "MAGICWORD42 refund 14 days")
assert "MAGICWORD42" in res, res[:150]
assert res.count("MAGICWORD42") == 1, ("parent must be de-duplicated", res.count("MAGICWORD42"))
pc_len = len(res)
assert pc_len > 1200, ("expected the big parent block, got %d chars" % pc_len)
shutil.rmtree(out, ignore_errors=True)
print("1. parent_child returns the big parent (%d chars), deduped ok" % pc_len)

# 2. chunk (default) is unchanged: returns just the small matched chunk
m, out = _build("chunk")
res2 = m._rag_search("knowledge", "MAGICWORD42 refund 14 days")
assert "MAGICWORD42" in res2
ch_len = len(res2)
shutil.rmtree(out, ignore_errors=True)
print("2. chunk baseline returns a small chunk (%d chars) ok" % ch_len)

assert pc_len > ch_len, ("parent must be larger than the child", pc_len, ch_len)
print("3. small-to-big confirmed: parent %d > child %d" % (pc_len, ch_len))

# 4. the chunk builder tags children with their parent + a de-dup id
cfg = {"docs_dir": _docs, "chunk_chars": 800, "parent_chunk_chars": 2400,
       "retrieval_granularity": "parent_child", "retrieval_algorithm": "bm25"}
units = m._rag_parent_child(_doc, cfg)
assert units and all(len(u) == 3 for u in units), "expected (child, parent, idx) tuples"
kids, parents, _ = zip(*units)
assert all(len(k) <= len(p) for k, p in zip(kids, parents)), "child must be <= parent"
assert len(set(id(p) for p in parents)) < len(kids) or len(kids) == 1, \
    "multiple children should share a parent"
print("4. _rag_parent_child yields (child<=parent) units sharing parents ok")

shutil.rmtree(_docs, ignore_errors=True)
print("\nALL PARENT-CHILD RETRIEVAL CHECKS PASSED")
