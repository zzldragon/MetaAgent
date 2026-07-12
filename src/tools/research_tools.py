"""Research + image toolset for the AI WeChat content factory.

Step 5 of the pipeline (从互联网搜索该主题相关的资料 / 图片): read the full text of a
source article/URL for grounding, and fetch stock images so the finished article is
图文并茂 (the PPTX requires 至少 3 张图, and images must be uploadable to WeChat's
servers — so we download them to local files the publisher can upload).

The China-friendly cn_web_search tool (cn_search_tools.py) finds *which* pages to read
(the built-in web_search/DuckDuckGo is disabled — unreachable from China); these tools
read a page's text and pull real image files.

Image search uses Pexels (free API key in env ``PEXELS_API_KEY``) when available; it
degrades gracefully to a clear message otherwise. Downloaded images land in
``./article_images/`` (override with env ``ARTICLE_IMAGE_DIR``).

Conventions: ``from tool_registry import tool``; every top-level ``def`` is a tool,
so all helpers are LAMBDAS. Tools return strings and never raise.
"""
from tool_registry import tool

import os
import re
import json
import urllib.parse
import urllib.request

_UA = "Mozilla/5.0 (compatible; MetaAgentContentBot/1.0)"
_MAX = 14000
_clip = lambda s: s if len(s) <= _MAX else s[:_MAX] + "\n... [truncated] ..."
_imgdir = lambda: os.environ.get("ARTICLE_IMAGE_DIR",
                                 os.path.join(os.getcwd(), "article_images"))
_req = lambda url, hdrs=None: urllib.request.urlopen(
    urllib.request.Request(url, headers={"User-Agent": _UA, **(hdrs or {})}),
    timeout=40)
_html_to_text = lambda h: re.sub(
    r"\s+", " ",
    re.sub(r"<[^>]+>", " ",
           re.sub(r"(?is)<(script|style).*?</\1>", " ", h or ""))).strip()


@tool
def read_web_page(url: str) -> str:
    """Fetch a web page and return its readable plain text (scripts/markup stripped).
    Use it to actually READ a news article or competitor post you found via web
    search, so your writing is grounded in real facts.

    Args:
        url: the page URL to read.
    """
    try:
        raw = _req(url).read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return f"[ERROR] Could not fetch {url}: {e}"
    text = _html_to_text(raw)
    return _clip(f"[text of {url}]\n{text}") if text else f"[empty] No text at {url}."


@tool
def search_images(query: str, count: int = 3) -> str:
    """Search free stock photos for a query (via Pexels; needs env PEXELS_API_KEY).
    Returns candidate image URLs — then call download_image on the ones you want.
    Use it to gather the 3+ images every article needs.

    Args:
        query: what to look for, e.g. "mini pc desk setup".
        count: how many image URLs to return (default 3).
    """
    key = os.environ.get("PEXELS_API_KEY", "").strip()
    if not key:
        return ("[ERROR] No PEXELS_API_KEY set. Set it in the environment/config, or "
                "provide image URLs another way, then use download_image.")
    api = ("https://api.pexels.com/v1/search?query="
           + urllib.parse.quote(query or "")
           + f"&per_page={max(1, int(count))}")
    try:
        data = json.loads(_req(api, {"Authorization": key}).read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        return f"[ERROR] Pexels search failed: {e}"
    photos = data.get("photos", []) or []
    if not photos:
        return f"[none] No images found for '{query}'."
    urls = [p.get("src", {}).get("large") or p.get("src", {}).get("original")
            for p in photos]
    return "\n".join(f"{i}. {u}" for i, u in enumerate([u for u in urls if u], 1))


@tool
def download_image(url: str, filename: str = "") -> str:
    """Download an image URL to a local file (in ./article_images/) and return the
    LOCAL PATH. The publisher uploads these local files to WeChat's servers, so every
    image in the article must be downloaded first.

    Args:
        url: the direct image URL.
        filename: optional file name; auto-generated from the URL if omitted.
    """
    d = _imgdir()
    try:
        os.makedirs(d, exist_ok=True)
        name = (filename or "").strip() or os.path.basename(
            urllib.parse.urlparse(url).path) or "image"
        if not re.search(r"\.(jpg|jpeg|png|gif|webp)$", name, re.I):
            name += ".jpg"
        path = os.path.join(d, name)
        with _req(url) as r, open(path, "wb") as f:
            f.write(r.read())
        return f"[saved] {path}"
    except Exception as e:  # noqa: BLE001
        return f"[ERROR] Could not download {url}: {e}"
