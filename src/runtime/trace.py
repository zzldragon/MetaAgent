# ── structured trace (JSONL per run, Pillar 12) ─────────────────────────────
TRACES_DIR = os.path.join(BASE_DIR, "traces")

_TRACE = {"id": None, "path": None}   # the DEFAULT run's trace; forked runs carry
                                      # their own on _rs().trace (see _RunState)
_TRACE_LOCK = threading.Lock()  # keep concurrent pool-worker writes intact
_TRACE_SINK = {"fn": None}      # optional live listener (designer debug overlay)


def set_trace_sink(fn) -> None:
    """Register a callback invoked with every trace record live, in addition to
    the JSONL file (pass None to clear). The designer's debug overlay uses this
    to light up the graph as the run proceeds. The callback must be
    thread-safe — pool workers emit trace events from other threads."""
    _TRACE_SINK["fn"] = fn


def start_trace(task: str) -> str:
    os.makedirs(TRACES_DIR, exist_ok=True)
    trace_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    _tr = _rs().trace                     # per-run (forked session) or default
    _tr["id"] = trace_id
    _tr["path"] = os.path.join(TRACES_DIR, trace_id + ".jsonl")
    trace_event("run_start", task=task)
    return trace_id


def trace_event(kind: str, **fields) -> None:
    """Append one structured record to the current run's trace file, and hand
    it to the live sink if one is registered."""
    _tr = _rs().trace
    if not _tr["path"]:
        return
    record = {"ts": round(time.time(), 3), "trace_id": _tr["id"],
              "kind": kind}
    record.update(fields)
    sink = _TRACE_SINK["fn"]
    if sink is not None:
        try:
            sink(record)
        except Exception:
            pass  # a misbehaving overlay must never break the run
    try:
        line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
        with _TRACE_LOCK:
            with open(_tr["path"], "a", encoding="utf-8") as f:
                f.write(line)
    except OSError:
        pass  # tracing must never break the run
