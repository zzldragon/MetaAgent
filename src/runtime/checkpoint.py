# ── graph-mode checkpoint / resume (per thread_id; JSON; opt-in) ─────────────
# Snapshots run_graph's single-cursor loop locals at each stage boundary so a run
# can resume after a Stop or crash. State is JSON-native (per STATE_SCHEMA), so
# no pickle — checkpoints don't couple to a module version. Persistence goes
# through _STORE (storage.py): disk (checkpoints/<tid>.json), sqlite, or postgres.
def save_checkpoint(tid: str, snap: dict) -> None:
    _STORE.ckpt_write(tid, snap)


def load_checkpoint(tid: str):
    """The saved snapshot dict, or None (missing/torn → resume from scratch)."""
    return _STORE.ckpt_read(tid)


def clear_checkpoint(tid: str) -> None:
    _STORE.ckpt_delete(tid)
