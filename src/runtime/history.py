# ── conversation sessions (named, recoverable; one JSON per conversation) ───
# HISTORY/SUMMARY are the ACTIVE session's content. Each turn re-saves the active
# session file; switching sessions never loses work. Empty sessions aren't written.
# Sessions + active pointer + checkpoints persist through _STORE (see storage.py,
# inlined above): disk (default), sqlite, or postgres. The disk layout is unchanged.
# SESSIONS_DIR is the disk backend's default location, kept here for tooling that
# inspects session files (the live backend is _STORE).
SESSIONS_DIR = os.path.join(BASE_DIR, "sessions")
# Legacy single-conversation files, migrated into a session on first run.
HISTORY_PATH = os.path.join(BASE_DIR, "history.json")
SUMMARY_PATH = os.path.join(BASE_DIR, "history_summary.txt")

# Rolling-summary compaction: turns older than COMPACT_KEEP_RECENT are folded
# into SUMMARY instead of being dropped, so long histories keep earlier context.
COMPACT_KEEP_RECENT = 10
COMPACT_AT = 20
_MAX_TURNS = 200                 # cap per session on disk
# HARD bound on the rolling summary so repeated folds across many runs can never
# grow it (and thus the injected context) without limit — the summarizer is asked
# to stay concise, but this is the guaranteed backstop regardless of the LLM.
# Overridable per-agent via CONFIG["summary_max_chars"].
SUMMARY_MAX_CHARS = 4000

HISTORY: list = []
SUMMARY: str = ""
_SESSION = {"id": ""}            # active session id
# Serializes session-file mutations (write vs clear) — the run worker thread and
# the GUI thread can both touch session files. Reentrant: new/load call _write.
_IO_LOCK = threading.RLock()


def _new_session_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]


def _read_session(sid: str) -> dict:
    return _STORE.session_read(sid)


def _all_sessions() -> list:
    """Saved sessions as records, newest-updated first (via the storage backend)."""
    return _STORE.session_list()


def _write_active_id(sid: str) -> None:
    _STORE.active_set(sid)


def _session_title() -> str:
    for h in HISTORY:
        if h.get("role") == "user":
            t = " ".join(str(h.get("content", "")).split())
            return (t[:50] + "…") if len(t) > 50 else (t or "(new session)")
    return "(new session)"


def _write_session() -> None:
    """Persist the active session, capped to the last _MAX_TURNS. Empty sessions
    are NOT written. Locked so a concurrent clear can't be raced (clear empties
    HISTORY first, so a write that loses the race simply sees nothing to save)."""
    with _IO_LOCK:
        sid = _SESSION.get("id")
        if not sid:
            return
        del HISTORY[:-_MAX_TURNS]
        if not HISTORY and not SUMMARY:
            return
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        created = _read_session(sid).get("created", now)
        rec = {"id": sid, "title": _session_title(), "created": created,
               "updated": now, "history": HISTORY, "summary": SUMMARY}
        _STORE.session_write(sid, rec)
        _STORE.active_set(sid)


def _migrate_legacy() -> str:
    """Import a pre-sessions history.json/summary into one session. Returns the
    new id, or "" if there was nothing to migrate."""
    global SUMMARY
    try:
        with open(HISTORY_PATH, encoding="utf-8") as f:
            hist = json.load(f)
    except (OSError, json.JSONDecodeError):
        hist = []
    if not isinstance(hist, list) or not hist:
        return ""
    try:
        with open(SUMMARY_PATH, encoding="utf-8") as f:
            summ = f.read()
    except OSError:
        summ = ""
    HISTORY[:] = hist
    SUMMARY = summ
    _SESSION["id"] = _new_session_id()
    _write_session()
    for p in (HISTORY_PATH, SUMMARY_PATH):    # mark legacy migrated (run once)
        try:
            os.replace(p, p + ".migrated")
        except OSError:
            pass
    return _SESSION["id"]


def _load_active() -> None:
    """Load the active session into HISTORY/SUMMARY at import: the saved active id,
    else the most recent session, else a migrated legacy conversation, else fresh."""
    global SUMMARY
    sid = _STORE.active_get()
    if not (sid and _STORE.session_read(sid).get("id")):
        recents = _all_sessions()
        sid = recents[0]["id"] if recents else ""
    rec = _STORE.session_read(sid) if sid else {}
    if rec.get("id"):
        HISTORY[:] = rec.get("history") if isinstance(rec.get("history"), list) else []
        SUMMARY = rec.get("summary") or ""
        _SESSION["id"] = sid
        return
    if _migrate_legacy():
        return
    _SESSION["id"] = _new_session_id()    # fresh empty active (no file until 1st turn)


_load_active()


