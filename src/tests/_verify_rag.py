"""Verify the RAG module: graph rules, generated BM25 retrieval, and the
agent loop calling search_docs — all offline."""

import importlib.util
import json
import os
import py_compile
import shutil
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.chdir(BASE)

import graph_codegen
from graph_model import Graph

# 0. Fixture docs folder
DOCS = os.path.join(BASE, "_rag_docs_test")
os.makedirs(DOCS, exist_ok=True)
with open(os.path.join(DOCS, "wx_notes.md"), "w", encoding="utf-8") as f:
    f.write("wxPython layout uses sizers. A BoxSizer arranges children "
            "horizontally or vertically; FlexGridSizer makes a grid. "
            "Always call SetSizer on the panel.\n")
with open(os.path.join(DOCS, "csv_notes.md"), "w", encoding="utf-8") as f:
    f.write("To load CSV files use the csv module. csv.reader parses rows; "
            "remember encoding utf-8-sig for Excel exports.\n")
with open(os.path.join(DOCS, "binary.bin"), "wb") as f:
    f.write(b"\x00\x01ignored")

LLM = {"provider": "siliconflow", "model": "deepseek-ai/DeepSeek-V4-Flash",
       "api_key": "sk-test", "base_url": "https://api.siliconflow.cn/v1"}

# 1. Graph rules — multiple RAG nodes per agent are allowed (Option A)
g = Graph()
solo = g.new_node("agent", 0, 0); solo.name = "solo"
llm = g.new_node("llm", 0, 0); llm.props.update(LLM)
assert g.add_edge(llm.id, solo.id) is None
rag = g.new_node("rag", 0, 0)
assert g.add_edge(rag.id, solo.id) is None
rag2 = g.new_node("rag", 0, 0)
assert g.add_edge(rag2.id, solo.id) is None, "multiple RAG nodes per agent now allowed"

errs = graph_codegen.analyze(g)["errors"]
assert any("docs folder" in e for e in errs), errs       # unconfigured rag(s)
rag.props["docs_dir"] = DOCS
rag2.props["docs_dir"] = DOCS
# two RAG nodes whose names slug to the same tool -> collision error
rag.name = "Docs"; rag2.name = "docs"
assert any("tool name" in e for e in graph_codegen.analyze(g)["errors"]), \
    "slug collision must be reported"
rag.name = "wx"; rag2.name = "csv"
# an orphan (configured but unlinked) RAG -> not-linked error
orphan = g.new_node("rag", 0, 0); orphan.props["docs_dir"] = DOCS
assert any("not linked" in e for e in graph_codegen.analyze(g)["errors"]), \
    "orphan rag must be reported"
g.remove_node(orphan.id)
assert not graph_codegen.analyze(g)["errors"], graph_codegen.analyze(g)["errors"]
# reduce to a single RAG node for the legacy single-tool path tested next
g.remove_node(rag2.id)
rag.name = "rag"
assert not graph_codegen.analyze(g)["errors"]
print("rag graph rules ok: multi-rag allowed, slug-collision + orphan caught")

# 2. Generate and inspect
out_dir = graph_codegen.generate_from_graph(g, "demo_rag", gui=True)
py_compile.compile(os.path.join(out_dir, "agent.py"), doraise=True)
with open(os.path.join(out_dir, "config.json"), encoding="utf-8") as f:
    cfg = json.load(f)
assert isinstance(cfg["rag"], list) and len(cfg["rag"]) == 1, cfg["rag"]
assert cfg["rag"][0]["docs_dir"] == DOCS and cfg["rag"][0]["top_k"] == 4
# default embedding is FREE + NO API KEY (local small model); retrieval stays
# bm25 by default so an out-of-the-box build needs no embedding lib/network.
_emb = cfg["rag"][0]["embedding"]
assert _emb["provider"] == "local", _emb
assert _emb["model"] == "BAAI/bge-small-zh-v1.5", _emb
assert cfg["rag"][0]["retrieval_algorithm"] == "bm25", cfg["rag"][0]
assert cfg["rag"][0]["grade_docs"] is False, cfg["rag"][0]   # opt-in, off by default
assert cfg["rag"][0]["corrective"] is False, cfg["rag"][0]   # opt-in, off by default
assert cfg["rag"][0]["corrective_max_rewrites"] == 2, cfg["rag"][0]

