"""Picture-book generator tools — a Canvas re-creation of the LangGraph
PictureBookGenerator, improved with PARALLEL page illustration.

A shared in-memory book (_BOOK) mirrors the LangGraph State: the author populates
the story + per-page specs + a single character-reference prompt; a worker pool then
generates every page's image IN PARALLEL (each page anchors to the one character-
reference image, never to the previous page, so the pages are independent); the
bookbinder assembles the PDF. Images come from SiliconFlow's FREE Kwai-Kolors/Kolors model (text-to-image and
image-to-image from a reference image); the PDF from reportlab.

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

    # ── platform profiles: switch the whole stack between SiliconFlow and NVIDIA ──
    # The GUI writes config['platform'] + per-platform overrides/keys under
    # config['platforms']; these are the built-in defaults (user-overridable).
    #   SiliconFlow -> DeepSeek-V4-Flash (chat) + Kwai-Kolors/Kolors (images)
    #   NVIDIA      -> llama-3.1-70b (chat) + SiliconFlow Kolors (images; NVIDIA has no image API)
    # `provider` only distinguishes Anthropic from OpenAI-compatible at runtime; both
    # of these are OpenAI-compatible, but it's set correctly so the config isn't
    # misleading (base_url + model + api_key are what actually drive the call).
    _PLATFORM_DEFAULTS = {
        "siliconflow": {
            "label": "SiliconFlow",
            "provider": "siliconflow",
            "chat_base_url": "https://api.siliconflow.cn/v1",
            "chat_model": "deepseek-ai/DeepSeek-V4-Flash",
            "image_url": "https://api.siliconflow.cn/v1/images/generations",
            "image_model": "Kwai-Kolors/Kolors",
            "edit_model": "Kwai-Kolors/Kolors",
        },
        "nvidia": {
            "label": "NVIDIA",
            "provider": "nvidia",
            "chat_base_url": "https://integrate.api.nvidia.com/v1",
            # llama-3.1-70b is much faster than deepseek-v4-pro on NVIDIA's endpoint.
            "chat_model": "deepseek-ai/deepseek-v4-flash",
            # NVIDIA's API is LLM-only (no image endpoint), so images are generated on
            # SiliconFlow with the FREE Kwai-Kolors/Kolors model (t2i + i2i). That needs
            # a SiliconFlow key: image_api_key here, else the siliconflow profile's key.
            "image_url": "https://api.siliconflow.cn/v1/images/generations",
            "image_model": "Kwai-Kolors/Kolors",
            "edit_model": "Kwai-Kolors/Kolors",
            "image_api_key": "",
        },
    }

    @staticmethod
    def cfg() -> dict:
        try:
            p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    @staticmethod
    def platform_defaults() -> dict:
        """Built-in profiles (fresh copy) — the GUI reads these to seed config."""
        return {k: dict(v) for k, v in _PB._PLATFORM_DEFAULTS.items()}

    @staticmethod
    def active_platform() -> str:
        p = (_PB.cfg().get("platform") or "siliconflow").strip().lower()
        return p if p in _PB._PLATFORM_DEFAULTS else "siliconflow"

    @staticmethod
    def profile(platform: str = "") -> dict:
        """The active (or named) platform profile = built-in defaults + any user
        override saved under config['platforms'][platform]."""
        p = (platform or _PB.active_platform())
        prof = dict(_PB._PLATFORM_DEFAULTS.get(p, _PB._PLATFORM_DEFAULTS["siliconflow"]))
        prof.update((_PB.cfg().get("platforms") or {}).get(p, {}) or {})
        return prof

    @staticmethod
    def api_key() -> str:
        """Resolve the API key for the ACTIVE platform: the per-platform key the GUI
        saved (config['platforms'][platform]['api_key']) wins; then a top-level key /
        any configured LLM key / the platform's env var."""
        cfg = _PB.cfg()
        p = _PB.active_platform()
        k = (((cfg.get("platforms") or {}).get(p) or {}).get("api_key") or "").strip()
        if k:
            return k
        k = (cfg.get("deepseek_api_key") or cfg.get("api_key") or "").strip()
        if k:
            return k
        entries = [c for cfgs in (cfg.get("llms") or {}).values() for c in (cfgs or [])
                   if isinstance(c, dict) and (c.get("api_key") or "").strip()]
        if entries:
            return entries[0]["api_key"].strip()
        env = "NVIDIA_API_KEY" if p == "nvidia" else "SILICONFLOW_API_KEY"
        return (os.environ.get(env) or os.environ.get("DEEPSEEK_API_KEY") or "").strip()

    @staticmethod
    def image_conf():
        """Image settings for the active platform: (url, image_model, edit_model, key).
        The image provider can DIFFER from the chat provider (e.g. NVIDIA chat but
        SiliconFlow Kolors) — so the key is resolved for the IMAGE endpoint: an
        explicit `image_api_key`, else the key of whichever platform hosts the image
        URL, else the active platform's key."""
        cfg = _PB.cfg()
        prof = _PB.profile()
        url = prof.get("image_url", "")
        key = (prof.get("image_api_key") or "").strip()
        if not key:
            host = (url or "").lower()
            plats = cfg.get("platforms") or {}
            for pk, d in _PB._PLATFORM_DEFAULTS.items():
                base = (d.get("image_url") or "").split("/v1")[0].lower()
                if base and base in host:
                    key = ((plats.get(pk) or {}).get("api_key") or "").strip()
                    if key:
                        break
        if not key:
            key = _PB.api_key()
        return url, prof.get("image_model", ""), prof.get("edit_model", ""), key

    @staticmethod
    def _proxy() -> str:
        """Proxy for image/tool HTTP calls: the active platform's proxy override, else
        the top-level config proxy, else '' (blank = honor env / direct). The chat
        LLMs already route through config['proxy']; this brings the image tool inline."""
        cfg = _PB.cfg()
        p = _PB.active_platform()
        return ((((cfg.get("platforms") or {}).get(p) or {}).get("proxy")
                 or cfg.get("proxy") or "")).strip()

    @staticmethod
    def _opener():
        """A urllib opener wired with the configured proxy (if any). A blank proxy
        keeps urllib's default (which honors HTTP(S)_PROXY env vars)."""
        import urllib.request
        px = _PB._proxy()
        if px:
            return urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": px, "https": px}))
        return urllib.request.build_opener()

    @staticmethod
    def _img_from_response(body, out_path) -> str:
        """Write the generated image from an API response — either a URL to download
        or inline base64 — covering the common shapes across providers."""
        # inline base64: OpenAI-images (data[].b64_json), NVIDIA/SD (artifacts[].base64),
        # or a bare field.
        arr = body.get("data") or body.get("images") or body.get("artifacts") or []
        b64 = None
        if arr and isinstance(arr[0], dict):
            b64 = arr[0].get("b64_json") or arr[0].get("base64") or arr[0].get("b64")
        b64 = b64 or body.get("b64_json") or body.get("image") or body.get("artifact")
        if b64:
            with open(out_path, "wb") as f:
                f.write(base64.b64decode(b64))
            return out_path
        url = None
        if arr and isinstance(arr[0], dict):
            url = arr[0].get("url")
        url = url or body.get("url")
        if not url:
            raise RuntimeError("No image in response: " + str(body)[:200])
        with _PB._opener().open(url, timeout=120) as r, open(out_path, "wb") as f:
            f.write(r.read())
        return out_path

    @staticmethod
    def gen_image(prompt: str, out_path: str, source_path: str = "",
                  image_model: str = "", edit_model: str = "",
                  image_size: str = "1024x1024") -> str:
        """Generate an image on the ACTIVE platform and save it to out_path. Text-to-
        image (no source_path) or image-to-image (source_path = a reference image, for
        character consistency). The endpoint / model / key come from the platform
        profile (SiliconFlow Kolors for both platforms; NVIDIA has no image API); model
        args override the profile if given. Raises on failure."""
        import time
        import urllib.error
        import urllib.request
        url, img_model, edit_m, key = _PB.image_conf()
        if not key:
            raise RuntimeError(
                "No image API key — open the maker's 'Set API Key' and enter the key "
                "for the image provider (%s)." % (url or "?"))
        model = (edit_model or edit_m) if source_path else (image_model or img_model)
        # Choose the request shape by the IMAGE endpoint (which may be a different
        # provider than the chat LLM): SiliconFlow uses native fields; anything else
        # gets the OpenAI-images shape (base64 back).
        if "siliconflow" in (url or "").lower():
            payload = {"model": model, "prompt": prompt, "image_size": image_size,
                       "batch_size": 1, "num_inference_steps": 20, "guidance_scale": 7.5}
        else:
            payload = {"model": model, "prompt": prompt, "size": image_size,
                       "n": 1, "response_format": "b64_json"}
        if source_path:                        # image-to-image: base it on the old image
            with open(source_path, "rb") as f:
                payload["image"] = "data:image/png;base64," + base64.b64encode(f.read()).decode()
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": "Bearer " + key,
                     "Content-Type": "application/json", "Accept": "application/json",
                     "User-Agent": "PictureBookCanvas/1.0"}, method="POST")
        # Retry with exponential back-off on rate-limit / transient server errors.
        opener = _PB._opener()
        body = None
        for attempt in range(5):
            try:
                with opener.open(req, timeout=180) as r:
                    body = json.loads(r.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 502, 503) and attempt < 4:
                    time.sleep(4 * (attempt + 1))   # 4s, 8s, 12s, 16s
                    continue
                raise
            except urllib.error.URLError:
                if attempt < 4:
                    time.sleep(4 * (attempt + 1))
                    continue
                raise
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        return _PB._img_from_response(body, out_path)

    # ── durable book state (only when checkpoint/resume is enabled) ────────────
    # The graph checkpoint restores the SHARED STATE (page_count, counters) but not
    # _BOOK (the story text, per-page prompts, char-ref path) — those live here in
    # the tool module. To recover a book after the process restarts, persist _BOOK
    # to disk and reload it. Gated on config['checkpoint'] so a non-resumable app
    # does ZERO extra I/O and behaves byte-identically.
    _STATE_FILE = "_active_book.json"

    @staticmethod
    def persist_on() -> bool:
        return bool(_PB.cfg().get("checkpoint"))

    @staticmethod
    def save_book():
        """Snapshot _BOOK to disk (no-op unless checkpoint/resume is on)."""
        if not _PB.persist_on():
            return
        try:
            with _LOCK:
                snap = json.loads(json.dumps(_BOOK))   # plain, JSON-safe copy
            with open(_PB._STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(snap, f, ensure_ascii=False)
        except Exception:  # noqa: BLE001 — persistence must never break a run
            pass

    @staticmethod
    def ensure_loaded():
        """If _BOOK is empty (e.g. after a restart) and a persisted book exists,
        reload it so a resumed illustrator/bookbinder can rebuild the book. No-op
        unless checkpoint/resume is on and there's something to load."""
        if not _PB.persist_on():
            return
        with _LOCK:
            if _BOOK:
                return
        try:
            with open(_PB._STATE_FILE, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        pages = {int(k): v for k, v in (data.get("pages") or {}).items()}
        data["pages"] = pages
        with _LOCK:
            _BOOK.clear()
            _BOOK.update(data)

    @staticmethod
    def write_progress():
        """Compute the TRUE progress from the real book and write pages_illustrated /
        illustrated_pages / missing_pages straight into shared state via the internal
        path (same as the built-in set_state tool — NOT a ```state block, which the
        tool-output guardrail blocks). A page counts as done once it has an image
        file. Returns (done, missing, total). _rs / _STATE_LOCK / _apply_state resolve
        from the inlined generated-agent module globals; guarded so a standalone
        import still works."""
        _PB.ensure_loaded()
        with _LOCK:
            pages = _BOOK.get("pages", {})
            done = sorted(int(p) for p, s in pages.items() if s.get("image_path"))
            total = len(pages)
        missing = sorted(int(p) for p in pages if int(p) not in done)
        updates = {"pages_illustrated": len(done),
                   "illustrated_pages": done, "missing_pages": missing}
        try:
            st = _rs().rec.get("state")
            if st is not None:
                with _STATE_LOCK:
                    applied = _apply_state(st, updates)
                    if applied:
                        _rs().rec.setdefault("written", set()).update(applied.keys())
        except NameError:
            pass
        return done, missing, total

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
                     image_model="Kwai-Kolors/Kolors", edit_model="Kwai-Kolors/Kolors",
                     image_size="1024x1024")
    _PB.save_book()                       # reset the persisted snapshot for a fresh book
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
    _PB.save_book()
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
    _PB.save_book()
    return f"Added page {page}."


@tool
def generate_character_ref() -> str:
    """Generate a character reference image using the 'char_ref_prompt' from the global _BOOK configuration. Call this after set_character sets the prompt and before generating page images. Returns a string: a success message with the file path (e.g., 'Character reference image saved to ...'), an 'already exists' message with the existing path, or an error message starting with '[ERROR]'. Raises no exceptions. Note: not thread-safe for concurrent calls; duplicate racing calls may both attempt generation."""
    with _LOCK:
        prompt = _BOOK.get("char_ref_prompt", "")
        out_dir = _BOOK.get("output_dir", "output")
        existing = _BOOK.get("char_ref_path", "")
    if existing and os.path.isfile(existing):
        return f"Character reference image already exists at {existing}"
    if not prompt:
        return "[ERROR] No character_ref_prompt — call set_character first."
    out_path = os.path.join(out_dir, "character_ref.png")
    try:
        _PB.gen_image(prompt, out_path)       # model comes from the active platform
    except Exception as e:
        return f"[ERROR] character reference image failed: {e}"
    with _LOCK:
        _BOOK["char_ref_path"] = out_path
    _PB.save_book()
    return f"Character reference image saved to {out_path}"


# ── illustration (one page; the worker pool fans these out in parallel) ──────────


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