# ── per-session buffers for CONCURRENT runs (web / gateway) ─────────────────
# The module HISTORY/SUMMARY above are the DEFAULT (GUI / CLI / single active)
# conversation. When run() forks a per-session state (_rs().session_id set), each
# session's turns live in its OWN in-memory buffer here instead — so simultaneous
# users never share history. Buffers are an LRU (a plain dict is insertion-ordered;
# re-insert = move-to-end, pop(next(iter)) = evict oldest) capped by
# CONFIG["sessions_in_memory"]; an evicted buffer is still on disk (persist-all)
# and reloads on next touch. The store (_STORE) remains the source of truth.
_MEM: dict = {}
_MEM_LOCK = threading.RLock()


def _mem_cap() -> int:
    try:
        return max(1, int(CONFIG.get("sessions_in_memory", 200) or 200))
    except Exception:
        return 200


def _mem_buf(sid: str) -> dict:
    """The in-memory {'history','summary'} buffer for `sid` (loaded from the store
    on first touch), marked most-recently-used. LRU-evicts the oldest over the cap."""
    with _MEM_LOCK:
        rec = _MEM.pop(sid, None)
        if rec is None:
            stored = _read_session(sid)
            rec = {"history": list(stored.get("history") or []),
                   "summary": stored.get("summary") or ""}
        _MEM[sid] = rec                       # (re)insert at end = most-recent
        while len(_MEM) > _mem_cap():
            _MEM.pop(next(iter(_MEM)))         # evict least-recent (stays on disk)
        return rec


def _cur_buf():
    """The forked run's session buffer, or None for the default/GUI/CLI session
    (which uses the module HISTORY/SUMMARY globals — byte-identical to before)."""
    try:
        sid = _rs().session_id or ""
    except Exception:
        sid = ""
    return _mem_buf(sid) if sid else None


def _buf_title(hist) -> str:
    for h in hist:
        if h.get("role") == "user":
            t = " ".join(str(h.get("content", "")).split())
            return (t[:50] + "…") if len(t) > 50 else (t or "(new session)")
    return "(new session)"


def _persist_buf(sid: str, rec: dict) -> None:
    """Write a forked session's buffer to the store (capped, empty skipped)."""
    with _IO_LOCK:
        del rec["history"][:-_MAX_TURNS]
        if not rec["history"] and not rec["summary"]:
            return
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        created = _read_session(sid).get("created", now)
        _STORE.session_write(sid, {
            "id": sid, "title": _buf_title(rec["history"]), "created": created,
            "updated": now, "history": rec["history"], "summary": rec["summary"]})


def record_turn(task: str, result: str) -> None:
    """Append a completed user/assistant exchange to the current run's conversation
    (forked session buffer, or the default globals) and persist it."""
    b = _cur_buf()
    if b is not None:
        b["history"].append({"role": "user", "content": task})
        b["history"].append({"role": "assistant", "content": result})
        _persist_buf(_rs().session_id, b)
    else:
        HISTORY.append({"role": "user", "content": task})
        HISTORY.append({"role": "assistant", "content": result})
        _write_session()


def save_history() -> None:
    b = _cur_buf()
    if b is not None:
        _persist_buf(_rs().session_id, b)
    else:
        _write_session()


def save_summary() -> None:
    save_history()


def clear_history() -> None:
    """Wipe the ACTIVE session's content (keeps it active, now empty)."""
    global SUMMARY
    with _IO_LOCK:
        del HISTORY[:]
        SUMMARY = ""
        _STORE.session_delete(_SESSION.get("id") or "")          # empty -> prune


# ── session management (recent sessions / recover / new) ────────────────────
def current_session() -> str:
    return _SESSION.get("id", "")


def list_sessions() -> list:
    """Recent sessions, newest first: [{id,title,updated,turns,active}]. Includes
    the active session even before its first turn (so the UI shows it)."""
    out = []
    active = _SESSION.get("id", "")
    seen = set()
    for rec in _all_sessions():
        out.append({"id": rec["id"], "title": rec.get("title") or "(session)",
                    "updated": rec.get("updated", ""),
                    "turns": len(rec.get("history") or []),
                    "active": rec["id"] == active})
        seen.add(rec["id"])
    if active and active not in seen:        # active not yet persisted (empty)
        out.insert(0, {"id": active, "title": _session_title(),
                       "updated": "", "turns": len(HISTORY), "active": True})
    return out


def new_session() -> str:
    """Start a fresh session. The current one is already saved each turn, so
    nothing is lost; an empty current session leaves no file behind."""
    global SUMMARY
    with _IO_LOCK:
        _write_session()                      # persist current (no-op if empty)
        del HISTORY[:]
        SUMMARY = ""
        _SESSION["id"] = _new_session_id()
        _write_active_id(_SESSION["id"])
        return _SESSION["id"]


