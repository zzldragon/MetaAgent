# ── image input (vision): provider-neutral attachments ──────────────────────
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")
_IMG_MEDIA = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
              ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp"}


def _img_limits():
    """(max_bytes, allowed_exts) from CONFIG["guardrails"] — file size/type
    guardrail for attachments. max_bytes 0 = no limit; allowed_exts [] = all."""
    cfg = CONFIG.get("guardrails", {}) if isinstance(CONFIG.get("guardrails"), dict) else {}
    try:
        mb = float(cfg.get("file_max_mb", 10) or 0)
    except (TypeError, ValueError):
        mb = 10
    return (int(mb * 1024 * 1024) if mb > 0 else 0,
            [str(e).lower() for e in (cfg.get("allowed_image_types") or [])])


def encode_image(path: str) -> dict:
    """Read an image file into a provider-neutral content part:
    {"type": "image", "media_type": ..., "data": <base64>}."""
    import base64
    ext = os.path.splitext(path)[1].lower()
    media = _IMG_MEDIA.get(ext)
    if not media:
        raise ValueError(f"unsupported image type: {ext or path}")
    _maxb, _allowed = _img_limits()
    if _allowed and ext not in _allowed:
        raise ValueError(f"image type not allowed by guardrails: {ext}")
    if _maxb and os.path.getsize(path) > _maxb:
        raise ValueError(f"image too large (> {_maxb // (1024 * 1024)} MB guardrail limit)")
    with open(path, "rb") as f:
        data = base64.b64encode(f.read()).decode("ascii")
    return {"type": "image", "media_type": media, "data": data}


def decode_data_url(url: str) -> dict:
    """Parse a browser 'data:image/png;base64,...' URL into a neutral part."""
    m = re.match(r"data:(image/[\w.+-]+);base64,(.*)$", url or "", re.DOTALL)
    if not m:
        raise ValueError("not a base64 image data URL")
    _maxb, _ = _img_limits()
    if _maxb and (len(m.group(2)) * 3) // 4 > _maxb:
        raise ValueError("image too large — guardrail size limit")
    return {"type": "image", "media_type": m.group(1), "data": m.group(2)}


_RUN_IMAGES = []                       # default run's staged parts (see _rs().images)
_RUN_IMAGES_LOCK = threading.Lock()


def set_run_image_parts(parts) -> None:
    """Stage already-encoded image parts for the current run (web UI path). Per-run:
    resolves the caller's run state so concurrent sessions don't share attachments."""
    with _RUN_IMAGES_LOCK:
        _rs().images[:] = list(parts or [])


def set_run_images(paths) -> int:
    """Stage image FILES for the next run (desktop path); bad files skipped.
    Returns how many encoded successfully."""
    parts = []
    for p in paths or []:
        try:
            parts.append(encode_image(p))
        except (OSError, ValueError):
            pass
    set_run_image_parts(parts)
    return len(parts)


def _take_run_images() -> list:
    """Consume the staged images (once) — the first agent to ask gets them."""
    with _RUN_IMAGES_LOCK:
        imgs = list(_rs().images)
        _rs().images.clear()
        return imgs


def _with_images(text: str, images):
    """A user-message content: plain text, or a multimodal [text, *images]."""
    if not images:
        return text
    return [{"type": "text", "text": text}] + list(images)