spec = importlib.util.spec_from_file_location(
    "demo_rag_agent", os.path.join(out_dir, "agent.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
assert "search_docs" in mod.TOOLS
assert "search_docs" in mod.AGENTS["solo"]["tools"]
schema = mod.tool_schema("search_docs")
assert schema["parameters"]["properties"]["query"]["type"] == "string"
assert schema["parameters"]["required"] == ["query"]
print("rag codegen ok: tool registered + schema + config")

# 3. Retrieval quality: right doc ranked first per query
hit = mod.TOOLS["search_docs"]("how do sizers work in wxPython", 1)
assert "wx_notes.md" in hit and "BoxSizer" in hit, hit[:200]
hit2 = mod.TOOLS["search_docs"]("csv encoding excel", 1)
assert "csv_notes.md" in hit2, hit2[:200]
miss = mod.TOOLS["search_docs"]("quantum chromodynamics lagrangian")
assert miss.startswith("No relevant documents"), miss[:100]
print("bm25 retrieval ok: relevant chunk first, graceful miss")

# 4. Full loop: stubbed LLM asks search_docs, grounds its answer on the chunk
rounds = []
def _rag_flow(agent_name, cfg_, system, messages):
    if not rounds:
        rounds.append(1)
        return "", [{"id": "r1", "name": "search_docs",
                     "args": {"query": "wxPython sizers"}}]
    assert messages[-1]["role"] == "tool"
    assert "BoxSizer" in messages[-1]["content"], messages[-1]["content"][:200]
    return "Use a BoxSizer (per wx_notes.md).", []
mod._call_one = _rag_flow
mod.clear_history()
answer = mod.run("how do I lay out widgets?", emit=lambda s: None)
assert answer == "Use a BoxSizer (per wx_notes.md).", answer
mod.clear_history()
print("rag agent loop ok: retrieve -> grounded answer")

# 5. Empty knowledge base -> clear error string for the model
mod.RAG["docs_dir"] = os.path.join(BASE, "_no_such_docs_dir")
mod.rag_invalidate()
assert mod.TOOLS["search_docs"]("anything").startswith("[ERROR]")
print("empty-kb handling ok")

# 6. Enable/disable toggle (persisted) gates the tool
mod.set_rag_enabled(False)
assert mod.rag_enabled() is False
assert mod.TOOLS["search_docs"]("anything").startswith("[RAG disabled]")
mod.set_rag_enabled(True)
assert mod.rag_enabled() is True
print("rag enable/disable ok")

# 7. Chunk CRUD: add/update/delete/clear, searchable, index rebuilds
mod.RAG["docs_dir"] = DOCS  # back to the real folder
mod.rag_clear()
msg = mod.rag_add_chunk("The deployment runbook lives in ops/runbook.md.",
                        source="ops")
assert msg.startswith("added chunk #1"), msg
cid = mod.rag_manual_chunks()[0]["id"]
hit = mod.TOOLS["search_docs"]("where is the deployment runbook", 1)
assert "runbook" in hit and "ops" in hit, hit[:200]
assert mod.rag_update_chunk(cid, "Runbook moved to docs/ops_v2.md.") is True
assert "ops_v2" in mod.TOOLS["search_docs"]("runbook location", 1)
assert mod.rag_delete_chunk(cid) is True
assert mod.rag_manual_chunks() == []
assert mod.rag_delete_chunk(999) is False
print("rag chunk CRUD ok: add/search/update/delete")

# manual chunks merge with file-derived chunks
mod.rag_add_chunk("Special token: ZZQ-MAGIC-42.")
assert "ZZQ-MAGIC-42" in mod.TOOLS["search_docs"]("ZZQ-MAGIC", 1)
assert "wx_notes.md" in mod.TOOLS["search_docs"]("sizers", 1)  # files still there
mod.rag_clear()
print("rag merge ok: manual + file chunks coexist")

# 7b. Add File: ingest a file's text as chunks, searchable, removable by source
big = os.path.join(DOCS, "_ingest_me.md")
with open(big, "w", encoding="utf-8") as f:
    f.write("Project Falcon ships on 2026-07-01. " * 80
            + "\nThe release owner is Dana Lee.\n")
msg = mod.rag_add_file(big)
assert msg.startswith("added") and "chunk(s) from _ingest_me.md" in msg, msg
assert int(msg.split()[1]) >= 1
hit = mod.TOOLS["search_docs"]("who is the release owner", 2)
assert "Dana Lee" in hit, hit[:200]
# the docs listing shows the auto-indexed files
files = mod.rag_docs_files()
assert "wx_notes.md" in files, files
# remove all chunks that came from that file
removed = mod.rag_remove_source("_ingest_me.md")
assert removed >= 1 and mod.rag_manual_chunks() == []
print(f"rag add-file ok: {msg}; remove_source removed {removed}")

# unsupported type -> clear error (no crash)
bad = os.path.join(DOCS, "_x.zip")
open(bad, "wb").close()
assert mod.rag_add_file(bad).startswith("[ERROR] unsupported file type")
# .docx path: python-docx is installed in this env -> real extraction
import importlib.util as _u
if _u.find_spec("docx"):
    import docx
    dp = os.path.join(DOCS, "_note.docx")
    d = docx.Document()
    d.add_paragraph("Quarterly KPI: latency p95 under 200ms.")
    d.save(dp)
    assert mod.rag_add_file(dp).startswith("added"), "docx ingest failed"
    assert "200ms" in mod.TOOLS["search_docs"]("latency kpi", 1)
    mod.rag_clear()
    print("rag docx ingest ok")
else:
    print("rag docx ingest skipped (python-docx not installed)")
mod.rag_clear()

# 7c. CJK tokenization: BM25 must work on Chinese without disturbing ASCII.
# (plain \w+ fuses a contiguous CJK run into ONE token -> Chinese retrieval is
# near-dead; the bigram tokenizer fixes it while ASCII stays byte-identical.)
import re  # noqa: E402

assert mod._rag_tokenize("wxPython BoxSizer csv utf-8-sig") == \
    re.findall(r"\w+", "wxpython boxsizer csv utf-8-sig"), \
    "ASCII tokenization must stay byte-identical to the old \\w+ path"
mod.rag_clear()
mod.RAG["docs_dir"] = os.path.join(BASE, "_no_such_docs_dir")  # isolate to manual
mod.rag_invalidate()
mod.rag_add_chunk("提高缓存命中率的方法是开启二级缓存。", source="zh_cache")
mod.rag_add_chunk("今天的天气很好，适合出门散步。", source="zh_weather")
zh = mod.TOOLS["search_docs"]("缓存命中率", 1)
assert "zh_cache" in zh, zh[:200]
zh_miss = mod.TOOLS["search_docs"]("量子色动力学")
assert zh_miss.startswith("No relevant documents"), zh_miss[:100]
degenerate = mod.TOOLS["search_docs"]("，。　 ")  # punctuation/space only
assert degenerate.startswith("No relevant documents"), degenerate[:100]
mod.rag_clear()
mod.RAG["docs_dir"] = DOCS
mod.rag_invalidate()
print("rag cjk tokenization ok: chinese retrieval works, ascii unchanged, "
      "graceful misses")

# 8. GUI exposes the RAG menu with the toggle + chunk management
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtGui import QAction  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

sys.path.insert(0, out_dir)
import gui  # noqa: E402  (generated gui.py)
app = QApplication.instance() or QApplication([])
frame = gui.ChatFrame()
# strip & accelerator mnemonics that QAction.text() keeps
titles = [a.text().replace("&", "") for a in frame.menuBar().actions()]
assert any("RAG" in t for t in titles), ("RAG menu missing", titles)
# introspect items via QAction texts (avoids the QAction.menu() ownership getter)
labels = [a.text().replace("&", "") for a in frame.findChildren(QAction)]
assert any("Enable RAG" in l for l in labels), labels
assert any("Add File" in l for l in labels), labels
assert any("Add Chunk" in l for l in labels), labels
assert any("Manage Knowledge Base" in l for l in labels), labels
assert any("Clear Knowledge Base" in l for l in labels), labels

# drive RagChunksDialog through the same agent module gui.py uses
import agent as _gcore  # noqa: E402  (sys.modules['agent'] set by `import gui`)
_gcore.rag_add_chunk("a manual chunk about pandas dataframes")
rdlg = gui.RagChunksDialog(frame)
n = rdlg.listbox.count()
assert n >= 1, "manual chunk should appear in RagChunksDialog"
rdlg.listbox.setCurrentRow(0)
rdlg.on_delete()
assert rdlg.listbox.count() == n - 1, "on_delete should drop one chunk"
_gcore.rag_clear()
frame.close()
print("rag gui menu + RagChunksDialog ok")

# 9. Multi-KB (Option A): two RAG nodes on one agent -> one tool each, isolated
# indexes, per-node descriptions as tool hints + a "Knowledge bases" prompt tail.
DOCS_A = os.path.join(BASE, "_rag_kb_a")
DOCS_B = os.path.join(BASE, "_rag_kb_b")
os.makedirs(DOCS_A, exist_ok=True)
os.makedirs(DOCS_B, exist_ok=True)
with open(os.path.join(DOCS_A, "sizers.md"), "w", encoding="utf-8") as f:
    f.write("wxPython layout uses sizers; a BoxSizer arranges children.\n")
with open(os.path.join(DOCS_B, "billing.md"), "w", encoding="utf-8") as f:
    f.write("Invoices are generated monthly; refunds take five business days.\n")

g2 = Graph()
a2 = g2.new_node("agent", 0, 0); a2.name = "helper"
l2 = g2.new_node("llm", 0, 0); l2.props.update(LLM)
g2.add_edge(l2.id, a2.id)
kwx = g2.new_node("rag", 0, 0); kwx.name = "wxdocs"
kwx.props["docs_dir"] = DOCS_A
kwx.props["description"] = "wxPython UI layout docs (sizers, panels)."
g2.add_edge(kwx.id, a2.id)
kbill = g2.new_node("rag", 0, 0); kbill.name = "billing"
kbill.props["docs_dir"] = DOCS_B
kbill.props["description"] = "Billing and invoice policy."
g2.add_edge(kbill.id, a2.id)
assert not graph_codegen.analyze(g2)["errors"], graph_codegen.analyze(g2)["errors"]

out2 = graph_codegen.generate_from_graph(g2, "demo_rag_multi", gui=False)
py_compile.compile(os.path.join(out2, "agent.py"), doraise=True)
with open(os.path.join(out2, "config.json"), encoding="utf-8") as f:
    cfg2 = json.load(f)
assert isinstance(cfg2["rag"], list) and len(cfg2["rag"]) == 2, cfg2["rag"]

spec2 = importlib.util.spec_from_file_location(
    "demo_rag_multi_agent", os.path.join(out2, "agent.py"))
m2 = importlib.util.module_from_spec(spec2)
spec2.loader.exec_module(m2)
# one tool per KB, both on the agent
assert "search_wxdocs" in m2.TOOLS and "search_billing" in m2.TOOLS, list(m2.TOOLS)
assert set(m2.AGENTS["helper"]["tools"]) >= {"search_wxdocs", "search_billing"}
# per-KB isolation: each tool only sees its own docs
assert "BoxSizer" in m2.TOOLS["search_wxdocs"]("how do sizers work", 1)
assert "Invoices" in m2.TOOLS["search_billing"]("invoices refunds monthly", 1)
assert m2.TOOLS["search_wxdocs"]("invoices refunds monthly").startswith(
    "No relevant"), "wxdocs KB must not see billing docs"
# node description = tool routing hint
assert "wxPython" in m2.tool_schema("search_wxdocs")["description"]
assert "Billing" in m2.tool_schema("search_billing")["description"]
# both KBs appear in the agent's system-prompt tail
sysp = m2.AGENTS["helper"]["system"]
assert "## Knowledge bases" in sysp, sysp[-400:]
assert "search_wxdocs" in sysp and "search_billing" in sysp, sysp[-400:]
shutil.rmtree(DOCS_A)
shutil.rmtree(DOCS_B)
print("rag multi-kb ok: per-node tools, isolation, descriptions, prompt tail")

# 10. Phase-3 chunk strategies: _rag_chunk_text splits per strategy.
md = "# A\nalpha alpha\n\n## B\nbeta beta beta\n\n## C\ngamma\n"
assert len(mod._rag_chunk_text(md, {"chunk_strategy": "fixed",
                                    "chunk_chars": 1000})) == 1
md_chunks = mod._rag_chunk_text(md, {"chunk_strategy": "markdown",
                                     "chunk_chars": 1000})
assert len(md_chunks) >= 3 and any("## B" in c for c in md_chunks), md_chunks
code = "import os\n\ndef a():\n    return 1\n\ndef b():\n    return 2\n"
code_chunks = mod._rag_chunk_text(code, {"chunk_strategy": "code",
                                         "chunk_chars": 1000})
assert len(code_chunks) >= 2 and any("def b" in c for c in code_chunks), code_chunks
rec = mod._rag_chunk_text("x" * 250, {"chunk_strategy": "recursive",
                                      "chunk_chars": 100})
assert len(rec) >= 3, rec
print("rag chunk strategies ok: fixed/markdown/code/recursive")

# 11. Embedding gate (real _embed) + dense + hybrid retrieval (stubbed embedder)
_real_embed = mod._embed
assert _real_embed([], {"embedding": {"model": ""}}) is None   # no model -> None
assert _real_embed(["x"], {}) is None                          # no embedding cfg

def _stub_embed(texts, cfg):
    vocab = ["sizer", "boxsizer", "invoice", "refund", "cache", "panel"]
    return [[float(t.lower().count(w)) for w in vocab] for t in texts]
mod._embed = _stub_embed
mod.rag_clear()
mod.RAG["docs_dir"] = os.path.join(BASE, "_no_such_docs_dir")
mod.rag_add_chunk("Use a BoxSizer to arrange sizer children in a panel.", source="ui")
mod.rag_add_chunk("Invoices and refund timing are covered here.", source="bill")
mod.rag_invalidate()
mod.RAG["retrieval_algorithm"] = "dense"
mod.RAG["embedding"] = {"model": "stub", "normalize": True}
assert "ui" in mod.TOOLS["search_docs"]("boxsizer panel", 1)
assert "bill" in mod.TOOLS["search_docs"]("invoice refund", 1)
mod.RAG["retrieval_algorithm"] = "hybrid"
mod.rag_invalidate()
assert "bill" in mod.TOOLS["search_docs"]("refund invoice", 1)
print("rag dense + hybrid ok (stubbed embedder); _embed gate returns None offline")

# 12. Offline fallback: dense configured but embeddings unavailable -> BM25.
#     The result now carries an M1 degradation [note:] prefix (see 14c).
mod._embed = lambda texts, cfg: None
mod.rag_invalidate()
mod.RAG["retrieval_algorithm"] = "dense"
_hit = mod.TOOLS["search_docs"]("refund", 1)
assert "bill" in _hit and _hit.startswith("[note:"), _hit
assert "No relevant" in mod.TOOLS["search_docs"]("quantum chromodynamics")
mod.RAG["retrieval_algorithm"] = "bm25"
print("rag offline fallback ok: dense -> bm25 when _embed returns None")

# 13. LLM rerank + query rewrite (stubbed agent LLM; identity-safe).
mod.RAG["retrieval_algorithm"] = "bm25"
mod.RAG["embedding"] = {"model": ""}
def _stub_llm(name, system, messages, emit=lambda s: None):
    if "rank passages" in system.lower():
        return "1, 0", []                  # prefer the 2nd candidate, then 1st
    if "rewrite" in system.lower():
        return "refund", []                # rewrite to a term the docs contain
    return "", []
mod.llm = _stub_llm
mod.rag_clear()
mod.RAG["docs_dir"] = os.path.join(BASE, "_no_such_docs_dir")
mod.rag_add_chunk("alpha refund alpha", source="A")        # weaker bm25 for refund
mod.rag_add_chunk("refund refund refund", source="B")      # stronger bm25
mod.rag_invalidate()
mod.RAG["rerank"] = {"mode": "llm"}
out = mod.TOOLS["search_docs"]("refund", 2)
assert "[A]" in out and "[B]" in out, out
assert out.index("[A]") < out.index("[B]"), "rerank should reorder A ahead of B"
mod.RAG["rerank"] = {"mode": "none"}
mod.RAG["query_transform"] = "none"
mod.rag_invalidate()
assert mod.TOOLS["search_docs"]("how do refunds work", 1).startswith("No relevant")
mod.RAG["query_transform"] = "rewrite"
assert not mod.TOOLS["search_docs"]("how do refunds work", 1).startswith("No relevant")
mod.rag_clear()
print("rag rerank + query-rewrite ok (stubbed llm)")

# 14. Vector-store backends: chroma (real, installed here) + faiss (absent ->
#     degrades to the in-RAM store). Both exercised offline via the stub embed.
os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")   # no chroma phone-home
mod._embed = _stub_embed
mod.rag_clear()
mod.RAG["docs_dir"] = os.path.join(BASE, "_no_such_docs_dir")
mod.RAG["retrieval_algorithm"] = "dense"
mod.RAG["embedding"] = {"model": "stub", "normalize": True}
mod.RAG["rerank"] = {"mode": "none"}
mod.RAG["query_transform"] = "none"
mod.rag_add_chunk("Use a BoxSizer to arrange sizer children in a panel.", source="ui")
mod.rag_add_chunk("Invoices and refund timing are covered here.", source="bill")
import importlib.util as _u2  # noqa: E402
_chroma_dir = os.path.join(mod.BASE_DIR, "rag_chroma")
if _u2.find_spec("chromadb"):
    mod.RAG["vector_db"] = "chroma"
    mod.rag_invalidate()
    assert "bill" in mod.TOOLS["search_docs"]("invoice refund", 1)
    assert "ui" in mod.TOOLS["search_docs"]("boxsizer panel", 1)
    assert os.path.isdir(_chroma_dir), "chroma persistent store not created"
    shutil.rmtree(_chroma_dir, ignore_errors=True)
    print("rag chroma backend ok (persistent store created, stubbed embedder)")
else:
    print("rag chroma backend skipped (chromadb not installed)")
mod.RAG["vector_db"] = "faiss"            # faiss absent here -> in-RAM fallback
mod.rag_invalidate()
assert "bill" in mod.TOOLS["search_docs"]("invoice refund", 1)
print("rag faiss backend ok (degrades to in-RAM store when faiss absent)")
# qdrant: embedded on-disk when qdrant-client is installed; degrades to the in-RAM
# store otherwise. Either way retrieval must return the right chunk (stub embedder).
_qdrant_dir = os.path.join(mod.BASE_DIR, "rag_qdrant")
mod.RAG["vector_db"] = "qdrant"
mod.rag_invalidate()
assert "bill" in mod.TOOLS["search_docs"]("invoice refund", 1)
assert "ui" in mod.TOOLS["search_docs"]("boxsizer panel", 1)
if _u2.find_spec("qdrant_client"):
    assert os.path.isdir(_qdrant_dir), "qdrant embedded store not created"
    print("rag qdrant backend ok (embedded on-disk store created, stubbed embedder)")
else:
    print("rag qdrant backend ok (degrades to in-RAM store when qdrant-client absent)")
shutil.rmtree(_qdrant_dir, ignore_errors=True)
mod.RAG["vector_db"] = "memory"
mod.rag_clear()
shutil.rmtree(_chroma_dir, ignore_errors=True)

# 14b. Relevance grading (Self-RAG style, opt-in): the agent LLM drops
#      clearly-irrelevant chunks before they reach the answer.
mod._embed = lambda texts, cfg: None
mod.RAG["retrieval_algorithm"] = "bm25"
mod.RAG["embedding"] = {"model": ""}
mod.RAG["rerank"] = {"mode": "none"}
mod.RAG["query_transform"] = "none"
mod.RAG["vector_db"] = "memory"
mod.rag_clear()
mod.RAG["docs_dir"] = os.path.join(BASE, "_no_such_docs_dir")
mod.rag_add_chunk("Refunds are processed within 30 days of the request.", source="refund")
mod.rag_add_chunk("Use a BoxSizer to arrange children in a panel.", source="ui")
mod.rag_invalidate()
_grade_reply = ["0"]                 # which passage numbers the grader keeps
def _grade_llm(name, system, messages, emit=lambda s: None):
    return (_grade_reply[0], []) if "judge which passages" in system.lower() else ("", [])
mod.llm = _grade_llm
mod.RAG["grade_docs"] = True
# query terms that the refund chunk contains (no stemming, so "refunds"/"processed",
# not "refund"); ui chunk matches neither -> refund ranks first as candidate [0].
out = mod.TOOLS["search_docs"]("refunds processed", 4)
assert "[refund]" in out and "[ui]" not in out, ("grading should drop the irrelevant chunk", out)
_grade_reply[0] = "none"             # grader says nothing is relevant
assert mod.TOOLS["search_docs"]("refunds processed", 4).startswith(
    "No documents passed relevance grading"), "grader 'none' -> filtered all"
_grade_reply[0] = "banana"           # unparseable -> no-op (keep all, never worse)
out = mod.TOOLS["search_docs"]("refunds processed", 4)
assert "[refund]" in out and "[ui]" in out, ("unparseable grade -> keep all", out)
mod.RAG["grade_docs"] = False
mod.rag_clear()
print("rag relevance grading ok (filter / 'none' / unparseable-no-op)")

# 14c. Degradation advisory (M1): dense configured but embeddings unavailable ->
#      BM25, surfaced as a [note:] prefix so the silent quality drop is visible.
mod._embed = lambda texts, cfg: None
mod.RAG["retrieval_algorithm"] = "dense"
mod.RAG["embedding"] = {"model": "stub"}
mod.RAG["grade_docs"] = False
mod.RAG["query_transform"] = "none"
mod.RAG["rerank"] = {"mode": "none"}
mod.RAG["vector_db"] = "memory"
mod.rag_clear()
mod.RAG["docs_dir"] = os.path.join(BASE, "_no_such_docs_dir")
mod.rag_add_chunk("Refunds are processed within 30 days.", source="refund")
mod.rag_invalidate()
out = mod.TOOLS["search_docs"]("processed", 1)   # term present in the chunk (no stemming)
assert out.startswith("[note:") and "semantic search unavailable" in out, ("M1 advisory missing", out)
assert "[refund]" in out, out
mod.RAG["retrieval_algorithm"] = "bm25"      # baseline must stay silent
mod.rag_invalidate()
assert not mod.TOOLS["search_docs"]("processed", 1).startswith("[note:"), "bm25 baseline has no note"
mod.RAG["embedding"] = {"model": ""}
mod.rag_clear()
print("rag degradation advisory ok (dense->bm25 surfaces [note:]; bm25 silent)")

# 14d. Corrective re-retrieval (CRAG, opt-in): a search that finds nothing
#      rewrites the query (LLM) and retries, bounded by corrective_max_rewrites.
mod._embed = lambda texts, cfg: None
mod.RAG["retrieval_algorithm"] = "bm25"
mod.RAG["embedding"] = {"model": ""}
mod.RAG["rerank"] = {"mode": "none"}
mod.RAG["query_transform"] = "none"
mod.RAG["grade_docs"] = False
mod.rag_clear()
mod.RAG["docs_dir"] = os.path.join(BASE, "_no_such_docs_dir")
mod.rag_add_chunk("Refunds are processed within 30 days.", source="refund")
mod.rag_invalidate()
_rw = ["processed"]                  # what the corrective rewrite returns
def _corr_llm(name, system, messages, emit=lambda s: None):
    return (_rw[0], []) if "different search query" in system.lower() else ("", [])
mod.llm = _corr_llm
mod.RAG["corrective"] = True
mod.RAG["corrective_max_rewrites"] = 2
# 'reimbursement' misses every chunk; the rewrite -> 'processed' hits the refund chunk.
out = mod.TOOLS["search_docs"]("reimbursement", 1)
assert "[refund]" in out and "retried with 'processed'" in out, ("corrective retry failed", out)
# bounded: when the rewrite still never matches, the loop stops (no infinite loop).
_rw[0] = "alsonothing"
out2 = mod.TOOLS["search_docs"]("reimbursement", 1)
assert out2.startswith("[note:") and "No relevant" in out2, ("corrective should bound + report", out2)
mod.RAG["corrective"] = False
mod.rag_clear()
print("rag corrective re-retrieval ok (rewrite+retry finds it; bounded when it can't)")

# 15. evict-used-retrievals (opt-in): a newer search elides earlier RAG results.
assert getattr(mod.TOOLS["search_docs"], "_rag_tool", False) is True
assert mod.RAG_EVICT_USED is False               # default off -> no eviction
_msgs = [
    {"role": "user", "content": "q"},
    {"role": "tool", "tool_call_id": "a", "name": "search_docs", "content": "CHUNKS_A"},
    {"role": "tool", "tool_call_id": "b", "name": "other_tool", "content": "KEEP_ME"},
    {"role": "tool", "tool_call_id": "c", "name": "search_docs", "content": "CHUNKS_C"}]
mod._evict_used_rag(_msgs)
assert _msgs[1].get("_evicted") and "elided" in _msgs[1]["content"], _msgs[1]
assert _msgs[3]["content"] == "CHUNKS_C", "latest retrieval kept intact"
assert _msgs[2]["content"] == "KEEP_ME", "non-RAG tool result untouched"
g_ev = Graph()
a_ev = g_ev.new_node("agent", 0, 0); a_ev.name = "solo"
l_ev = g_ev.new_node("llm", 0, 0); l_ev.props.update(LLM)
g_ev.add_edge(l_ev.id, a_ev.id)
r_ev = g_ev.new_node("rag", 0, 0); r_ev.props["docs_dir"] = DOCS
r_ev.props["evict_used"] = True
g_ev.add_edge(r_ev.id, a_ev.id)
out_ev = graph_codegen.generate_from_graph(g_ev, "demo_rag_evict", gui=False)
spec_ev = importlib.util.spec_from_file_location(
    "demo_rag_evict_agent", os.path.join(out_ev, "agent.py"))
m_ev = importlib.util.module_from_spec(spec_ev); spec_ev.loader.exec_module(m_ev)
assert m_ev.RAG_EVICT_USED is True, "evict_used RAG node -> RAG_EVICT_USED True"
print("rag evict-used ok: newer search elides earlier results (opt-in)")

# 16. Qdrant vector store codegen: adds the qdrant-client requirement and emits
#     vector_db=qdrant; server creds appear ONLY when a URL is set (embedded/blank
#     stays byte-clean, like the other opt-in Extra Settings).
g_q = Graph()
a_q = g_q.new_node("agent", 0, 0); a_q.name = "solo"
l_q = g_q.new_node("llm", 0, 0); l_q.props.update(LLM)
g_q.add_edge(l_q.id, a_q.id)
r_q = g_q.new_node("rag", 0, 0); r_q.props["docs_dir"] = DOCS
r_q.props["retrieval_algorithm"] = "dense"
r_q.props["vector_db"] = "qdrant"
r_q.props["qdrant_url"] = "http://localhost:6333"
r_q.props["qdrant_api_key"] = "secret"
g_q.add_edge(r_q.id, a_q.id)
assert not graph_codegen.analyze(g_q)["errors"], graph_codegen.analyze(g_q)["errors"]
out_q = graph_codegen.generate_from_graph(g_q, "demo_rag_qdrant", gui=False)
py_compile.compile(os.path.join(out_q, "agent.py"), doraise=True)
with open(os.path.join(out_q, "requirements.txt"), encoding="utf-8") as f:
    _reqs = f.read()
assert "qdrant-client" in _reqs, _reqs
with open(os.path.join(out_q, "config.json"), encoding="utf-8") as f:
    cfg_q = json.load(f)
assert cfg_q["rag"][0]["vector_db"] == "qdrant", cfg_q["rag"][0]
assert cfg_q["rag"][0]["qdrant_url"] == "http://localhost:6333", cfg_q["rag"][0]
assert cfg_q["rag"][0]["qdrant_api_key"] == "secret", cfg_q["rag"][0]
r_q.props["qdrant_url"] = ""; r_q.props["qdrant_api_key"] = ""   # embedded on-disk
out_q2 = graph_codegen.generate_from_graph(g_q, "demo_rag_qdrant2", gui=False)
with open(os.path.join(out_q2, "config.json"), encoding="utf-8") as f:
    cfg_q2 = json.load(f)
assert "qdrant_url" not in cfg_q2["rag"][0], cfg_q2["rag"][0]
assert "qdrant_api_key" not in cfg_q2["rag"][0], cfg_q2["rag"][0]
print("rag qdrant codegen ok: requirement added, server creds only when URL set")

shutil.rmtree(DOCS)
print("\nALL RAG CHECKS PASSED")
