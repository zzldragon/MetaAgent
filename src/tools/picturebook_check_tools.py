"""Picture-book checker tool — split out of picturebook_tools.py so ONLY the checker
agent gets it (role-scoped: the checker/illustrator/bookbinder must NOT have
start_book, which resets the whole book). Shares the book state — _BOOK / _LOCK
/ _PB are defined in picturebook_tools.py; both files are inlined into the SAME
generated agent module, so these names resolve to the one shared book."""
from tool_registry import tool

import os


@tool
def sync_page_status() -> str:
    """Record the TRUE illustration progress into shared state so the while-loop
    knows when every page is done. Reads the real book state, counts which page
    numbers already have a finished image, and writes pages_illustrated /
    illustrated_pages / missing_pages directly into shared state. Call this EXACTLY
    once after an illustration round — it does the bookkeeping itself; you only
    relay the summary it returns.
    """
    done, missing, total = _PB.write_progress()
    return ("Progress: %d/%d pages illustrated. illustrated=%s missing=%s"
            % (len(done), total, done, missing))


@tool
def pages_report() -> str:
    """Per-page illustration status: which page numbers already have a finished image
    and which are still MISSING (read-only, does NOT write shared state — use
    sync_page_status for that). Returns a line like:
    'total=12 illustrated=[1, 2, 3] missing=[4, 5]'."""
    with _LOCK:
        pages = _BOOK.get("pages", {})
        done = sorted(int(p) for p, spec in pages.items() if spec.get("image_path"))
        missing = sorted(int(p) for p in pages if int(p) not in done)
    return "total=%d illustrated=%s missing=%s" % (len(pages), done, missing)
