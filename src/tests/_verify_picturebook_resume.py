"""Verify the RESUMABLE PictureBook variant recovers a book that failed mid-run.

graphs/PictureBookAgentResumable.mta = the base picturebook + graph-mode
checkpointing (storage.checkpoint=True) + a Resume GUI. This proves the payoff:
  * the generated config turns checkpoint ON (the plain PictureBookAgent leaves it OFF),
  * a crash during illustration leaves a checkpoint (done=False) + a persisted _BOOK,
  * after the in-memory book is lost (a simulated process restart), resume(thread_id)
    reloads _BOOK from disk, the idempotent tools finish the missing pages, and the
    bookbinder produces the PDF.
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.chdir(BASE)

import graph_codegen  # noqa: E402
import graph_model  # noqa: E402

RES = os.path.join(BASE, "graphs", "PictureBookAgentResumable.mta")
PLAIN = os.path.join(BASE, "graphs", "PictureBookAgent.mta")

# 1. the resumable graph opts into checkpointing; the plain one does not
gr, _ = graph_model.load_mta(RES, tempfile.mkdtemp())
assert (getattr(gr, "storage", {}) or {}).get("checkpoint") is True, "resumable must set storage.checkpoint"
gp, _ = graph_model.load_mta(PLAIN, tempfile.mkdtemp())
assert not (getattr(gp, "storage", {}) or {}).get("checkpoint"), "plain must NOT checkpoint"
print("ok 1: resumable .mta enables checkpoint; plain .mta leaves it off")

out = graph_codegen.generate_from_graph(gr, "verify_pb_resume", gui=True)
cfg = json.load(open(os.path.join(out, "config.json"), encoding="utf-8"))
assert cfg.get("checkpoint") is True, "generated config must carry checkpoint=True"
gui = open(os.path.join(out, "gui.py"), encoding="utf-8").read()
assert "Resume last book" in gui and "picturebook-main" in gui, "GUI must expose Resume + a thread_id"
print("ok 2: generated config checkpoint=True; GUI has Resume + a stable thread_id")

spec = importlib.util.spec_from_file_location("vpbr", os.path.join(out, "agent.py"))
m = importlib.util.module_from_spec(spec)
sys.path.insert(0, out)
os.chdir(out)                      # config.json (checkpoint) + _active_book.json live here
spec.loader.exec_module(m)
assert m.CHECKPOINT_ENABLED, "runtime CHECKPOINT_ENABLED must be true"


def _fake_gen(pr, op, source_path="", **k):
    os.makedirs(os.path.dirname(op), exist_ok=True)
    open(op, "wb").write(b"PNG")
    return op


m._PB.gen_image = staticmethod(_fake_gen)


def _has_tool(msgs):
    return any(x.get("role") == "tool" for x in msgs)


# RUN 1: the author builds the whole book, then the illustrator stage crashes
def _stub_crash(agent, cfg, system, messages):
    if agent == "author":
        if not _has_tool(messages):
            return "", [
                {"id": "s", "name": "start_book",
                 "args": {"title": "Kitten Picnic", "language": "English", "output_dir": "./books"}},
                {"id": "p1", "name": "add_page",
                 "args": {"page": 1, "sentence": "one", "image_prompt": "a", "main_char_present": False}},
                {"id": "p2", "name": "add_page",
                 "args": {"page": 2, "sentence": "two", "image_prompt": "b", "main_char_present": False}},
                {"id": "p3", "name": "add_page",
                 "args": {"page": 3, "sentence": "three", "image_prompt": "c", "main_char_present": False}},
                {"id": "ss", "name": "set_state", "args": {"not_a_book": False, "page_count": 3}}]
        return "[1,2,3]", []
    if agent == "illustrator":
        raise RuntimeError("simulated crash during illustration")
    return "?", []


m._call_one = _stub_crash
try:
    m.run("make a 3-page book about a kitten", thread_id="picturebook-main", emit=lambda s: None)
except Exception:
    pass
snap = m.load_checkpoint("picturebook-main")
assert snap and not snap.get("done"), "a crash must leave an unfinished checkpoint"
assert (snap.get("state") or {}).get("page_count") == 3, "checkpoint must carry the shared state"
assert os.path.isfile("_active_book.json"), "the book (story/prompts) must be persisted to disk"
print("ok 3: crash leaves an unfinished checkpoint + a persisted book on disk")

# SIMULATE A RESTART: the in-memory book is gone; only the disk state survives
m._BOOK.clear()
assert not m._BOOK


def _stub_ok(agent, cfg, system, messages):
    if agent == "illustrator":
        if not _has_tool(messages):
            return "", [{"id": "i", "name": "illustrate_missing_pages", "args": {}}]
        return "done", []
    if agent == "checker":
        return "", [{"id": "c", "name": "sync_page_status", "args": {}}]
    if agent == "bookbinder":
        return "", [{"id": "b", "name": "make_picture_book_pdf", "args": {}}]
    return "[1,2,3]", []


m._call_one = _stub_ok
res = m.resume("picturebook-main", emit=lambda s: None)
assert m._BOOK, "resume must reload _BOOK from disk (ensure_loaded)"
assert "Saved picture book" in (res or ""), res
assert m.load_checkpoint("picturebook-main") in (None, {}) or \
    (m.load_checkpoint("picturebook-main") or {}).get("done"), "checkpoint cleared on completion"
os.chdir(BASE)
print("ok 4: after a simulated restart, resume() rebuilds the book from disk and makes the PDF")

shutil.rmtree(out, ignore_errors=True)
print("ALL PICTUREBOOK-RESUME CHECKS PASSED")
