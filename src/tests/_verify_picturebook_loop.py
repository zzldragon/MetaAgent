"""Verify the redesigned PictureBookAgent's shared-state loop coordination.

The redesign uses global shared state + a WHILE loop to guarantee every page is
illustrated before the PDF is composed:
  author (writes page_count, not_a_book) → condition(is_book)
     → while(pages_illustrated < page_count){ illustrator[pool] → checker } → bookbinder
     → (greeting) end
Asserts, with a stubbed LLM (no real image/PDF):
  * a book request LOOPS the illustrator→checker body until pages_illustrated ==
    page_count, THEN reaches the bookbinder (retry loop works),
  * a greeting sets not_a_book and short-circuits to end — illustrator/checker/
    bookbinder never run.
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
import graph_model  # noqa: E402

td = tempfile.mkdtemp()
g, _ = graph_model.load_mta("graphs/PictureBookAgent.mta", td)
info = graph_codegen.analyze(g)
assert not info["errors"], info["errors"]
names = {f["name"] for f in g.state_schema}
assert {"not_a_book", "page_count", "pages_illustrated"} <= names, names
print("ok 1: analyze clean; shared state has not_a_book / page_count / pages_illustrated")

out = graph_codegen.generate_from_graph(g, "verify_picbook_loop", gui=False)
spec = importlib.util.spec_from_file_location("pbl", os.path.join(out, "agent.py"))
m = importlib.util.module_from_spec(spec)
sys.path.insert(0, out); os.chdir(out)
spec.loader.exec_module(m)
os.chdir(BASE)

assert m.CONDITIONS["gate"] == [("pages_illustrated < page_count", "illustrator"),
                                (None, "bookbinder")], m.CONDITIONS["gate"]
print("ok 2: the while gate loops illustrator (body) and exits to bookbinder")

# role-scoped tools: the checker/illustrator must NOT have start_book, which resets
# the whole book (a rogue checker start_book was the real "round 2, no pages" bug).
assert "pages_report" in m.AGENTS["checker"]["tools"], m.AGENTS["checker"]["tools"]
assert "start_book" not in m.AGENTS["checker"]["tools"], "checker must NOT reset the book"
assert "start_book" not in m.AGENTS["illustrator"]["tools"], "illustrator must NOT reset the book"
assert set(t for t in m.AGENTS["author"]["tools"] if not t.startswith("_")) >= {"start_book"}
print("ok 2b: role-scoped tools — only the author has start_book (checker/illustrator can't wipe the book)")

# book request: loop illustrator→checker until complete, then bind
cnt = {"check": 0}


def stub(agent, cfg, system, messages):
    if agent == "author":
        if not any(x.get("role") == "tool" for x in messages):
            return "", [{"id": "a", "name": "set_state",
                         "args": {"not_a_book": False, "page_count": 2}}]
        return "[1, 2]", []
    if agent == "illustrator":
        return "done", []
    if agent == "checker":
        if not any(x.get("role") == "tool" for x in messages):
            cnt["check"] += 1
            val = 1 if cnt["check"] == 1 else 2      # 1st pass incomplete → must loop
            return "", [{"id": "c", "name": "set_state", "args": {"pages_illustrated": val}}]
        return "checked", []
    if agent == "bookbinder":
        return "PDF saved to output/picturebook.pdf", []
    return "?", []


m._call_one = stub
res = m.run("make a 2-page book about a brave kitten", emit=lambda s: None)
assert cnt["check"] == 2, "checker should run twice (retry until complete): %d" % cnt["check"]
assert "picturebook.pdf" in res, res
print("ok 3: book request loops until every page is illustrated, THEN binds the PDF")

# greeting: not_a_book → straight to end, no pool/checker/bookbinder
ran = set()


def stub2(agent, cfg, system, messages):
    ran.add(agent)
    if agent == "author":
        if not any(x.get("role") == "tool" for x in messages):
            return "", [{"id": "a", "name": "set_state", "args": {"not_a_book": True}}]
        return "Hi! Ask me to make a picture book.", []
    return "should-not-run", []


m._call_one = stub2
m.run("hello", emit=lambda s: None)
assert ran == {"author"}, "greeting must short-circuit; ran=%s" % ran
print("ok 4: a greeting short-circuits to end (illustrator/checker/bookbinder skipped)")

# per-page observability + targeted retry: pages 4,5 fail the first pass, are
# recorded in shared state as missing, then regenerated on the next loop.
import json as _json
import re as _re
assert {"illustrated_pages", "missing_pages"} <= {f["name"] for f in g.state_schema}
last = {}
m.set_trace_sink(lambda r: last.update(r.get("state") or {}) if r.get("kind") == "state" else None)
done3 = set(); il_pass = {"n": 0}


def stub3(agent, cfg, system, messages):
    if agent == "author":
        if not any(x.get("role") == "tool" for x in messages):
            return "", [{"id": "a", "name": "set_state", "args": {"not_a_book": False, "page_count": 5}}]
        return "built 5 pages", []
    if agent == "illustrator":
        # illustrate_missing_pages: 1st pass leaves 4,5 missing (429); 2nd finishes them
        il_pass["n"] += 1
        done3.update([1, 2, 3] if il_pass["n"] == 1 else [1, 2, 3, 4, 5])
        return "done", []
    if agent == "checker":
        if not any(x.get("role") == "tool" for x in messages):
            ill = sorted(done3); miss = [p for p in (1, 2, 3, 4, 5) if p not in done3]
            return "", [{"id": "c", "name": "set_state", "args": {
                "pages_illustrated": len(ill), "illustrated_pages": ill, "missing_pages": miss}}]
        return "checked", []
    if agent == "bookbinder":
        return "PDF saved to output/picturebook.pdf", []
    return "?", []


m._call_one = stub3
m.run("make a 5-page book about a kitten", emit=lambda s: None)
assert il_pass["n"] >= 2, "illustrator should re-run until complete: %d passes" % il_pass["n"]
assert sorted(done3) == [1, 2, 3, 4, 5], "retry should finish every page: %s" % sorted(done3)
assert last.get("missing_pages") == [], "missing_pages should be empty once complete: %s" % last
assert last.get("illustrated_pages") == [1, 2, 3, 4, 5], last.get("illustrated_pages")
print("ok 5: shared state tracks illustrated_pages / missing_pages; failed pages are "
      "regenerated on the next loop (targeted retry)")

# DETERMINISTIC counter (regression guard): the real bug was the checker LLM printing
# JSON that was never applied, so pages_illustrated stuck at 0 and the gate always
# looped to max_iterations. The fix forces the checker to call sync_page_status, which
# writes the TRUE count straight into shared state via the internal path (NOT a
# ```state block — that is blocked by the tool-output guardrail). Here: build a real
# 3-page book (page 3 fails the 1st render, recovers the 2nd) and assert the loop
# exits after 2 rounds — not max_iterations.
import re as _re2
fail = {"3": True}


def _fake_gen(pr, op, source_path="", **k):
    pg = int(_re2.search(r"page_(\d+)", op).group(1))
    if pg == 3 and fail["3"]:
        fail["3"] = False
        raise Exception("HTTP Error 429: Too Many Requests")
    os.makedirs(os.path.dirname(op), exist_ok=True)
    open(op, "wb").write(b"PNG")
    return op


il6 = []
seen6 = []
m.set_trace_sink(lambda r: seen6.append(dict(r.get("state") or {}))
                 if r.get("kind") == "state" else None)


def _has_tool(msgs):
    return any(x.get("role") == "tool" for x in msgs)


def stub6(agent, cfg, system, messages):
    if agent == "author":
        if not _has_tool(messages):
            tmp = tempfile.mkdtemp()
            m.start_book("T", "English", tmp)
            for p in (1, 2, 3):
                m.add_page(p, "s", "p %d" % p, False)
            m._PB.gen_image = staticmethod(_fake_gen)
            return "", [{"id": "a", "name": "set_state",
                         "args": {"not_a_book": False, "page_count": 3}}]
        return "[1,2,3]", []
    if agent == "illustrator":
        if not _has_tool(messages):
            il6.append(1)
            return "", [{"id": "i", "name": "illustrate_missing_pages", "args": {}}]
        return "done", []
    if agent == "checker":                    # forced to sync_page_status in real runs
        return "", [{"id": "c", "name": "sync_page_status", "args": {}}]
    if agent == "bookbinder":
        return "", [{"id": "b", "name": "make_picture_book_pdf", "args": {}}]
    return "?", []


m._call_one = stub6
res6 = m.run("make a 3-page book about a kitten", emit=lambda s: None)
_pi6 = [s.get("pages_illustrated") for s in seen6 if "pages_illustrated" in s]
assert len(il6) == 2, "loop must exit as soon as complete (2 rounds), got %d" % len(il6)
assert _pi6 and _pi6[-1] == 3, "sync_page_status must write the TRUE count (3): %s" % _pi6
assert "picturebook.pdf" in res6, res6
print("ok 6: sync_page_status writes the true counter into shared state deterministically "
      "(not a blocked ```state block) — the gate exits at 2 rounds, not max_iterations")

# ARG-FILTER guard (general runtime hardening): models sometimes hallucinate surplus
# kwargs on a no-arg tool — e.g. sync_page_status({"summary": ...}) — which used to
# raise "[ERROR] Bad arguments" and burn a whole loop iteration. The runtime must now
# DROP kwargs the tool can't accept before calling it.
def _sig_probe(a, b=1):
    return (a, b)


assert m._filter_tool_args(_sig_probe, {"a": 1, "summary": "x", "name": "y"}) == {"a": 1}, \
    "surplus kwargs must be dropped"
assert m._filter_tool_args(_sig_probe, {"a": 1, "b": 2}) == {"a": 1, "b": 2}, \
    "valid kwargs must be kept"
print("ok 7: runtime drops hallucinated surplus kwargs -> a no-arg tool called with "
      "extra kwargs (e.g. sync_page_status({'summary':...})) no longer fails the call")

# EMPTY-RESPONSE retry guard: a transient empty LLM reply (no text, no tool calls)
# must be retried, not accepted as "(empty response)". The real bug: the bookbinder
# flaked once and the whole run ended with NO pdf despite all pages being ready.
_ptmp = tempfile.mkdtemp()
m.start_book("T", "English", _ptmp)
for _p in (1, 2):
    m.add_page(_p, "s", "p %d" % _p, False)
m._PB.gen_image = staticmethod(
    lambda pr, op, source_path="", **k: (os.makedirs(os.path.dirname(op), exist_ok=True),
                                         open(op, "wb").write(b"PNG"), op)[-1])
m.illustrate_missing_pages()
_bb = {"n": 0}


def _stub_empty_then_pdf(agent, cfg, system, messages):
    if agent == "bookbinder":
        _bb["n"] += 1
        if _bb["n"] == 1:
            return "", []                       # transient empty flake
        return "", [{"id": "b", "name": "make_picture_book_pdf", "args": {}}]
    return "?", []


m._call_one = _stub_empty_then_pdf
_r8 = m.run_stage("bookbinder", "assemble", emit=lambda s: None)
assert _bb["n"] == 2, "empty response must be retried, got %d call(s)" % _bb["n"]
assert "(empty response)" not in _r8 and "Saved picture book" in _r8, _r8
print("ok 8: a transient EMPTY model reply is retried -> the bookbinder still makes the PDF")

shutil.rmtree(out, ignore_errors=True)
print("ALL PICTUREBOOK-LOOP CHECKS PASSED")
