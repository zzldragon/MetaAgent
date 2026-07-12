"""Picture-book illustrator tool — split so ONLY the illustrator agent gets it
(role-scoped: no start_book, which would reset the book). Shares the book state —
_BOOK / _LOCK / _PB are defined in picturebook_tools.py; both files are inlined into
the SAME generated agent module, so these names resolve to the one shared book.

ONE tool: illustrate_missing_pages() renders every page that lacks an image — a few
in parallel, idempotent (already-done pages are skipped), with the image API's 429
back-off handled inside _PB.gen_image. This replaces the old per-page tool + worker-
pool text-splitting, which mis-parsed the pool input and re-hammered done pages.
"""
from tool_registry import tool

import os


@tool
def illustrate_missing_pages(max_parallel: int = 3) -> str:
    """Generate the illustration for EVERY page that does not have one yet — pages
    already illustrated are SKIPPED. Renders a few pages in parallel and returns a
    report: which pages were illustrated this pass, how many are done in total, and
    which (if any) are still missing. Safe to call repeatedly (idempotent — only the
    missing pages are (re)generated). Call this ONCE to illustrate the whole book.

    Args:
        max_parallel: how many pages to render at once (1-4; default 3 — kept small
            so a free image endpoint is less likely to rate-limit).
    """
    from concurrent.futures import ThreadPoolExecutor
    _PB.ensure_loaded()                   # after a restart, reload the book from disk
    with _LOCK:
        book_pages = dict(_BOOK.get("pages", {}))
        out_dir = _BOOK.get("output_dir", "output")
        char_ref = _BOOK.get("char_ref_path", "")
        main_char = _BOOK.get("main_character", "")
        image_size = _BOOK.get("image_size", "1024x1024")
    if not book_pages:
        return "[ERROR] No pages yet — the author must build the book (add_page) first."
    pages = sorted(int(p) for p in book_pages)

    def _has_image(p):
        ip = book_pages[p].get("image_path")
        return bool(ip and os.path.isfile(ip)) or \
            os.path.isfile(os.path.join(out_dir, "page_%02d.png" % p))

    todo = [p for p in pages if not _has_image(p)]
    if not todo:
        return "All %d pages already illustrated." % len(pages)

    errors = {}

    def _one(p):
        spec = book_pages[p]
        out_path = os.path.join(out_dir, "page_%02d.png" % p)
        if spec.get("main_char_present") and not (char_ref and os.path.isfile(char_ref)):
            errors[p] = "needs the character reference (call generate_character_ref first)"
            return
        try:
            if spec.get("main_char_present"):
                wrapped = ("The main character is %s. Preserve ALL characters' exact "
                           "appearances — species, colours, clothing, body shape, facial "
                           "features — identically to the reference image. Only change the "
                           "background scene, setting, lighting and action. New scene: %s"
                           % (main_char, spec["image_prompt"]))
                _PB.gen_image(wrapped, out_path, source_path=char_ref, image_size=image_size)
            else:
                _PB.gen_image(spec["image_prompt"], out_path, image_size=image_size)
        except Exception as e:  # noqa: BLE001
            errors[p] = "%s: %s" % (type(e).__name__, e)
            return
        with _LOCK:
            _BOOK["pages"][p]["image_path"] = out_path

    n = max(1, min(int(max_parallel or 3), 4))
    with ThreadPoolExecutor(max_workers=n) as ex:
        list(ex.map(_one, todo))
    _PB.save_book()                       # persist which pages now have images

    with _LOCK:
        now_done = sorted(p for p in pages if _BOOK["pages"][p].get("image_path"))
    missing = [p for p in pages if p not in now_done]
    newly = [p for p in todo if p in now_done]
    msg = "Illustrated %d page(s) this pass: %s. Done %d/%d." % (
        len(newly), newly, len(now_done), len(pages))
    if missing:
        msg += " Still missing: %s" % missing
        if errors:
            msg += " (errors: %s)" % errors
    return msg
