# ── RAG: local retrieval over per-node knowledge bases (Pillar 8) ────────────
# config["rag"] is a LIST of KB dicts (one per canvas RAG node); a legacy single
# dict is accepted and wrapped as the sole "_default" KB. Each KB carries name,
# tool, docs_dir, chunk_chars/chunk_strategy/chunk_overlap, top_k, plus optional
# pipeline knobs: retrieval_algorithm (bm25|dense|hybrid), recall_n, mmr/
# mmr_lambda, rerank{mode,model}, query_transform, embedding{model,base_url,
# api_key,normalize}, vector_db. Every knob defaults to the plain offline BM25
# baseline; dense/hybrid/rerank/rewrite degrade to BM25/identity with no network
# or api key. Several KBs => one search tool each, so the agent can route.
RAG_EXTS = (".txt", ".md", ".rst", ".py", ".json", ".csv", ".log", ".html")
RAG_STATE_PATH = os.path.join(BASE_DIR, "rag_state.json")
RAG_CHUNKS_PATH = os.path.join(BASE_DIR, "rag_chunks.json")


def _rag_load_kbs() -> dict:
    raw = CONFIG.get("rag", [])
    kbs = {}
    if isinstance(raw, dict) and raw:                       # legacy single KB
        kbs["_default"] = {"name": "_default", "tool": "search_docs", **raw}
    elif isinstance(raw, list):
        for kb in raw:
            if not isinstance(kb, dict):
                continue
            name = kb.get("name") or "_default"
            kbs[name] = {**kb, "name": name,
                         "tool": kb.get("tool") or "search_docs"}
    return kbs


_RAG_KBS = _rag_load_kbs()
# `RAG` stays a back-compat alias to the primary KB's config dict (same object),
# so callers/tests that read or mutate RAG[...] affect that KB.
RAG = _RAG_KBS.get("_default") or next(iter(_RAG_KBS.values()), None) or {}
# per-KB lazy index, keyed by KB name: chunks + (dense) vectors
_RAG_INDEX = {name: {"chunks": None, "vectors": None} for name in _RAG_KBS}


def rag_invalidate() -> None:
    """Drop every cached KB index (chunks + vectors + any ANN index) so the
    next search rebuilds it."""
    for idx in _RAG_INDEX.values():
        idx["chunks"] = None
        idx["vectors"] = None
        idx.pop("faiss", None)


# enable / disable (persisted; toggled from the GUI's RAG menu) ──────────────
def rag_enabled() -> bool:
    try:
        with open(RAG_STATE_PATH, encoding="utf-8") as f:
            return bool(json.load(f).get("enabled", True))
    except (OSError, json.JSONDecodeError):
        return True


def set_rag_enabled(value: bool) -> None:
    with open(RAG_STATE_PATH, "w", encoding="utf-8") as f:
        json.dump({"enabled": bool(value)}, f)


