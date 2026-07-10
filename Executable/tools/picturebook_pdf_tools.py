"""Picture-book PDF tool — split out of picturebook_tools.py so ONLY the bookbinder
agent gets it (role-scoped: the checker/illustrator/bookbinder must NOT have
start_book, which resets the whole book). Shares the book state — _BOOK / _LOCK
/ _PB are defined in picturebook_tools.py; both files are inlined into the SAME
generated agent module, so these names resolve to the one shared book."""
from tool_registry import tool

import os


@tool
def make_picture_book_pdf(filename: str = "picturebook.pdf") -> str:
    """Create a PDF with a cover page (gradient background, framed, wrapped title) using the global _BOOK dictionary (requires 'title', 'output_dir', and 'pages' keys).

    Parameters:
    filename (str, optional): Output PDF filename. Defaults to "picturebook.pdf". If it doesn't end with '.pdf', the extension is added.

    Returns:
    str: Path to the saved PDF on success, or an error message string if no pages exist or reportlab is missing.

    Call this after the book data is fully populated (title, pages generated, output directory set). This function depends on the global _PB object for font registration (must be available). Only the cover page is rendered; the pages themselves are not drawn (this implementation is limited to cover generation).
    """
    _PB.ensure_loaded()                   # after a restart, reload the book from disk
    with _LOCK:
        title = _BOOK.get("title", "Picture Book")
        out_dir = _BOOK.get("output_dir", "output")
        pages = dict(_BOOK.get("pages", {}))
    if not pages:
        return "[ERROR] No pages — generate the book first."
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.units import cm
        from reportlab.pdfgen import canvas as _canvas
    except Exception:
        return "[ERROR] reportlab is not installed (pip install reportlab Pillow)."
    font, font_bold = _PB.register_fonts()    # CJK-capable when available
    pw, ph = A4
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, filename if filename.endswith(".pdf") else filename + ".pdf")
    try:
        c = _canvas.Canvas(path, pagesize=A4)
        c.setTitle(title)
        # ── cover: warm gradient, framed, CJK-aware wrapped title ──
        steps = 40
        for i in range(steps):
            t = i / steps
            c.setFillColorRGB(1.0, (0xF0 + (0xC0 - 0xF0) * t) / 255,
                              (0xD0 + (0x80 - 0xD0) * t) / 255)
            c.rect(0, ph - (i + 1) * ph / steps, pw, ph / steps + 1, fill=1, stroke=0)
        for y_pos in (ph - 0.5 * cm, 0.5 * cm):
            c.setStrokeColor(colors.HexColor("#B8860B")); c.setLineWidth(2)
            c.line(1.5 * cm, y_pos, pw - 1.5 * cm, y_pos)
        c.setFillColor(colors.HexColor("#3D2B1F"))
        fs, max_w = 36, pw - 4 * cm
        c.setFont(font_bold, fs)
        tlines = _PB.wrap_text(c, title, font_bold, fs, max_w, max_lines=6)
        line_h = fs * 1.4
        start_y = ph / 2 + (len(tlines) * line_h) / 2 - line_h * 0.3
        for i, ln in enumerate(tlines):
            lw = c.stringWidth(ln, font_bold, fs)
            c.drawString((pw - lw) / 2, start_y - i * line_h, ln)
        subtitle = "绘本故事" if _PB.has_cjk(title) else "A Picture Book"
        c.setFont(font, 11); c.setFillColor(colors.HexColor("#7A5C3A"))
        sw = c.stringWidth(subtitle, font, 11)
        c.drawString((pw - sw) / 2, start_y - len(tlines) * line_h - 0.7 * cm, subtitle)
        c.showPage()
        # ── story pages, in order: full-bleed image + wrapped caption bar ──
        for n in sorted(pages):
            spec = pages[n]
            img = spec.get("image_path")
            if img and os.path.isfile(img):
                try:
                    _PB.draw_full_bleed(c, img, pw, ph)
                except Exception:
                    pass
            bar_h, pad_x = ph * 0.20, 1.5 * cm
            c.saveState()
            c.setFillColor(colors.Color(0, 0, 0, alpha=0.58))
            c.rect(0, 0, pw, bar_h, fill=1, stroke=0)
            c.restoreState()
            c.setStrokeColor(colors.HexColor("#C8A84B")); c.setLineWidth(1.2)
            c.line(pad_x, bar_h, pw - pad_x, bar_h)
            c.setFillColor(colors.white)
            size, line_h = 18, 26
            clines = _PB.wrap_text(c, spec.get("sentence", ""), font_bold, size,
                                   pw - 2 * pad_x, max_lines=3)
            c.setFont(font_bold, size)
            top = bar_h - 1.1 * cm - size
            for i, ln in enumerate(clines):
                lw = c.stringWidth(ln, font_bold, size)
                c.drawString((pw - lw) / 2, top - i * line_h, ln)
            c.setFont(font, 9); c.setFillColor(colors.HexColor("#BBBBBB"))
            c.drawRightString(pw - pad_x, 0.4 * cm, str(n))
            c.showPage()
        c.save()
    except Exception as e:
        return f"[ERROR] PDF assembly failed: {e}"
    return f"Saved picture book to {path} ({len(pages)} pages)."
