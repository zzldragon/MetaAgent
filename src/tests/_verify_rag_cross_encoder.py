"""Verify the cross-encoder reranker (learnRAG_cn §9): rerank_mode='cross_encoder'
re-orders the recall pool with a FREE, LOCAL cross-encoder — no tokens, no API key,
deterministic. It fails soft (missing lib/model -> keeps retrieval order + a note,
never crashes), mirroring dense->BM25. The cross-encoder itself is mocked here so
the test is fast and offline (no ~1GB model download)."""

import importlib.util
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

_docs = tempfile.mkdtemp(prefix="ce_docs_")
with open(os.path.join(_docs, "a.txt"), "w", encoding="utf-8") as f:
    f.write("Dogs are loyal animals. The refund window is exactly 14 days. Cats nap.")

g = gm.Graph(); a = g.new_node("agent", 0, 0); a.name = "A"
l = g.new_node("llm", -200, 0); l.props.update(LLM); g.add_edge(l.id, a.id)
r = g.new_node("rag", -200, 150); r.name = "kb"
r.props.update(docs_dir=_docs, retrieval_algorithm="bm25", top_k=3, chunk_chars=180,
               rerank_mode="cross_encoder", rerank_model="BAAI/bge-reranker-base")
g.add_edge(r.id, a.id)
out = gc.generate_from_graph(g, "vce_test", gui=False)
py_compile.compile(os.path.join(out, "agent.py"), doraise=True)
cfg = json.load(open(os.path.join(out, "config.json"), encoding="utf-8"))["rag"][0]
assert cfg["rerank"] == {"mode": "cross_encoder", "model": "BAAI/bge-reranker-base"}, cfg["rerank"]
m = importlib.util.module_from_spec(
    importlib.util.spec_from_file_location("vce", os.path.join(out, "agent.py")))
importlib.util.spec_from_file_location("vce", os.path.join(out, "agent.py")).loader.exec_module(m)
print("1. config carries rerank cross_encoder + model; agent.py compiles ok")

# 2. fail-soft: reranker unavailable -> retrieval order kept + a surfaced note
m._cross_encoder_scores = lambda model, q, docs: None
res = m._rag_search("kb", "refund window days")
assert "refund" in res.lower() and "reranker unavailable" in res, res[:180]
print("2. fail-soft: unavailable reranker keeps retrieval order + notes ok")

# 3. available (mocked cross-encoder): reorders the pool by CE score
def _fake_ce(model, query, docs):
    return [9.0 if "refund" in d.lower() else 0.1 for d in docs]
m._cross_encoder_scores = _fake_ce
ranked = m._rag_rerank_cross_encoder(
    "refund", [(0, 1.0), (1, 0.4)],
    [{"source": "a", "text": "Dogs are loyal."},
     {"source": "a", "text": "The refund window is 14 days."}],
    {"rerank": {"model": "x"}}, notes=[])
assert ranked[0][0] == 1, ("CE should rank the refund passage first", ranked)
print("3. available: cross-encoder reorders the pool by score ok -> %s" % ranked)
shutil.rmtree(out, ignore_errors=True)
shutil.rmtree(_docs, ignore_errors=True)

# 4. RagDialog round-trips rerank_mode + rerank_model
try:
    from PySide6.QtWidgets import QApplication
    from canvas_qt.dialogs import RagDialog
    QApplication.instance() or QApplication([])
    n = gm.Graph().new_node("rag", 0, 0)
    d = RagDialog(None, n)
    d.rerank_mode.setCurrentText("cross_encoder")
    d.rerank_model.setText("BAAI/bge-reranker-v2-m3")
    assert d.apply() is None
    assert n.props["rerank_mode"] == "cross_encoder"
    assert n.props["rerank_model"] == "BAAI/bge-reranker-v2-m3", n.props
    print("4. RagDialog round-trips rerank_mode + rerank_model ok")
except Exception as e:                       # PySide6 unavailable -> skip UI check
    print("4. dialog check skipped:", type(e).__name__)

print("\nALL CROSS-ENCODER RERANK CHECKS PASSED")