# GUI-managed chunk store (added/updated/deleted from the GUI) ───────────────
def rag_manual_chunks() -> list:
    try:
        with open(RAG_CHUNKS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _rag_save_manual(chunks: list) -> None:
    with open(RAG_CHUNKS_PATH, "w", encoding="utf-8") as f:
        json.dump(chunks, f, indent=2, ensure_ascii=False)
    rag_invalidate()


def rag_add_chunk(text: str, source: str = "manual") -> str:
    chunks = rag_manual_chunks()
    cid = max((c.get("id", 0) for c in chunks), default=0) + 1
    chunks.append({"id": cid, "source": source or "manual", "text": text})
    _rag_save_manual(chunks)
    return f"added chunk #{cid}"


def rag_update_chunk(chunk_id: int, text: str) -> bool:
    chunks = rag_manual_chunks()
    for c in chunks:
        if c.get("id") == chunk_id:
            c["text"] = text
            _rag_save_manual(chunks)
            return True
    return False


def rag_delete_chunk(chunk_id: int) -> bool:
    chunks = rag_manual_chunks()
    kept = [c for c in chunks if c.get("id") != chunk_id]
    if len(kept) == len(chunks):
        return False
    _rag_save_manual(kept)
    return True


def rag_clear() -> None:
    """Clear the GUI-managed chunk store and the in-memory index."""
    _rag_save_manual([])


def rag_remove_source(source: str) -> int:
    """Delete all managed chunks that came from one file/source; returns count."""
    chunks = rag_manual_chunks()
    kept = [c for c in chunks if c.get("source") != source]
    removed = len(chunks) - len(kept)
    if removed:
        _rag_save_manual(kept)
    return removed


def rag_docs_files() -> list:
    """Files in the docs folder that are auto-indexed (relative paths)."""
    docs_dir = RAG.get("docs_dir", "")
    out = []
    if os.path.isdir(docs_dir):
        for root, _dirs, files in os.walk(docs_dir):
            for fn in sorted(files):
                if fn.lower().endswith(RAG_EXTS):
                    out.append(os.path.relpath(os.path.join(root, fn),
                                               docs_dir))
    return sorted(out)


def _rag_extract_text(path: str) -> str:
    """Extract plain text from a file for indexing. PDF needs `pypdf`, .docx
    needs `python-docx` (both optional). Raises ValueError with a helpful
    message for unsupported types / missing libraries."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        try:
            import pypdf
        except ImportError:
            raise ValueError("reading PDF needs pypdf — run: pip install pypdf")
        reader = pypdf.PdfReader(path)
        return "\n".join((pg.extract_text() or "") for pg in reader.pages)
    if ext == ".docx":
        try:
            import docx
        except ImportError:
            raise ValueError("reading .docx needs python-docx — run: "
                             "pip install python-docx")
        return "\n".join(p.text for p in docx.Document(path).paragraphs)
    if ext in RAG_EXTS:
        with open(path, encoding="utf-8", errors="ignore") as f:
            return f.read()
    raise ValueError(f"unsupported file type '{ext}'. Supported: "
                     + ", ".join(RAG_EXTS) + ", .pdf, .docx")


def rag_add_file(path: str) -> str:
    """Ingest a file: extract its text, chunk it, and add the chunks to the
    managed store (tagged with the file name). Returns a status string."""
    try:
        text = _rag_extract_text(path)
    except Exception as e:
        return f"[ERROR] {e}"
    if not text.strip():
        return f"[ERROR] no extractable text in {os.path.basename(path)}."
    source = os.path.basename(path)
    chunks = rag_manual_chunks()
    nxt = max((c.get("id", 0) for c in chunks), default=0) + 1
    added = 0
    # Honor the KB's configured chunk_strategy/overlap (was a hardcoded fixed
    # split that ignored them, so manually-added files chunked differently from
    # docs-folder files).
    for piece in _rag_chunk_text(text, RAG):
        if piece.strip():
            chunks.append({"id": nxt, "source": source, "text": piece})
            nxt += 1
            added += 1
    _rag_save_manual(chunks)
    return f"added {added} chunk(s) from {source}"


_CJK = "一-鿿㐀-䶿豈-﫿぀-ヿ가-힯"
_CJK_TOK = re.compile(rf"[{_CJK}]|[^\W{_CJK}]+")
_CJK_CHAR = re.compile(rf"[{_CJK}]")


def _cjk_grams(run: list) -> list:
    """A maximal CJK run -> overlapping character bigrams (a single char stays
    itself)."""
    if len(run) <= 1:
        return run
    return [run[i] + run[i + 1] for i in range(len(run) - 1)]


def _rag_tokenize(text: str) -> list:
    """BM25 terms. ASCII/Latin/digit word runs pass through exactly like the old
    re.findall(r'\\w+', text.lower()); each contiguous CJK (Chinese/Japanese/
    Korean) run becomes overlapping character bigrams so Chinese queries
    actually match — plain \\w+ fuses a whole CJK run into ONE token, which made
    CJK retrieval near-useless. Index and query MUST share this function so the
    bigram terms line up."""
    out, run = [], []
    for tok in _CJK_TOK.findall(text.lower()):
        if _CJK_CHAR.match(tok):              # a single CJK character
            run.append(tok)
            continue
        if run:                               # flush the pending CJK run
            out.extend(_cjk_grams(run))
            run = []
        out.append(tok)
    if run:
        out.extend(_cjk_grams(run))
    return out


def _rag_split_fixed(text: str, size: int, overlap: int) -> list:
    """Char-window split (byte-identical to the original splitter)."""
    pieces, pos, step = [], 0, max(1, size - overlap)
    while pos < len(text):
        piece = text[pos:pos + size]
        if piece.strip():
            pieces.append(piece)
        pos += step
    return pieces


def _rag_split_recursive(text: str, size: int, overlap: int) -> list:
    """Split on a separator cascade (paragraph -> line -> sentence -> space),
    then greedily merge atoms up to `size` with a char overlap. Keeps natural
    boundaries; dependency-free (no LangChain)."""
    units = [text]
    for sep in ("\n\n", "\n", "。", "！", "？", "! ", "? ", ". ", " "):
        if all(len(u) <= size for u in units):
            break
        nxt = []
        for u in units:
            if len(u) <= size:
                nxt.append(u)
                continue
            parts = u.split(sep)
            for i, p in enumerate(parts):
                nxt.append(p + (sep if i < len(parts) - 1 else ""))
        units = nxt
    atoms = []
    for u in units:                                   # hard-split any leftover
        while len(u) > size:
            atoms.append(u[:size])
            u = u[size:]
        if u:
            atoms.append(u)
    chunks, cur = [], ""
    for a in atoms:
        if cur and len(cur) + len(a) > size:
            chunks.append(cur)
            cur = (cur[-overlap:] if overlap else "") + a
        else:
            cur += a
    if cur:
        chunks.append(cur)
    return [c for c in chunks if c.strip()]


_MD_HEADER = re.compile(r"#{1,6}\s")
_CODE_DEF = re.compile(r"\s*(async\s+def |def |class )")


def _rag_split_by(text: str, size: int, overlap: int, pattern) -> list:
    """Split before each line matching `pattern` (markdown header / code def);
    recursively split any section still larger than `size`."""
    sections, cur = [], ""
    for ln in text.splitlines(keepends=True):
        if pattern.match(ln) and cur.strip():
            sections.append(cur)
            cur = ln
        else:
            cur += ln
    if cur.strip():
        sections.append(cur)
    out = []
    for sec in sections:
        if len(sec) > size:
            out.extend(_rag_split_recursive(sec, size, overlap))
        elif sec.strip():
            out.append(sec)
    return out


def _rag_chunk_text(text: str, cfg: dict) -> list:
    """Split a document into chunk strings per the KB's chunk_strategy. 'fixed'
    (overlap 0 => size//8) reproduces the original splitter byte-for-byte."""
    size = max(100, int(cfg.get("chunk_chars", 800)))
    ov = int(cfg.get("chunk_overlap", 0) or 0)
    overlap = ov if ov > 0 else size // 8
    strategy = cfg.get("chunk_strategy", "fixed")
    if strategy == "recursive":
        return _rag_split_recursive(text, size, overlap)
    if strategy == "markdown":
        return _rag_split_by(text, size, overlap, _MD_HEADER)
    if strategy == "code":
        return _rag_split_by(text, size, overlap, _CODE_DEF)
    return _rag_split_fixed(text, size, overlap)


def _rag_parent_child(text: str, cfg: dict) -> list:
    """Small-to-big / parent-child (§4.3): split into big PARENT blocks
    (parent_chunk_chars, respecting the KB's chunk_strategy), then split each
    parent into small CHILD pieces. Returns [(child, parent, parent_idx)] — the
    children are indexed/embedded for precise retrieval; the parent is what gets
    returned to the LLM for fuller context. Resolves the block-too-big-vs-too-small
    tension the doc calls out."""
    size = max(100, int(cfg.get("chunk_chars", 800)))
    ov = int(cfg.get("chunk_overlap", 0) or 0)
    overlap = ov if ov > 0 else size // 8
    psize = max(size + 100, int(cfg.get("parent_chunk_chars", 2400)))
    child_size = min(size, max(100, psize // 2))       # child strictly < parent
    parents = _rag_chunk_text(text, {**cfg, "chunk_chars": psize})
    out = []
    for pi, parent in enumerate(parents):
        children = _rag_split_recursive(parent, child_size, overlap) or [parent]
        for child in children:
            if child.strip():
                out.append((child, parent, pi))
    return out


def _rag_chunks(kb_name: str) -> list:
    """Lazily build one KB's index: its docs-folder files + GUI-managed chunks.
    (Manual chunks are a single shared store in this version; per-KB stores
    arrive with the multi-KB GUI.)"""
    idx = _RAG_INDEX.setdefault(kb_name, {"chunks": None, "vectors": None})
    if idx["chunks"] is not None:
        return idx["chunks"]
    cfg = _RAG_KBS.get(kb_name, {})
    parent_child = cfg.get("retrieval_granularity") == "parent_child"
    chunks = []
    docs_dir = cfg.get("docs_dir", "")
    if os.path.isdir(docs_dir):
        for root, _dirs, files in os.walk(docs_dir):
            for fname in sorted(files):
                if not fname.lower().endswith(RAG_EXTS):
                    continue
                path = os.path.join(root, fname)
                try:
                    with open(path, encoding="utf-8", errors="ignore") as f:
                        text = f.read()
                except OSError:
                    continue
                src = os.path.relpath(path, docs_dir)
                if parent_child:
                    for child, parent, pi in _rag_parent_child(text, cfg):
                        chunks.append({"source": src, "text": child,
                                       "tokens": _rag_tokenize(child),
                                       "parent": parent,
                                       "parent_id": "%s#%d" % (src, pi)})
                else:
                    for piece in _rag_chunk_text(text, cfg):
                        chunks.append({"source": src, "text": piece,
                                       "tokens": _rag_tokenize(piece)})
    for c in rag_manual_chunks():
        txt = c.get("text", "")
        if txt.strip():
            d = {"source": c.get("source", "manual"), "text": txt,
                 "tokens": _rag_tokenize(txt)}
            if parent_child:                    # a manual chunk is its own parent
                d["parent"] = txt
                d["parent_id"] = "manual#%s" % c.get("id", len(chunks))
            chunks.append(d)
    idx["chunks"] = chunks
    idx["vectors"] = None          # force re-embed on the next dense/hybrid call
    return chunks


def _bm25_scores(query_tokens: list, chunks: list,
                 k1: float = 1.5, b: float = 0.75) -> list:
    n = len(chunks)
    avgdl = sum(len(c["tokens"]) for c in chunks) / max(1, n)
    df = {}
    for c in chunks:
        for t in set(c["tokens"]):
            df[t] = df.get(t, 0) + 1
    scores = []
    for c in chunks:
        tf = {}
        for t in c["tokens"]:
            tf[t] = tf.get(t, 0) + 1
        s = 0.0
        for t in query_tokens:
            if t not in tf:
                continue
            idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
            denom = tf[t] + k1 * (1 - b + b * len(c["tokens"]) / max(1.0, avgdl))
            s += idf * tf[t] * (k1 + 1) / denom
        scores.append(s)
    return scores


# ── dense retrieval helpers (reuse the agent's OpenAI-compatible client) ──────
def _l2norm(v: list) -> list:
    s = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / s for x in v]


def _cosine(a: list, b: list) -> float:
    da = math.sqrt(sum(x * x for x in a)) or 1.0
    db = math.sqrt(sum(y * y for y in b)) or 1.0
    return sum(x * y for x, y in zip(a, b)) / (da * db)


def _rag_embed_endpoint(cfg: dict):
    """(base_url, api_key) for embeddings: the KB's own, else borrow the first
    non-anthropic (OpenAI-compatible) chat LLM config. Never the Anthropic key."""
    emb = cfg.get("embedding") or {}
    base_url, api_key = (emb.get("base_url") or ""), (emb.get("api_key") or "")
    if not api_key:
        for cfgs in (CONFIG.get("llms") or {}).values():
            for lc in cfgs:
                if lc.get("provider") != "anthropic" and lc.get("api_key"):
                    base_url = base_url or lc.get("base_url") or ""
                    api_key = lc.get("api_key")
                    break
            if api_key:
                break
    return base_url, api_key


_RAG_LOCAL_EMB = {}


def _embed_local(model, texts):
    """Free, NO-API-KEY local embeddings. Tries fastembed (light ONNX) then
    sentence-transformers; the model downloads once then runs fully offline.
    None if neither library is installed / any error -> caller falls back to
    BM25. Encoders are cached (loading is expensive)."""
    try:
        enc = _RAG_LOCAL_EMB.get(("fastembed", model))
        if enc is None:
            from fastembed import TextEmbedding
            enc = TextEmbedding(model_name=model)
            _RAG_LOCAL_EMB[("fastembed", model)] = enc
        return [[float(x) for x in v] for v in enc.embed(list(texts))]
    except Exception:
        pass
    try:
        enc = _RAG_LOCAL_EMB.get(("st", model))
        if enc is None:
            from sentence_transformers import SentenceTransformer
            enc = SentenceTransformer(model)
            _RAG_LOCAL_EMB[("st", model)] = enc
        return [[float(x) for x in v] for v in enc.encode(list(texts))]
    except Exception:
        return None


def _embed_openai(model, texts, cfg):
    """Embeddings via an OpenAI-compatible API (needs a key)."""
    try:
        from openai import OpenAI
    except Exception:
        return None
    base_url, api_key = _rag_embed_endpoint(cfg)
    if not api_key:
        return None
    try:
        client = _clients.setdefault(
            ("__embed__", model, base_url),
            OpenAI(api_key=api_key, base_url=base_url or None))
        rsp = client.embeddings.create(model=model, input=list(texts))
        return [list(d.embedding) for d in rsp.data]
    except Exception:
        return None


def _embed(texts, cfg):
    """Embed texts, returning a list of vectors or None. Gated on an explicit
    embedding MODEL first, so the BM25 path is the default and tests that don't
    stub embeddings never touch the network. The default provider is 'local'
    (free, no API key — fastembed/sentence-transformers); 'openai' uses an
    OpenAI-compatible API. Any failure -> None -> BM25 fallback (offline-safe)."""
    emb = cfg.get("embedding") or {}
    model = (emb.get("model") or "").strip()
    if not model:
        return None
    if (emb.get("provider") or "local") == "openai":
        vecs = _embed_openai(model, list(texts), cfg)
    else:
        vecs = _embed_local(model, list(texts))
    if vecs is None:
        return None
    if emb.get("normalize", True):
        vecs = [_l2norm(v) for v in vecs]
    return vecs


def _rag_vectors(kb_name: str, chunks: list, cfg: dict):
    """Per-KB chunk vectors, embedded lazily. False = tried and unavailable
    (stay on BM25); None = not yet attempted."""
    idx = _RAG_INDEX.setdefault(kb_name, {"chunks": None, "vectors": None})
    if idx.get("vectors") is not None:
        return idx["vectors"]
    vecs = _embed([c["text"] for c in chunks], cfg)
    idx["vectors"] = vecs if (vecs and len(vecs) == len(chunks)) else False
    return idx["vectors"]


# ── ranking: bm25 / dense / hybrid (RRF), with offline fallback to bm25 ───────
def _rag_rank_bm25(query: str, chunks: list) -> list:
    """(idx, score) for every chunk, highest first — same ordering as the
    original BM25 path (sort by (score, idx) descending)."""
    scores = _bm25_scores(_rag_tokenize(query), chunks)
    return [(i, s) for s, i in sorted(zip(scores, range(len(chunks))),
                                      reverse=True)]


def _rag_dense_mem(kb_name, query, chunks, cfg):
    """Built-in brute-force cosine over in-RAM vectors. None if embeddings
    aren't available."""
    vecs = _rag_vectors(kb_name, chunks, cfg)
    qv = _embed([query], cfg) if vecs else None
    if not vecs or not qv:
        return None
    qv = qv[0]
    return sorted(((i, _cosine(qv, vecs[i])) for i in range(len(chunks))),
                  key=lambda t: t[1], reverse=True)


def _rag_faiss_rank(kb_name, query, chunks, cfg):
    """FAISS ANN over the embedded chunk vectors (in-memory, rebuilt per
    session). None if faiss/numpy missing or embeddings unavailable -> caller
    falls back to the in-RAM store."""
    try:
        import faiss
        import numpy as np
    except Exception:
        return None
    vecs = _rag_vectors(kb_name, chunks, cfg)
    qv = _embed([query], cfg) if vecs else None
    if not vecs or not qv:
        return None
    try:
        idx = _RAG_INDEX.setdefault(kb_name, {})
        cached = idx.get("faiss")
        if not cached or cached[0] != len(vecs):
            mat = np.asarray(vecs, dtype="float32")
            index = faiss.IndexFlatIP(mat.shape[1])   # IP == cosine (normalized)
            index.add(mat)
            cached = (len(vecs), index)
            idx["faiss"] = cached
        n = min(max(int(cfg.get("top_k", 4)) * 5, 20), len(vecs))
        scores, ids = cached[1].search(np.asarray([qv[0]], dtype="float32"), n)
        return [(int(i), float(s)) for i, s in zip(ids[0], scores[0]) if i >= 0]
    except Exception:
        return None


_RAG_CHROMA = {}


def _rag_chroma_rank(kb_name, query, chunks, cfg):
    """Persistent vector store via chromadb: chunk embeddings are saved to disk
    and reused across sessions while the docs are unchanged (a content hash in a
    sidecar .sig file gates a rebuild). None if chromadb missing / embeddings
    unavailable / any backend error -> caller falls back to the in-RAM store."""
    try:
        import chromadb
    except Exception:
        return None
    qv = _embed([query], cfg)
    if not qv:
        return None
    try:
        import hashlib
        client = _RAG_CHROMA.get("client")
        if client is None:
            client = chromadb.PersistentClient(
                path=os.path.join(BASE_DIR, "rag_chroma"))
            _RAG_CHROMA["client"] = client
        cname = "kb_" + re.sub(r"[^a-zA-Z0-9]", "_", kb_name)[:48]
        sig = hashlib.md5(
            "\x00".join(c["text"] for c in chunks).encode("utf-8")).hexdigest()
        sig_path = os.path.join(BASE_DIR, "rag_chroma", cname + ".sig")
        col = client.get_or_create_collection(
            name=cname, metadata={"hnsw:space": "cosine"})
        persisted = ""
        try:
            with open(sig_path, encoding="utf-8") as f:
                persisted = f.read().strip()
        except OSError:
            pass
        if col.count() != len(chunks) or persisted != sig:   # (re)build + persist
            try:
                client.delete_collection(cname)
            except Exception:
                pass
            col = client.get_or_create_collection(
                name=cname, metadata={"hnsw:space": "cosine"})
            vecs = _rag_vectors(kb_name, chunks, cfg)
            if not vecs:
                return None
            col.add(ids=[str(i) for i in range(len(chunks))],
                    embeddings=[list(v) for v in vecs],
                    documents=[c["text"] for c in chunks])
            try:
                with open(sig_path, "w", encoding="utf-8") as f:
                    f.write(sig)
            except OSError:
                pass
        res = col.query(
            query_embeddings=[list(qv[0])],
            n_results=min(max(int(cfg.get("top_k", 4)) * 5, 20), len(chunks)))
        ids = (res.get("ids") or [[]])[0]
        dists = (res.get("distances") or [[]])[0]
        out = [(int(i), 1.0 - float(d)) for i, d in zip(ids, dists)
               if str(i).isdigit()]
        return out or None
    except Exception:
        return None


_RAG_QDRANT = {}


def _rag_qdrant_rank(kb_name, query, chunks, cfg):
    """Persistent vector store via qdrant-client. Runs on-disk in EMBEDDED mode by
    default (BASE_DIR/rag_qdrant, no server, offline) or against a remote Qdrant
    when the KB sets qdrant_url (+ optional qdrant_api_key). A content hash in a
    sidecar .sig gates a rebuild so the collection is reused across sessions while
    the docs are unchanged. None if qdrant-client missing / embeddings unavailable /
    any backend error -> caller falls back to the in-RAM store."""
    try:
        from qdrant_client import QdrantClient
        from qdrant_client.models import Distance, VectorParams, PointStruct
    except Exception:
        return None
    qv = _embed([query], cfg)
    if not qv:
        return None
    try:
        import hashlib
        url = (cfg.get("qdrant_url") or "").strip()
        client = _RAG_QDRANT.get(("client", url))
        if client is None:
            if url:                              # remote server
                client = QdrantClient(
                    url=url, api_key=(cfg.get("qdrant_api_key") or "") or None)
            else:                                # embedded, on-disk (no server)
                client = QdrantClient(path=os.path.join(BASE_DIR, "rag_qdrant"))
            _RAG_QDRANT[("client", url)] = client
        cname = "kb_" + re.sub(r"[^a-zA-Z0-9]", "_", kb_name)[:48]
        sig = hashlib.md5(
            "\x00".join(c["text"] for c in chunks).encode("utf-8")).hexdigest()
        sig_dir = os.path.join(BASE_DIR, "rag_qdrant")
        try:
            os.makedirs(sig_dir, exist_ok=True)
        except OSError:
            pass
        sig_path = os.path.join(sig_dir, cname + ".sig")
        try:
            count = client.count(cname).count
        except Exception:
            count = -1
        persisted = ""
        try:
            with open(sig_path, encoding="utf-8") as f:
                persisted = f.read().strip()
        except OSError:
            pass
        if count != len(chunks) or persisted != sig:      # (re)build + persist
            vecs = _rag_vectors(kb_name, chunks, cfg)
            if not vecs:
                return None
            try:
                client.delete_collection(cname)
            except Exception:
                pass
            client.create_collection(
                collection_name=cname,
                vectors_config=VectorParams(size=len(vecs[0]),
                                            distance=Distance.COSINE))
            client.upsert(collection_name=cname, points=[
                PointStruct(id=i, vector=list(vecs[i]),
                            payload={"text": chunks[i]["text"]})
                for i in range(len(chunks))])
            try:
                with open(sig_path, "w", encoding="utf-8") as f:
                    f.write(sig)
            except OSError:
                pass
        n = min(max(int(cfg.get("top_k", 4)) * 5, 20), len(chunks))
        try:
            hits = client.search(collection_name=cname,
                                 query_vector=list(qv[0]), limit=n)
        except (AttributeError, TypeError):    # newer qdrant-client: search removed
            hits = client.query_points(collection_name=cname,
                                       query=list(qv[0]), limit=n).points
        out = [(int(h.id), float(h.score)) for h in hits]
        return out or None
    except Exception:
        return None


def _rag_dense_rank(kb_name, query, chunks, cfg):
    """Dense ranking [(idx, score)] via the configured vector_db backend.
    chroma/faiss/qdrant fall through to the built-in in-RAM store on any failure
    (missing lib, error); the in-RAM store returns None when embeddings are
    unavailable so the caller can drop to BM25."""
    backend = cfg.get("vector_db", "memory")
    if backend == "chroma":
        r = _rag_chroma_rank(kb_name, query, chunks, cfg)
        if r is not None:
            return r
    elif backend == "faiss":
        r = _rag_faiss_rank(kb_name, query, chunks, cfg)
        if r is not None:
            return r
    elif backend == "qdrant":
        r = _rag_qdrant_rank(kb_name, query, chunks, cfg)
        if r is not None:
            return r
    return _rag_dense_mem(kb_name, query, chunks, cfg)


def _rag_rank(kb_name: str, query: str, chunks: list, cfg: dict,
              notes: list = None) -> list:
    algo = cfg.get("retrieval_algorithm", "bm25")
    if algo not in ("dense", "hybrid"):
        return _rag_rank_bm25(query, chunks)
    dense = _rag_dense_rank(kb_name, query, chunks, cfg)
    if not dense:
        # Configured for semantic search but embeddings are unavailable (no key /
        # lib / offline). We degrade to keyword BM25 — surface it (M1) so the
        # silent quality drop isn't invisible to the agent / user.
        if notes is not None:
            notes.append("semantic search unavailable (no embeddings) — used "
                         "keyword (BM25) matching")
        return _rag_rank_bm25(query, chunks)          # offline / no embeddings
    if algo == "dense":
        return dense
    bm = _rag_rank_bm25(query, chunks)                # hybrid: RRF fuse the two
    rrf = {}
    for rank, (i, _s) in enumerate(dense):
        rrf[i] = rrf.get(i, 0.0) + 1.0 / (60 + rank)
    for rank, (i, _s) in enumerate(bm):
        rrf[i] = rrf.get(i, 0.0) + 1.0 / (60 + rank)
    return sorted(rrf.items(), key=lambda kv: kv[1], reverse=True)


def _rag_mmr(kb_name, query, ranked, chunks, cfg):
    """Re-order a candidate pool for relevance + diversity (MMR). No-op when
    embeddings are unavailable."""
    vecs = _rag_vectors(kb_name, chunks, cfg)
    qv = _embed([query], cfg) if vecs else None
    if not vecs or not qv:
        return ranked
    qv, lam = qv[0], float(cfg.get("mmr_lambda", 0.5))
    pool = [i for i, _s in ranked]
    rel = {i: _cosine(qv, vecs[i]) for i in pool}
    chosen, out = [], []
    while pool:
        best, best_score = None, None
        for i in pool:
            div = max((_cosine(vecs[i], vecs[j]) for j in chosen), default=0.0)
            mmr = lam * rel[i] - (1 - lam) * div
            if best_score is None or mmr > best_score:
                best, best_score = i, mmr
        chosen.append(best)
        pool.remove(best)
        out.append((best, rel[best]))
    return out


# ── optional LLM steps (reuse the agent LLM; identity on any failure) ─────────
def _rag_llm_once(system: str, prompt: str):
    name = (PIPELINE[0] if "PIPELINE" in globals() and PIPELINE else None)
    if name and "llm" in globals():
        text, _ = llm(name, system, [{"role": "user", "content": prompt}],
                      lambda s: None)
    else:
        text, _ = _llm_once(system, [{"role": "user", "content": prompt}])
    return text or ""


def _rag_transform(query: str, mode: str, kb_name: str) -> str:
    if mode != "rewrite":
        return query
    try:
        out = _rag_llm_once(
            "Rewrite the user's question into a concise keyword search query for "
            "a document search engine. Reply with ONLY the rewritten query.",
            query).strip()
        return out or query
    except Exception:
        return query


def _rag_rerank_llm(query: str, ranked: list, chunks: list) -> list:
    """Re-order a candidate pool with the agent LLM (cross-encoder style),
    assigning descending positive scores. Pool unchanged on any failure."""
    cand = ranked[:]
    try:
        listing = "\n".join(f"[{n}] {chunks[i]['text'].strip()[:300]}"
                            for n, (i, _s) in enumerate(cand))
        text = _rag_llm_once(
            "You rank passages by relevance to a query. Reply with ONLY a "
            "comma-separated list of passage numbers, most relevant first.",
            f"Query: {query}\n\nPassages:\n{listing}")
        order, seen = [], set()
        for x in re.findall(r"\d+", text):
            n = int(x)
            if n < len(cand) and n not in seen:
                seen.add(n)
                order.append(n)
        order += [n for n in range(len(cand)) if n not in seen]
        m = len(order)
        return [(cand[n][0], float(m - r)) for r, n in enumerate(order)]
    except Exception:
        return cand


_RAG_RERANKER = {}
_DEFAULT_RERANK_MODEL = "BAAI/bge-reranker-base"


def _cross_encoder_scores(model, query, docs):
    """Cross-encoder relevance scores (0..1-ish) for each (query, doc) pair — the
    query and doc are encoded JOINTLY (§9), so it captures interaction a bi-encoder
    misses. Free, no API key, deterministic; the model downloads once then runs
    offline. Tries fastembed's TextCrossEncoder then sentence-transformers'
    CrossEncoder; None if neither is available. Encoders cached (loading is slow)."""
    try:
        enc = _RAG_RERANKER.get(("fastembed", model))
        if enc is None:
            from fastembed.rerank.cross_encoder import TextCrossEncoder
            enc = TextCrossEncoder(model_name=model)
            _RAG_RERANKER[("fastembed", model)] = enc
        return [float(s) for s in enc.rerank(query, list(docs))]
    except Exception:
        pass
    try:
        enc = _RAG_RERANKER.get(("st", model))
        if enc is None:
            from sentence_transformers import CrossEncoder
            enc = CrossEncoder(model)
            _RAG_RERANKER[("st", model)] = enc
        return [float(s) for s in enc.predict([[query, d] for d in docs])]
    except Exception:
        return None


def _rag_rerank_cross_encoder(query, ranked, chunks, cfg, notes=None):
    """Re-order the candidate pool with a LOCAL cross-encoder (§9's two-stage
    retrieval, second stage): free, no tokens, deterministic, not prompt-injectable
    — unlike the LLM reranker. On any failure (lib/model/network missing) it keeps
    the retrieval order and surfaces a note, so it can only sharpen, never break,
    the baseline (mirrors the dense->BM25 degrade)."""
    if not ranked:
        return ranked
    model = (((cfg.get("rerank") or {}).get("model") or "").strip()
             or _DEFAULT_RERANK_MODEL)
    scores = _cross_encoder_scores(model, query, [chunks[i]["text"] for i, _s in ranked])
    if scores is None or len(scores) != len(ranked):
        if notes is not None:
            notes.append("cross-encoder reranker unavailable — kept retrieval order "
                         "(pip install fastembed or sentence-transformers)")
        return ranked
    order = sorted(range(len(ranked)), key=lambda n: scores[n], reverse=True)
    return [(ranked[n][0], float(scores[n])) for n in order]


def _rag_grade_docs(query: str, ranked: list, chunks: list) -> list:
    """Keep only the passages the agent LLM judges relevant to the query
    (Self-RAG-style document grading). LENIENT — drops only clearly-irrelevant
    chunks. Returns [] when the LLM says NONE are relevant (an honest "the KB
    doesn't cover this"); returns the pool UNCHANGED on any parse/LLM failure, so
    it is offline-safe and never worse than no grading."""
    cand = ranked[:]
    if not cand:
        return cand
    try:
        listing = "\n".join(f"[{n}] {chunks[i]['text'].strip()[:300]}"
                            for n, (i, _s) in enumerate(cand))
        text = _rag_llm_once(
            "You judge which passages are relevant to a search query. A passage "
            "is relevant if it could even partially help answer the query — keep "
            "it unless it is clearly unrelated. Reply with ONLY a comma-separated "
            "list of the relevant passage numbers, or the word none if none are "
            "relevant.",
            f"Query: {query}\n\nPassages:\n{listing}")
        digits = re.findall(r"\d+", text)
        if not digits:
            # 'none' → filter everything; anything else unparseable → no-op.
            return [] if re.search(r"\bnone\b", text, re.I) else cand
        keep = {int(x) for x in digits if int(x) < len(cand)}
        return [cand[n] for n in range(len(cand)) if n in keep] if keep else cand
    except Exception:
        return cand


def _rag_source_match(source: str, pattern: str) -> bool:
    """True if `source` matches any comma-separated glob in `pattern` (matched
    against the full path AND the basename, case-insensitively; \\ normalized to /).
    An empty pattern matches everything."""
    import fnmatch
    pattern = (pattern or "").strip()
    if not pattern:
        return True
    src = str(source or "").replace("\\", "/").lower()
    base = src.rsplit("/", 1)[-1]
    for pat in (p.strip().replace("\\", "/").lower() for p in pattern.split(",")):
        if pat and (fnmatch.fnmatch(src, pat) or fnmatch.fnmatch(base, pat)):
            return True
    return False


def _rag_multi_queries(query: str, n: int, kb_name: str) -> list:
    """LLM-generate up to n-1 alternative search queries and return [query, *variants]
    (deduped, capped at n). Fail-soft -> [query] (offline-safe, mirrors _rag_transform)."""
    n = max(1, int(n or 1))
    if n <= 1:
        return [query]
    try:
        out = _rag_llm_once(
            f"Generate {n - 1} alternative search queries for the user's question — "
            "different phrasings, synonyms, or focused sub-questions — to broaden a "
            "document search. Reply with ONE query per line, no numbering.", query)
        variants = [ln.strip() for ln in (out or "").splitlines() if ln.strip()]
    except Exception:
        variants = []
    seen, uniq = set(), []
    for x in [query] + variants:
        if x.lower() not in seen:
            seen.add(x.lower())
            uniq.append(x)
        if len(uniq) >= n:
            break
    return uniq


def _rag_pipeline(kb_name: str, q: str, query: str, chunks: list, cfg: dict,
                  k: int, notes: list) -> list:
    """One retrieval pass for the query string `q`: rank (bm25/dense/hybrid) ->
    optional MMR + LLM rerank over a wider pool -> optional relevance grading.
    `query` is the ORIGINAL user question (rerank/grading judge against it).
    Returns ranked [(idx, score)]. Every step degrades to the BM25 baseline."""
    ranked = _rag_rank(kb_name, q, chunks, cfg, notes)
    mfilter = (cfg.get("metadata_filter") or "").strip()
    if mfilter:                          # restrict to chunks from matching sources
        ranked = [(i, s) for i, s in ranked
                  if _rag_source_match(chunks[i].get("source", ""), mfilter)]
        if not ranked and notes is not None:
            notes.append(f"metadata_filter '{mfilter}' matched no sources")
    rerank_mode = (cfg.get("rerank") or {}).get("mode", "none")
    use_mmr = bool(cfg.get("mmr", False))
    if use_mmr or rerank_mode in ("llm", "cross_encoder"):
        recall_n = int(cfg.get("recall_n", 0) or 0) or max(k * 5, 20)
        pool = ranked[:recall_n]
        if use_mmr:
            pool = _rag_mmr(kb_name, q, pool, chunks, cfg)
        if rerank_mode == "llm":
            pool = _rag_rerank_llm(query, pool, chunks)
        elif rerank_mode == "cross_encoder":
            pool = _rag_rerank_cross_encoder(query, pool, chunks, cfg, notes)
        ranked = pool
    # Optional relevance grading (Self-RAG style): the agent LLM filters the top
    # candidates to the ones actually relevant. Opt-in, bounded pool, no-op on
    # failure — can only sharpen, never break, the offline baseline.
    if cfg.get("grade_docs"):
        ranked = _rag_grade_docs(query, ranked[:max(k * 4, 20)], chunks)
    return ranked


def _rag_format(ranked: list, chunks: list, k: int, graded: bool,
                parent_child: bool = False, threshold: float = 0.0) -> list:
    """Format the top-k ranked chunks as "[source] (score) text" parts. The BM25
    score<=0 cutoff is skipped when graded (the grader, not the score, decided).
    `threshold` (>0) additionally drops chunks below a min relevance score — the
    caller only passes a non-zero value when scores are absolute (dense/cross-encoder).
    In parent_child mode the matched CHILD is mapped to its bigger parent block,
    parents are de-duplicated (multiple children can share one), and we walk
    deeper than k to still return up to k distinct parents (§4.3)."""
    if not parent_child:
        parts = []
        for idx, score in ranked[:k]:
            if score <= 0 and not graded:
                continue
            if threshold and score < threshold:
                continue
            c = chunks[idx]
            parts.append(f"[{c['source']}] (score {score:.2f})\n{c['text'].strip()}")
        return parts
    parts, seen = [], set()
    for idx, score in ranked:
        if len(parts) >= k:
            break
        if score <= 0 and not graded:
            continue
        if threshold and score < threshold:
            continue
        c = chunks[idx]
        pid = c.get("parent_id", idx)
        if pid in seen:
            continue
        seen.add(pid)
        body = c.get("parent") or c["text"]
        parts.append(f"[{c['source']}] (score {score:.2f})\n{body.strip()}")
    return parts


def _rag_corrective_rewrite(query: str, tried: list) -> str:
    """Rewrite a query that found nothing into a DIFFERENT one (synonyms / broader /
    key entities), avoiding the queries already tried. "" on any failure (the
    caller then stops the corrective loop — offline-safe)."""
    try:
        return _rag_llm_once(
            "A document search for the user's question returned no relevant "
            "results. Rewrite it into a DIFFERENT search query — try synonyms, a "
            "broader phrasing, or the key entities — and do NOT repeat a query "
            "already tried. Reply with ONLY the new query.",
            f"User question: {query}\nAlready tried: {'; '.join(tried)}").strip()
    except Exception:
        return ""


def _rag_search(kb_name: str, query: str, top_k: int = 0) -> str:
    """Retrieve for one KB: optional query rewrite -> rank (bm25/dense/hybrid)
    -> optional MMR + LLM rerank -> optional relevance grading -> top-k, formatted
    with sources. With `corrective` on, a pass that finds nothing rewrites the
    query and retries (bounded by corrective_max_rewrites) — CRAG-style local
    correction. Every step degrades to the plain offline BM25 baseline."""
    if not rag_enabled():
        return ("[RAG disabled] The document knowledge base is turned off. "
                "Answer from your own knowledge or ask the user to enable it.")
    chunks = _rag_chunks(kb_name)
    if not chunks:
        cfg = _RAG_KBS.get(kb_name, {})
        return ("[ERROR] The knowledge base is empty — add chunks via the GUI "
                "RAG menu, or check rag.docs_dir in config.json: "
                + str(cfg.get("docs_dir")))
    cfg = _RAG_KBS.get(kb_name, {})
    k = int(top_k) or int(cfg.get("top_k", 4))
    graded = bool(cfg.get("grade_docs"))
    corrective = bool(cfg.get("corrective"))
    max_extra = int(cfg.get("corrective_max_rewrites", 2) or 0) if corrective else 0
    notes = []                       # advisories surfaced to the agent (e.g. degraded search)

    parent_child = cfg.get("retrieval_granularity") == "parent_child"
    _algo = cfg.get("retrieval_algorithm", "bm25")
    _rr = (cfg.get("rerank") or {}).get("mode", "none")
    _qt = cfg.get("query_transform", "none")
    # An absolute score_threshold is only calibratable where scores are absolute:
    # dense (cosine) or a cross-encoder rerank. bm25 (unbounded) / hybrid & multi_query
    # (tiny RRF sums) / llm-rerank (positional) have no comparable scale, so ignore it
    # there and tell the user rather than silently returning nothing.
    _thr = float(cfg.get("score_threshold", 0) or 0)
    eff_thr = _thr if (_thr > 0 and _qt != "multi_query"
                       and (_algo == "dense" or _rr == "cross_encoder")) else 0.0
    if _thr > 0 and eff_thr == 0.0:
        notes.append("score_threshold ignored — only dense retrieval or a cross-encoder "
                     "rerank produce absolute scores")
    if _qt == "multi_query":
        # fan the question into N LLM variants; run the pipeline per variant and
        # RRF-fuse (same fusion as hybrid) for recall breadth beyond a single rewrite.
        qs = _rag_multi_queries(query, int(cfg.get("multi_query_n", 3) or 3), kb_name)
        fused: dict = {}
        for _q in qs:
            for rank, (idx, _s) in enumerate(
                    _rag_pipeline(kb_name, _q, query, chunks, cfg, k, notes)):
                fused[idx] = fused.get(idx, 0.0) + 1.0 / (60 + rank)
        ranked = sorted(fused.items(), key=lambda x: x[1], reverse=True)
        parts = _rag_format(ranked, chunks, k, graded, parent_child, eff_thr)
    else:
        q = _rag_transform(query, _qt, kb_name)
        tried = [q]
        parts = []
        for attempt in range(max_extra + 1):
            ranked = _rag_pipeline(kb_name, q, query, chunks, cfg, k, notes)
            parts = _rag_format(ranked, chunks, k, graded, parent_child, eff_thr)
            if parts or attempt == max_extra:
                break
            nq = _rag_corrective_rewrite(query, tried)   # nothing found — rewrite + retry
            if not nq or nq in tried:
                break
            notes.append(f"no results for '{q}' — retried with '{nq}'")
            tried.append(nq)
            q = nq

    if not parts:
        body = (("No documents passed relevance grading for: " + query) if graded
                else ("No relevant documents found for: " + query))
    else:
        body = "\n\n---\n\n".join(parts)
    if notes:
        # dedup (the degradation note repeats once per corrective attempt) but keep order
        body = "[note: " + "; ".join(dict.fromkeys(notes)) + "]\n\n" + body
    return body


_RAG_TOOL_ARGS = (
    "\n\nArgs:\n    query: keywords or a question to search for.\n"
    "    top_k: how many chunks to return (0 = use the configured default).")
_RAG_DEFAULT_DESC = (
    "Search the local document knowledge base; returns the most relevant text "
    "chunks with their source files. Call this before answering questions that "
    "depend on the documents.")


def _rag_register_tools() -> None:
    """Register one retrieval tool per KB. The function name is the KB's `tool`
    (so a lone KB keeps the legacy `search_docs`); the KB's description becomes
    the tool's routing hint shown to the model."""
    for _name, _cfg in _RAG_KBS.items():
        def _make(kb):
            def _search(query: str, top_k: int = 0) -> str:
                return _rag_search(kb, query, top_k)
            return _search
        fn = _make(_name)
        fn.__name__ = _cfg.get("tool") or "search_docs"
        fn.__qualname__ = fn.__name__
        desc = (_cfg.get("description") or "").strip() or _RAG_DEFAULT_DESC
        fn.__doc__ = desc + _RAG_TOOL_ARGS
        fn._rag_tool = True          # lets the react loop find & evict stale results
        tool(fn)


_rag_register_tools()
