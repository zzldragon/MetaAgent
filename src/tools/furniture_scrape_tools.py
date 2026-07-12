"""Scrape promotional / product images from furniture retailer sites.

Step 1 of the furniture-ad collector: given a site URL, fetch the page, pull the big
promo/hero/product images (skipping logos/icons/sprites/scripts), and download them into
a local inbox folder so the vision classifier can label them next.

Handles the three real-world cases seen on furniture sites:
  * plain server-rendered HTML  -> parsed directly (fast, no browser);
  * modern CDN images with NO file extension (``?w=1920&fm=webp``, imgix, scene7, …)
    -> recognized by CDN host + format/width query hints, not just ``.jpg``;
  * JavaScript-rendered SPAs (e.g. Castlery) or anti-bot pages (403/DataDome/captcha)
    -> optional headless-browser fallback via Playwright (real Chromium executes the JS
    and passes most bot checks). Playwright is OPTIONAL: if it isn't installed we still
    do the static pass and return a clear hint on how to enable rendering.

Collect dir: env ``FURNITURE_DIR`` (default ``./collected_images``). Raw downloads land
in ``<FURNITURE_DIR>/_inbox/``; the organizer later files them into per-category folders.

Enable JS rendering (one-time):  ``pip install playwright`` then ``playwright install chromium``.

Conventions: ``from tool_registry import tool``; every top-level ``def`` is a tool, so
all module-level helpers are LAMBDAS (browser/parse helpers are nested INSIDE the tool,
which keeps them from being registered as tools). Tools return strings and never raise.
"""
from tool_registry import tool

import os
import re
import time
import html as _htmlmod
import urllib.parse
import requests

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_dir = lambda: os.environ.get("FURNITURE_DIR", os.path.join(os.getcwd(), "collected_images"))
_inbox = lambda: os.path.join(_dir(), "_inbox")
_host = lambda u: (urllib.parse.urlparse(u).netloc or "site").replace("www.", "").split(":")[0]
_slug = lambda s: re.sub(r"[^a-zA-Z0-9_.-]+", "_", s)[:60].strip("_") or "img"
_absurl = lambda base, u: urllib.parse.urljoin(base, _htmlmod.unescape((u or "").strip().strip('"\'')))
# never want these (chrome/branding/tracking creative)
_JUNK = re.compile(r"(logo|icon|favicon|sprite|avatar|badge|flag|payment|visa|master"
                   r"|paypal|pixel|spinner|loader|placeholder|1x1|blank|sprite)", re.I)
# hard-reject non-image assets that live on the same CDNs
_BADEXT = re.compile(r"\.(js|mjs|css|svg|woff2?|ttf|eot|ico|json|xml|mp4|webm|m3u8|pdf)(\?|$)", re.I)
_IMGEXT = re.compile(r"\.(jpe?g|png|webp|avif)(\?|$)", re.I)
# a media/CDN host (extensionless images live here)
_CDNHOST = re.compile(r"(cdn|media|img|image|static|assets|imgix|scene7|cloudinary"
                      r"|cloudfront|akamai|contentful|shopify|fastly|kraken|mozu|demandware)", re.I)
# an image-format / resize query hint (extensionless images carry these)
_FMTHINT = re.compile(r"[?&](?:fm|format)=(?:webp|jpe?g|png|avif)|[?&](?:w|width)=\d{2,4}", re.I)
# biggest width hint we can read from the url (?w=1920, _1920x, 1920w)
_width = lambda u: max([int(x) for x in re.findall(
    r"(?:[?&](?:w|width)=|_)(\d{3,4})(?:x|w|&|$)", u or "")] or [0])


@tool
def list_trending_furniture_sites() -> str:
    """Return a curated list of popular / trending furniture retailer homepages to
    scrape (Castlery, Maisons du Monde, and peers). Use it when the task doesn't name
    a specific site, or to fill a scheduled crawl. Returns one URL per line.
    """
    # NOTE: many global furniture sites geo-gate the bare domain behind a country
    # selector (e.g. castlery.com -> region splash with no products). Use a
    # COUNTRY-SPECIFIC url so the real, image-rich homepage renders.
    sites = [
        "https://www.article.com",                 # server-rendered, no browser needed
        "https://www.castlery.com/us",             # country url (needs headless browser)
        "https://www.castlery.com/sg",
        "https://www.maisonsdumonde.com/US/en",    # best-effort (may be bot-gated)
        "https://www.made.com",
        "https://www2.hm.com/en_us/home.html",     # H&M Home
    ]
    return "\n".join(sites)


