# ── persistent cross-run memory (Reflexion-style long-term learning) ────────
# A `memory` node linked to an agent gives it TWO tools:
#   remember(content, tags="")  — append a lesson / fact / episode to a store
#   recall(query, k=0)          — BM25-retrieve the most relevant past memories
# The store is a single JSON file under BASE_DIR (memory_store.json), written with
# the shared crash-safe _atomic_write; retrieval REUSES the RAG BM25 tokenizer +
# ranker (_rag_tokenize / _rag_rank_bm25), so a memory node needs no embeddings and
# no extra deps. It SURVIVES process restarts, so the agent can learn across runs:
# recall relevant lessons before acting, remember new ones after. This fragment
# only uses names the host module already defines (BASE_DIR, CONFIG, os/json/time/
# threading, tool, trace_event, _atomic_write) plus _rag_tokenize/_rag_rank_bm25
# from the rag fragment (co-emitted whenever a memory node exists).

MEMORY_PATH = os.path.join(BASE_DIR, "memory_store.json")
MEMORY_TOP_K = int(CONFIG.get("memory_top_k", 5) or 5)
_MEMORY_LOCK = threading.Lock()


def _memory_load() -> list:
    """The stored memories (a list of {id, content, tags, ts}); [] if none / unreadable."""
    try:
        with open(MEMORY_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _memory_save(items) -> None:
    _atomic_write(MEMORY_PATH, json.dumps(items, ensure_ascii=False, indent=2))


def remember(content: str, tags: str = "") -> str:
    """Save a durable lesson, fact or outcome to long-term memory so it survives
    across runs and can be reused next time. Call this after learning something
    worth keeping (a mistake to avoid, a user preference, a result).

    Args:
        content: the lesson or note to store — a short, self-contained sentence.
        tags: optional comma-separated labels to group related memories.
    """
    text = (content or "").strip()
    if not text:
        return "[memory] nothing to remember (empty content)."
    with _MEMORY_LOCK:
        items = _memory_load()
        items.append({"id": len(items) + 1, "content": text,
                      "tags": (tags or "").strip(), "ts": time.time()})
        _memory_save(items)
        n = len(items)
    trace_event("memory", action="remember", total=n)
    return f"[memory] remembered (#{n} total)."


def recall(query: str, k: int = 0) -> str:
    """Search long-term memory for lessons/facts relevant to `query` and return the
    most relevant ones (most-relevant first). Call this BEFORE acting so you reuse
    what earlier runs learned. Falls back to the most RECENT memories if nothing
    matches the query lexically.

    Args:
        query: keywords or a question describing what you need to recall.
        k: how many memories to return (0 = the configured default).
    """
    items = _memory_load()
    if not items:
        return "[memory] (empty — nothing has been remembered yet)."
    top = int(k) if k and int(k) > 0 else MEMORY_TOP_K
    chunks = [{"text": it.get("content", ""),
               "tokens": _rag_tokenize(it.get("content", ""))} for it in items]
    ranked = _rag_rank_bm25(query or "", chunks)
    hits = [i for i, s in ranked if s > 0][:top]
    if not hits:                              # query matched nothing -> most recent
        hits = list(range(len(items)))[-top:][::-1]
    lines = []
    for i in hits:
        it = items[i]
        tag = f" [{it['tags']}]" if it.get("tags") else ""
        lines.append(f"- (#{it.get('id', i + 1)}{tag}) {it.get('content', '')}")
    trace_event("memory", action="recall", returned=len(lines))
    return "Relevant memories:\n" + "\n".join(lines)


def _memory_register_tools() -> None:
    """Register remember/recall in TOOLS (docstring -> tool schema, generic dispatch).
    The memory node's `description` (a 'when should the agent use this?' hint) is
    PREPENDED to both tool docs so it steers the model — mirrors how a RAG node's
    description becomes its search tool's routing hint."""
    _d = (CONFIG.get("memory_description") or "").strip()
    if _d:                                # weave the routing hint into the tool docs
        remember.__doc__ = "This memory store is for: " + _d + "\n\n" + (remember.__doc__ or "")
        recall.__doc__ = "This memory store is for: " + _d + "\n\n" + (recall.__doc__ or "")
    tool(remember)
    tool(recall)


_memory_register_tools()