def load_session(sid: str) -> bool:
    """Recover a saved session as the active conversation. Returns False if absent."""
    global SUMMARY
    rec = _read_session(sid)
    if not rec.get("id"):
        return False
    with _IO_LOCK:
        _write_session()                      # persist the one we're leaving
        HISTORY[:] = rec.get("history") if isinstance(rec.get("history"), list) else []
        SUMMARY = rec.get("summary") or ""
        _SESSION["id"] = sid
        _write_active_id(sid)
    return True


def use_session(sid: str) -> None:
    """Bind the current run to a caller-supplied session/user id: make `sid` the
    active conversation, loading it from the store if it exists or starting it empty
    with that exact id if it doesn't. Unlike load_session (which refuses unknown
    ids), this is for per-connection / per-user isolation where the server/gateway
    coins the id. The session being left is persisted first. No-op for a blank id or
    if `sid` is already active — so run() without a session_id keeps today's single
    active-session behaviour (byte-identical).

    Phase 1 relies on the server's single-flight run lock, so exactly one session is
    active in-process at a time; concurrent per-session state is a Phase-2 change."""
    if not sid or sid == _SESSION.get("id"):
        return
    global SUMMARY
    rec = _read_session(sid)
    with _IO_LOCK:
        _write_session()                      # persist the one we're leaving
        if rec.get("id"):
            HISTORY[:] = rec.get("history") if isinstance(rec.get("history"), list) else []
            SUMMARY = rec.get("summary") or ""
        else:                                 # brand-new caller-coined session
            del HISTORY[:]
            SUMMARY = ""
        _SESSION["id"] = sid
        _write_active_id(sid)


def _summary_cap() -> int:
    """The rolling-summary char bound (CONFIG override, else the constant)."""
    try:
        return int(CONFIG.get("summary_max_chars", SUMMARY_MAX_CHARS)
                   or SUMMARY_MAX_CHARS)
    except Exception:                # CONFIG absent / bad value -> the safe default
        return SUMMARY_MAX_CHARS


def _cap_summary(text: str) -> str:
    """Hard-bound the rolling summary at a word boundary with a marker, so repeated
    folds across runs can NEVER grow it (or the injected context) without limit.
    The summarizer is asked to stay concise; this is the guaranteed backstop."""
    text = (text or "").strip()
    cap = _summary_cap()
    if len(text) <= cap:
        return text
    marker = " …[summary truncated to fit]"
    return text[:max(0, cap - len(marker))].rsplit(" ", 1)[0] + marker


def compact_history(summarize) -> None:
    """Fold turns older than COMPACT_KEEP_RECENT into SUMMARY via
    summarize(prior_summary, old_turns) -> str. Best-effort: on failure or an
    empty result, leave HISTORY unchanged (save_history's cap is the backstop).
    `summarize` is provided by the agent runtime (it owns the LLM). The merged
    summary is hard-capped (_cap_summary) so many folds can't grow it unbounded."""
    global SUMMARY
    b = _cur_buf()
    hist = b["history"] if b is not None else HISTORY
    summ = b["summary"] if b is not None else SUMMARY
    if len(hist) <= COMPACT_AT:
        return
    old = hist[:-COMPACT_KEEP_RECENT]
    try:
        new_summary = summarize(summ, old)
    except Exception:
        return
    if not new_summary:
        return
    if b is not None:
        b["summary"] = _cap_summary(new_summary)
        del b["history"][:-COMPACT_KEEP_RECENT]
    else:
        SUMMARY = _cap_summary(new_summary)
        del HISTORY[:-COMPACT_KEEP_RECENT]
    save_summary()
    save_history()


def history_context(max_chars: int = 4000) -> str:
    """Recent conversation (plus any rolling summary), formatted for inclusion
    ahead of a new task. Uses the forked run's session buffer when set, else the
    default (GUI/CLI) HISTORY/SUMMARY."""
    b = _cur_buf()
    hist = b["history"] if b is not None else HISTORY
    summ = b["summary"] if b is not None else SUMMARY
    if not hist and not summ:
        return ""
    parts = []
    if summ:
        # cap here too, so even a loaded/legacy oversized summary can't blow the
        # injected context (the recent-turns part below is already char-capped).
        parts.append("Summary of earlier conversation:\n" + _cap_summary(summ))
    if hist:
        lines = []
        for h in hist:
            who = "User" if h["role"] == "user" else "Agent"
            lines.append(f"{who}: {h['content']}")
        text = "\n\n".join(lines)
        if len(text) > max_chars:
            text = "...(earlier context trimmed)...\n" + text[-max_chars:]
        parts.append(text)
    return ("Conversation so far (context from previous runs):\n"
            + "\n\n".join(parts) + "\n\n---\nCurrent task:\n")