@tool
def scrape_ad_images(site_url: str, max_images: int = 12, render: str = "auto") -> str:
    """Fetch ONE furniture site page and download its promotional / product images to a
    local inbox folder. Skips logos, icons, scripts and tracking pixels; understands
    extensionless CDN images; can fall back to a headless browser for JS-rendered or
    bot-protected sites. Returns one saved LOCAL PATH per line (feed these to
    classify_image next). Call once per site.

    Args:
        site_url: the page URL to scrape, e.g. "https://www.article.com".
        max_images: max images to keep (1-30, default 12).
        render: "auto" (static first, browser only if static finds nothing / is blocked),
                "always" (force headless browser), or "never" (static only).
    """
    url = (site_url or "").strip()
    if not url:
        return "[ERROR] scrape_ad_images needs a site_url."
    if not url.startswith("http"):
        url = "https://" + url
    try:
        n = max(1, min(int(max_images), 30))
    except Exception:  # noqa: BLE001
        n = 12
    mode = (render or "auto").strip().lower()

    def extract(html, base):
        cands = []
        cands += re.findall(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)', html, re.I)
        cands += re.findall(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', html, re.I)
        cands += re.findall(r'<img[^>]+(?:data-src|data-original|data-lazy|src)=["\']([^"\']+)', html, re.I)
        for ss in re.findall(r'(?:srcset|data-srcset)=["\']([^"\']+)', html, re.I):
            for part in ss.split(","):
                u = part.strip().split(" ")[0]
                if u:
                    cands.append(u)
        cands += [m[1] for m in re.findall(
            r'background-image\s*:\s*url\((["\']?)([^)"\']+)\1\)', html, re.I)]
        cands += re.findall(r'"(https?://[^"]+?(?:\.(?:jpe?g|png|webp|avif)|[?&](?:fm|format)=[a-z]+)[^"]*)"', html, re.I)
        best = {}  # path (sans query) -> (width, url) : collapse all sizes of one asset
        for c in cands:
            a = _absurl(base, c)
            if not a.startswith("http") or _JUNK.search(a) or _BADEXT.search(a):
                continue
            is_img = _IMGEXT.search(a) or (_CDNHOST.search(urllib.parse.urlparse(a).netloc)
                                           and _FMTHINT.search(a))
            if not is_img:
                continue
            key = a.split("?")[0]
            w = _width(a)
            if key not in best or w > best[key][0]:
                best[key] = (w, a)
        return [u for _, u in sorted(best.values(), key=lambda x: x[0], reverse=True)]

    def render_html(u):
        """Return (html, status). status: 'ok' | 'noplaywright' | 'failed:<msg>'."""
        try:
            from playwright.sync_api import sync_playwright
        except Exception:  # noqa: BLE001
            return "", "noplaywright"
        try:
            with sync_playwright() as p:
                b = p.chromium.launch(headless=True)
                pg = b.new_context(user_agent=_UA, locale="en-US").new_page()
                pg.goto(u, wait_until="domcontentloaded", timeout=45000)
                pg.wait_for_timeout(2500)
                for _ in range(6):  # scroll to trigger lazy-loaded creatives
                    pg.mouse.wheel(0, 4000)
                    pg.wait_for_timeout(700)
                content = pg.content()
                b.close()
                return content, "ok"
        except Exception as e:  # noqa: BLE001
            return "", f"failed:{e}"

    # ── 1) static pass (unless forced to browser) ──
    urls, notes = [], []
    static_status = ""
    if mode != "always":
        try:
            resp = requests.get(url, headers={"User-Agent": _UA,
                                "Accept-Language": "en,zh-CN;q=0.8"}, timeout=30)
            static_status = str(resp.status_code)
            if resp.status_code == 200:
                urls = extract(resp.text, url)
            else:
                notes.append(f"static HTTP {resp.status_code}（疑似反爬）")
        except Exception as e:  # noqa: BLE001
            notes.append(f"static fetch 失败: {e}")

    # ── 2) headless-browser fallback ──
    if mode == "always" or (mode == "auto" and not urls):
        html2, rstatus = render_html(url)
        if rstatus == "ok":
            urls = extract(html2, url) or urls
        elif rstatus == "noplaywright":
            notes.append("未安装 Playwright，无法渲染 JS 站点。"
                         "启用方法：pip install playwright 然后 playwright install chromium")
        else:
            notes.append("浏览器渲染" + rstatus)

    if not urls:
        tail = ("；" + "；".join(notes)) if notes else ""
        return (f"[none] 在 {url} 未找到可用的广告/产品图（static={static_status or 'n/a'}）{tail}。"
                "该站可能纯前端渲染或有反爬——装好 Playwright 后重试，或换用 Article 这类服务端渲染的站。")

    # ── 3) download ──
    os.makedirs(_inbox(), exist_ok=True)
    host, saved = _host(url), []
    for i, a in enumerate(urls):
        if len(saved) >= n:
            break
        try:
            with requests.get(a, headers={"User-Agent": _UA, "Referer": url},
                              timeout=40) as r:
                if r.status_code != 200:
                    continue
                data = r.content
                ctype = r.headers.get("Content-Type", "").lower()
            if len(data) < 8000:            # too small to be a real creative
                continue
            if "image" not in ctype and not _IMGEXT.search(a) and "webp" not in a.lower():
                continue                    # not actually an image
            m = _IMGEXT.search(a)
            ext = (m.group(1) if m else None) or (
                "webp" if "webp" in (ctype + a).lower() else
                "png" if "png" in ctype else "jpg")
            ext = "jpg" if ext == "jpeg" else ext
            name = f"{_slug(host)}_{int(time.time())}_{i}.{ext}"
            path = os.path.join(_inbox(), name)
            with open(path, "wb") as f:
                f.write(data)
            saved.append(path)
        except Exception:  # noqa: BLE001
            continue

    if not saved:
        return (f"[none] 在 {url} 解析到 {len(urls)} 个图片链接，但下载均失败"
                + (("；" + "；".join(notes)) if notes else "") + "。")
    extra = ("（" + "；".join(notes) + "）") if notes else ""
    return (f"[saved {len(saved)} images from {host}]{extra}\n" + "\n".join(saved))
