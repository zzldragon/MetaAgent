"""Classify a local image with SiliconFlow's vision model (image-to-text).

Step 2 of the furniture-ad collector: read a downloaded image file, send it to a
SiliconFlow VLM (OpenAI-compatible /chat/completions with a base64 image) and get back
ONE category slug + a short caption. Runs unattended (no need to attach images to the
chat), and stays China-accessible + 0-extra-cost on the same SiliconFlow account.

Credentials / model (env, NEVER in the graph):
  * ``SILICONFLOW_API_KEY``    — your SiliconFlow key (same sk-... as the text LLM)
  * ``SILICONFLOW_BASE_URL``   — default https://api.siliconflow.cn/v1
  * ``SILICONFLOW_VLM_MODEL``  — default Qwen/Qwen3-VL-30B-A3B-Instruct
    Other currently-online vision models you can set instead:
      Qwen/Qwen3-VL-32B-Instruct, Qwen/Qwen3-VL-8B-Instruct, zai-org/GLM-4.5V
    (older ones like Qwen2.5-VL-* / Qwen2-VL-* / deepseek-vl2 were discontinued and
    return errcode 30003 "Model disabled".)

Category slugs (folder-safe, stable): sofa, bed, dining, table, storage, lighting,
decor, rug, outdoor, office, promo, other. ("promo" = a marketing banner/poster with
sale text and no single clear product.)

Conventions: ``from tool_registry import tool``; every top-level ``def`` is a tool,
so all helpers are LAMBDAS. Tools return strings and never raise. Uses ``requests``.
"""
from tool_registry import tool

import os
import re
import base64
import requests

_CATS = ["sofa", "bed", "dining", "table", "storage", "lighting",
         "decor", "rug", "outdoor", "office", "promo", "other"]
_key = lambda: os.environ.get("SILICONFLOW_API_KEY", "").strip()
_base = lambda: os.environ.get("SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1").rstrip("/")
_model = lambda: os.environ.get("SILICONFLOW_VLM_MODEL", "Qwen/Qwen3-VL-30B-A3B-Instruct")
_mime = lambda p: {"png": "image/png", "webp": "image/webp", "gif": "image/gif"}.get(
    os.path.splitext(p)[1].lower().lstrip("."), "image/jpeg")
_b64 = lambda p: base64.b64encode(open(p, "rb").read()).decode("ascii")
_pick = lambda text: next((c for c in _CATS if c in (text or "").lower()), "other")
_msg = lambda j: (((j.get("choices") or [{}])[0].get("message") or {}).get("content") or "")

_PROMPT = (
    "你在给一家家具电商网站的广告/产品图做分类。请从下面的类目里【只选一个】最贴切的英文 slug：\n"
    "sofa(沙发) / bed(床) / dining(餐桌椅) / table(桌几) / storage(收纳储物) / "
    "lighting(灯具) / decor(软装装饰品) / rug(地毯) / outdoor(户外家具) / office(办公家具) / "
    "promo(带促销文案的营销海报/banner，没有单一清晰产品) / other(其他)。\n"
    "严格按如下两行格式回复，不要多余内容：\n"
    "category: <slug>\n"
    "caption: <一句话中文描述，20字以内>")


@tool
def classify_image(image_path: str) -> str:
    """Classify ONE local furniture image via SiliconFlow's vision model and return its
    category slug + a short caption. Use it on each path from scrape_ad_images before
    organizing. Returns "category: <slug> | caption: <...>" (slug is one of: sofa, bed,
    dining, table, storage, lighting, decor, rug, outdoor, office, promo, other).

    Args:
        image_path: local path to the image file to classify.
    """
    if not os.path.isfile(image_path):
        return f"[ERROR] Not a file: {image_path}"
    key = _key()
    if not key:
        return "[ERROR] No SILICONFLOW_API_KEY set — cannot classify images."
    try:
        data_url = f"data:{_mime(image_path)};base64,{_b64(image_path)}"
    except Exception as e:  # noqa: BLE001
        return f"[ERROR] Could not read {image_path}: {e}"
    body = {
        "model": _model(),
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": data_url, "detail": "low"}},
            {"type": "text", "text": _PROMPT}]}],
        "max_tokens": 120, "temperature": 0.0,
    }
    try:
        r = requests.post(_base() + "/chat/completions",
                          headers={"Authorization": "Bearer " + key,
                                   "Content-Type": "application/json"},
                          json=body, timeout=90)
        j = r.json()
    except Exception as e:  # noqa: BLE001
        return f"[ERROR] VLM request failed: {e}"
    text = _msg(j)
    if not text:
        code = j.get("code") or j.get("error", {}).get("code") if isinstance(j, dict) else None
        if str(code) == "30003" or "disabled" in str(j).lower():
            return (f"[ERROR] 视觉模型 '{_model()}' 已被下架/停用 (30003)。"
                    "请把环境变量 SILICONFLOW_VLM_MODEL 设为在线模型，如 "
                    "Qwen/Qwen3-VL-30B-A3B-Instruct、Qwen/Qwen3-VL-32B-Instruct 或 zai-org/GLM-4.5V。")
        return f"[ERROR] VLM returned no content: {str(j)[:300]}"
    cat_line = re.search(r"category\s*[:：]\s*([a-zA-Z]+)", text)
    cap_line = re.search(r"caption\s*[:：]\s*(.+)", text)
    cat = _pick(cat_line.group(1) if cat_line else text)
    cap = (cap_line.group(1).strip() if cap_line else "").strip()[:40]
    return f"category: {cat} | caption: {cap}"
