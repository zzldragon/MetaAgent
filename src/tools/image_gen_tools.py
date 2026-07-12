"""Free image generation for the WeChat content factory — SiliconFlow → Kolors
(text-to-image). Guarantees every article has REAL local image files (cover +
illustrations) without needing stock-photo APIs or manual screenshots, and keeps
the whole pipeline 0-cost (same SiliconFlow account as the text LLM).

Saves generated images to ``./article_images/`` (override with ``ARTICLE_IMAGE_DIR``)
so the Writer can embed the exact local path and the Publisher can upload it.

Credentials / model (env, NEVER in the graph):
  * ``SILICONFLOW_API_KEY``      — your SiliconFlow key (the same sk-... you use for LLM)
  * ``SILICONFLOW_BASE_URL``     — default https://api.siliconflow.cn/v1
  * ``SILICONFLOW_IMAGE_MODEL``  — default Kwai-Kolors/Kolors

Conventions: ``from tool_registry import tool``; every top-level ``def`` is a tool,
so all helpers are LAMBDAS. Tools return strings and never raise. Uses ``requests``.
"""
from tool_registry import tool

import os
import time
import requests

_imgdir = lambda: os.environ.get("ARTICLE_IMAGE_DIR",
                                 os.path.join(os.getcwd(), "article_images"))
_key = lambda: os.environ.get("SILICONFLOW_API_KEY", "").strip()
_base = lambda: os.environ.get("SILICONFLOW_BASE_URL",
                               "https://api.siliconflow.cn/v1").rstrip("/")
_model = lambda: os.environ.get("SILICONFLOW_IMAGE_MODEL", "Kwai-Kolors/Kolors")
_pick_url = lambda j: (
    (j.get("images") or [{}])[0].get("url")
    or (j.get("data") or [{}])[0].get("url"))


@tool
def generate_image(prompt: str, filename: str = "", image_size: str = "1024x1024") -> str:
    """Generate ONE image from a text prompt via SiliconFlow's Kolors model, save it
    locally, and return the LOCAL PATH. Use this to create the cover and each in-article
    illustration so every <img> points at a real file. Write vivid, concrete prompts
    (subject + style + colors). For an infographic-style figure, describe it in words.

    Args:
        prompt: what to draw, e.g. "扁平插画风：中美 AI 算力对比，暖色调，简洁现代".
        filename: optional output name; auto-generated if omitted.
        image_size: one of 1024x1024 / 960x1280 / 1280x960 / 720x1440 / 1440x720.
    """
    key = _key()
    if not key:
        return "[ERROR] No SILICONFLOW_API_KEY set — cannot generate images."
    if not (prompt or "").strip():
        return "[ERROR] Empty prompt."
    body = {"model": _model(), "prompt": prompt.strip(),
            "image_size": image_size, "batch_size": 1,
            "num_inference_steps": 20, "guidance_scale": 7.5}
    try:
        r = requests.post(_base() + "/images/generations",
                          headers={"Authorization": "Bearer " + key,
                                   "Content-Type": "application/json"},
                          json=body, timeout=120)
        j = r.json()
    except Exception as e:  # noqa: BLE001
        return f"[ERROR] Kolors request failed: {e}"
    url = _pick_url(j)
    if not url:
        return f"[ERROR] No image returned: {str(j)[:400]}"
    try:
        d = _imgdir()
        os.makedirs(d, exist_ok=True)
        name = (filename or "").strip() or f"gen_{int(time.time()*1000)}"
        if not name.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
            name += ".png"
        path = os.path.join(d, name)
        with requests.get(url, timeout=120, stream=True) as resp, open(path, "wb") as f:
            for chunk in resp.iter_content(8192):
                f.write(chunk)
        return f"[saved] {path}"
    except Exception as e:  # noqa: BLE001
        return f"[ERROR] Could not download generated image: {e}"
