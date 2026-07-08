"""Picture-book generator tools — a Canvas re-creation of the LangGraph
PictureBookGenerator, improved with PARALLEL page illustration.

A shared in-memory book (_BOOK) mirrors the LangGraph State: the author populates
the story + per-page specs + a single character-reference prompt; a worker pool then
generates every page's image IN PARALLEL (each page anchors to the one character-
reference image, never to the previous page, so the pages are independent); the
bookbinder assembles the PDF. Images come from SiliconFlow (Qwen-Image /
Qwen-Image-Edit); the PDF from reportlab.

Helpers live on the _PB class (indented `def`s) so the generator's top-level-`def`
tool scan never mistakes them for tools.
"""

from tool_registry import tool

import base64
import json
import os
import threading

_BOOK: dict = {}                 # shared book state (reset by start_book)
_LOCK = threading.RLock()        # guards _BOOK across parallel pool workers


class _PB:
    """Internal helpers (NOT tools — indented defs are invisible to the tool scan)."""
    SF_URL = "https://api.siliconflow.cn/v1/images/generations"

    @staticmethod
    def cfg() -> dict:
        try:
            p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    @staticmethod
    def api_key() -> str:
        """Resolve the SiliconFlow key. A MetaAgent-generated agent stores keys
        per-LLM under config['llms'][agent][i]['api_key'] (NOT a top-level key),
        so read from there — preferring a SiliconFlow endpoint — and fall back to
        a standalone top-level key or the usual env vars."""
        cfg = _PB.cfg()
        key = (cfg.get("deepseek_api_key") or cfg.get("api_key") or "").strip()
        if key:
            return key
        llms = cfg.get("llms") or {}
        entries = [c for cfgs in llms.values() for c in (cfgs or [])
                   if isinstance(c, dict) and (c.get("api_key") or "").strip()]
        # prefer a SiliconFlow LLM (same endpoint as the image API), else any key
        for c in entries:
            if "siliconflow" in (c.get("base_url") or "").lower():
                return c["api_key"].strip()
        if entries:
            return entries[0]["api_key"].strip()
        return (os.environ.get("SILICONFLOW_API_KEY")
                or os.environ.get("DEEPSEEK_API_KEY") or "").strip()

    @staticmethod
    def gen_image(prompt: str, out_path: str, source_path: str = "",
                  image_model: str = "Qwen/Qwen-Image",
                  edit_model: str = "Qwen/Qwen-Image-Edit",
                  image_size: str = "1024x1024") -> str:
        """Call the SiliconFlow image API (text-to-image, or edit when source_path is
        given) and download the PNG to out_path. Raises on failure. The model/size
        are passed in (snapshotted under _LOCK by the caller) so this touches no
        shared global while running on parallel pool threads."""
        import urllib.request
        key = _PB.api_key()
        if not key:
            raise RuntimeError(
                "No SiliconFlow API key found — set one on any LLM node in the "
                "designer (it is saved under config.json 'llms'), or set the "
                "SILICONFLOW_API_KEY environment variable.")
        if source_path:                       # edit mode (Qwen-Image-Edit)
            with open(source_path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
            payload = {"model": edit_model,
                       "prompt": prompt, "image": "data:image/png;base64," + b64,
                       "num_inference_steps": 20, "guidance_scale": 4}
        else:                                 # text-to-image (Qwen-Image)
            payload = {"model": image_model,
                       "prompt": prompt, "image_size": image_size,
                       "n": 1, "num_inference_steps": 20}
        req = urllib.request.Request(
            _PB.SF_URL, data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": "Bearer " + key,
                     "Content-Type": "application/json",
                     "User-Agent": "PictureBookCanvas/1.0"}, method="POST")
        with urllib.request.urlopen(req, timeout=180) as r:
            body = json.loads(r.read().decode("utf-8"))
        arr = body.get("images") or body.get("data") or []
        url = arr[0].get("url") if arr and isinstance(arr[0], dict) else None
        if not url:
            raise RuntimeError("No image URL in response: " + str(body)[:200])
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        with urllib.request.urlopen(url, timeout=120) as r, open(out_path, "wb") as f:
            f.write(r.read())
        return out_path

    # ── PDF rendering helpers (ported from the original pdf_tool.py) ───────────
    _fonts = None                      # cached (regular, bold) names after register

    @staticmethod
    def slug(title: str) -> str:
        """Filesystem-safe per-book folder name (keeps CJK; mirrors the original
        _topic_slug so each book's images/PDF live in their own subfolder)."""
        import re
        s = re.sub(r"[^\w一-鿿-]+", "_", (title or "book").strip())
        return (s.strip("_") or "book")[:40]

    @staticmethod
    def has_cjk(text: str) -> bool:
        for ch in text or "":
            cp = ord(ch)
            if (0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF
                    or 0xF900 <= cp <= 0xFAFF or 0x3000 <= cp <= 0x303F
                    or 0xFF00 <= cp <= 0xFFEF):
                return True
        return False

    @staticmethod
    def register_fonts():
        """Register a CJK-capable (preferred) or Latin TrueType font as Book/BookBold
        so non-Latin captions (e.g. Chinese) render instead of silent blank boxes.
        Falls back to the base-14 Helvetica if no TTF is found. Cached."""
        if _PB._fonts is not None:
            return _PB._fonts
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont

        def _try(name, path):
            if not os.path.isfile(path):
                return False
            for kw in ({"subfontIndex": 0}, {}):
                try:
                    pdfmetrics.registerFont(TTFont(name, path, **kw))
                    return True
                except Exception:
                    continue
            return False

        candidates = [
            (r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\msyhbd.ttc"),
            (r"C:\Windows\Fonts\simhei.ttf", r"C:\Windows\Fonts\simhei.ttf"),
            (r"C:\Windows\Fonts\simsun.ttc", r"C:\Windows\Fonts\simhei.ttf"),
            ("/System/Library/Fonts/PingFang.ttc", "/System/Library/Fonts/PingFang.ttc"),
            ("/System/Library/Fonts/Hiragino Sans GB.ttc",
             "/System/Library/Fonts/Hiragino Sans GB.ttc"),
            ("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
             "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"),
            ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
             "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"),
            (r"C:\Windows\Fonts\segoeui.ttf", r"C:\Windows\Fonts\segoeuib.ttf"),
            (r"C:\Windows\Fonts\arial.ttf", r"C:\Windows\Fonts\arialbd.ttf"),
            ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
             "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        ]
        result = ("Helvetica", "Helvetica-Bold")
        for reg, bold in candidates:
            if not _try("Book", reg):
                continue
            if not (bold != reg and _try("BookBold", bold)):
                _try("BookBold", reg)          # bold == regular fallback
            result = ("Book", "BookBold")
            break
        _PB._fonts = result
        return result

    @staticmethod
    def wrap_text(c, text, font, size, max_width, max_lines):
        """Wrap to fit max_width; char-level for CJK (no word spaces), word-level
        otherwise. Returns up to max_lines lines."""
        text = str(text or "")
        if _PB.has_cjk(text):
            lines, cur = [], ""
            for ch in text:
                if c.stringWidth(cur + ch, font, size) <= max_width:
                    cur += ch
                else:
                    if cur:
                        lines.append(cur)
                        if len(lines) >= max_lines:
                            return lines
                    cur = ch
            if cur and len(lines) < max_lines:
                lines.append(cur)
            return lines
        words, lines, line = text.split(), [], ""
        for w in words:
            test = (line + " " + w).strip()
            if c.stringWidth(test, font, size) <= max_width:
                line = test
            else:
                if line:
                    lines.append(line)
                    if len(lines) >= max_lines:
                        return lines
                line = w
        if line and len(lines) < max_lines:
            lines.append(line)
        return lines

    @staticmethod
    def draw_full_bleed(c, img_path, pw, ph):
        """Cover the whole page with the image (crop overflow) using PIL when
        available; otherwise fit it letterboxed via reportlab."""
        from reportlab.lib.utils import ImageReader
        img = ImageReader(img_path)
        try:
            from PIL import Image as _PILImage
            with _PILImage.open(img_path) as pil:
                iw, ih = pil.size
            scale = max(pw / iw, ph / ih)
            dw, dh = iw * scale, ih * scale
            c.drawImage(img, (pw - dw) / 2, (ph - dh) / 2, width=dw, height=dh,
                        mask="auto")
        except Exception:
            c.drawImage(img, 0, 0, width=pw, height=ph,
                        preserveAspectRatio=True, anchor="c", mask="auto")


# ── authoring (shared book state) ───────────────────────────────────────────────
@tool
def start_book(title: str, language: str = "English", output_dir: str = "output") -> str:
    """Start a NEW picture book, resetting any previous one. Call this first.

    Args:
        title: the book's title.
        language: language for the printed page sentences (e.g. English, Chinese).
        output_dir: base folder; this book's images and PDF go in a per-book
            subfolder under it (output_dir/<title-slug>/) so books never overwrite
            each other.
    """
    run_dir = os.path.join(output_dir, _PB.slug(title))
    with _LOCK:
        _BOOK.clear()
        _BOOK.update(title=title, language=language, output_dir=run_dir,
                     base_dir=output_dir, pages={},
                     main_character="", char_ref_prompt="", char_ref_path="",
                     image_model="Qwen/Qwen-Image", edit_model="Qwen/Qwen-Image-Edit",
                     image_size="1024x1024")
    return f"Started book '{title}' ({language}); output dir = {run_dir}"


@tool
def set_character(main_character: str, character_ref_prompt: str) -> str:
    """Record the main character and a character-reference-sheet prompt for the picture book. Must call start_book() first to initialize the book session; otherwise returns an error message.

    Args:
        main_character (str): Description of the main character.
        character_ref_prompt (str): Prompt to generate a character reference sheet, ensuring visual consistency across pages.

    Returns:
        str: "[ERROR] Call start_book first." if no book session is active; otherwise "Recorded main character + character-reference prompt."
    """
    with _LOCK:
        if not _BOOK:
            return "[ERROR] Call start_book first."
        _BOOK["main_character"] = main_character
        _BOOK["char_ref_prompt"] = character_ref_prompt
    return "Recorded main character + character-reference prompt."


@tool
def add_page(page: int, sentence: str, image_prompt: str,
             main_char_present: bool = True) -> str:
    """Add one page of the book. Must call start_book() before using this tool. If a page with the same number already exists, it will be overwritten. Returns a success message or '[ERROR] Call start_book first.' if start_book has not been called.

    Args:
        page: 1-based page number.
        sentence: the text printed on the page, in the book's language.
        image_prompt: an ENGLISH illustration description for this page's scene.
        main_char_present: true if the main character is the dominant subject (the page then anchors to the character-reference image for consistency).
    """
    with _LOCK:
        if not _BOOK:
            return "[ERROR] Call start_book first."
        _BOOK.setdefault("pages", {})[int(page)] = {
            "sentence": sentence, "image_prompt": image_prompt,
            "main_char_present": bool(main_char_present), "image_path": ""}
    return f"Added page {page}."


@tool
def generate_character_ref() -> str:
    """Generate a character reference image using the 'char_ref_prompt' from the global _BOOK configuration. Call this after set_character sets the prompt and before generating page images. Returns a string: a success message with the file path (e.g., 'Character reference image saved to ...'), an 'already exists' message with the existing path, or an error message starting with '[ERROR]'. Raises no exceptions. Note: not thread-safe for concurrent calls; duplicate racing calls may both attempt generation."""
    with _LOCK:
        prompt = _BOOK.get("char_ref_prompt", "")
        out_dir = _BOOK.get("output_dir", "output")
        existing = _BOOK.get("char_ref_path", "")
        image_model = _BOOK.get("image_model", "Qwen/Qwen-Image")
    if existing and os.path.isfile(existing):
        return f"Character reference image already exists at {existing}"
    if not prompt:
        return "[ERROR] No character_ref_prompt — call set_character first."
    out_path = os.path.join(out_dir, "character_ref.png")
    try:
        _PB.gen_image(prompt, out_path, image_model=image_model)
    except Exception as e:
        return f"[ERROR] character reference image failed: {e}"
    with _LOCK:
        _BOOK["char_ref_path"] = out_path
    return f"Character reference image saved to {out_path}"


# ── illustration (one page; the worker pool fans these out in parallel) ──────────
@tool
def generate_page_image(page: int) -> str:
    """Generate and save the illustration for ONE page. Returns an informative error string prefixed with '[ERROR]' on failure, or hangs on success (the success return is not implemented).

    Parameters:
        page (int): The page number (0-based, as used in add_page). The page must already exist via add_page() before calling this tool.

    Returns:
        str: On failure, returns an error string like '[ERROR] No page {page} — call add_page first.' or '[ERROR] page {page} needs the character reference — call generate_character_ref before illustrating main-character pages.' On success, the function hangs without returning (incomplete implementation).

    Prerequisites:
        - The page must exist (call add_page first).
        - For pages where main_char_present is True, generate_character_ref must have been called first and the reference image must exist.

    Notes:
        - This tool is not idempotent due to potential inconsistent state if _LOCK is not used consistently by other tools modifying _BOOK.
        - Pages are independent, so these calls can run in parallel across workers.
    """
    with _LOCK:
        spec = _BOOK.get("pages", {}).get(int(page))
        out_dir = _BOOK.get("output_dir", "output")
        char_ref = _BOOK.get("char_ref_path", "")
        main_char = _BOOK.get("main_character", "")
        image_model = _BOOK.get("image_model", "Qwen/Qwen-Image")
        edit_model = _BOOK.get("edit_model", "Qwen/Qwen-Image-Edit")
        image_size = _BOOK.get("image_size", "1024x1024")
    if not spec:
        return f"[ERROR] No page {page} — call add_page first."
    # A main-character page MUST anchor to the reference image; later-page prompts
    # deliberately omit the character's appearance, so without the reference the
    # hero would silently vanish. Fail loud so the agent generates the ref first.
    if spec.get("main_char_present") and not (char_ref and os.path.isfile(char_ref)):
        return ("[ERROR] page {0} needs the character reference — call "
                "generate_character_ref before illustrating main-character "
                "pages.".format(page))
    out_path = os.path.join(out_dir, f"page_{int(page):02d}.png")
    try:
        if spec.get("main_char_present"):
            wrapped = (f"The main character is {main_char}. Preserve ALL characters' "
                       "exact appearances — species, colours, clothing, body shape, "
                       "facial features — identically to the reference image. Only "
                       "change the background scene, setting, lighting and action. "
                       f"New scene: {spec['image_prompt']}")
            _PB.gen_image(wrapped, out_path, source_path=char_ref,
                          edit_model=edit_model)
        else:
            _PB.gen_image(spec["image_prompt"], out_path, image_model=image_model,
                          image_size=image_size)
    except Exception as e:
        return f"[ERROR] page {page} image failed: {e}"
    with _LOCK:
        _BOOK["pages"][int(page)]["image_path"] = out_path
    return f"Saved page {page} image to {out_path}"


# ── assembly ─────────────────────────────────────────────────────────────────────
@tool
def make_picture_book_pdf(filename: str = "picturebook.pdf") -> str:
    """Create a PDF with a cover page (gradient background, framed, wrapped title) using the global _BOOK dictionary (requires 'title', 'output_dir', and 'pages' keys).

    Parameters:
    filename (str, optional): Output PDF filename. Defaults to "picturebook.pdf". If it doesn't end with '.pdf', the extension is added.

    Returns:
    str: Path to the saved PDF on success, or an error message string if no pages exist or reportlab is missing.

    Call this after the book data is fully populated (title, pages generated, output directory set). This function depends on the global _PB object for font registration (must be available). Only the cover page is rendered; the pages themselves are not drawn (this implementation is limited to cover generation).
    """
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


@tool
def book_status() -> str:
    """Report the current book: title, language, total page count, number of illustrated pages, and whether the character reference exists."""
    with _LOCK:
        if not _BOOK:
            return "No book started yet."
        pages = _BOOK.get("pages", {})
        done = sum(1 for p in pages.values() if p.get("image_path"))
        return (f"'{_BOOK.get('title')}' ({_BOOK.get('language')}): {len(pages)} pages, "
                f"{done} illustrated, char-ref={'yes' if _BOOK.get('char_ref_path') else 'no'}.")
