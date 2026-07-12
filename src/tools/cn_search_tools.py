"""China-friendly, key-less web search for the AI WeChat content factory.

The built-in ``web_search`` tool defaults to DuckDuckGo, which is unreachable from
mainland China. This module provides a drop-in replacement, ``cn_web_search``, that
scrapes reachable engines directly (no API key):

    1. Bing 国内版 (cn.bing.com) — reachable in China, light anti-bot, easy to parse.
    2. Baidu (www.baidu.com)     — fallback; carries a BAIDUID cookie and returns
                                    Baidu's redirect links (read_web_page follows them).

It returns the top results as ``title / url / snippet`` — the same shape the agent
expects from web search — then use ``read_web_page`` to read the ones you want.

Best-effort by nature: HTML scraping can break if an engine changes its markup or
tightens anti-bot. For stable topic sourcing prefer the RSS tools; use this for
ad-hoc fact-checking.

Conventions: ``from tool_registry import tool``; every top-level ``def`` is a tool,
so all helpers are LAMBDAS. Tools return strings and never raise.
"""
from tool_registry import tool

import re
import http.client
import html as _html
import urllib.parse
import urllib.request

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_MAXLEN = 200  # snippet clip

# GET a URL and return decoded HTML. Raises on failure; IncompleteRead is caught by
# callers (its .partial still holds a usable page). Accept-Encoding: identity avoids
# gzip/chunked truncation seen from Baidu.
_get = lambda url, cookie=None: urllib.request.urlopen(
    urllib.request.Request(url, headers={
        "User-Agent": _UA,
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Accept-Encoding": "identity",
        **({"Cookie": cookie} if cookie else {})}),
    timeout=25).read().decode("utf-8", "replace")

# Strip tags + collapse whitespace + unescape entities -> plain text.
_text = lambda h: _html.unescape(
    re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", h or ""))).strip()
_clip = lambda s: s if len(s) <= _MAXLEN else s[:_MAXLEN] + "…"

# First "<h2 ..><a href=..>title</a>" (Bing) / "<h3 ..><a ..>" (Baidu) in a chunk.
_h2a = lambda c: re.search(r'<h2[^>]*>\s*<a[^>]+href="(http[^"]+)"[^>]*>(.*?)</a>', c, re.S)
_h2a_all = lambda c: re.findall(r'<h2[^>]*>\s*<a[^>]+href="(http[^"]+)"[^>]*>(.*?)</a>', c, re.S)
_h3a = lambda c: re.search(r'<h3[^>]*>\s*<a[^>]+href="(http[^"]+)"[^>]*>(.*?)</a>', c, re.S)
# First paragraph snippet (Bing) / abstract span (Baidu content区) in a chunk.
_p1 = lambda c: re.search(r'<p[^>]*>(.*?)</p>', c, re.S)
_babs = lambda c: re.search(
    r'class="(?:content-right_[^"]*|c-abstract[^"]*)"[^>]*>(.*?)</(?:span|div)>', c, re.S)
# Snippet text from a match (or ""), tags stripped + clipped.
_snip = lambda m: _clip(_text(m.group(1))) if m else ""

# Format hits [(title,url,snippet)] into numbered lines.
_fmt = lambda engine, hits: "\n".join(
    [f"[via {engine}] {len(hits)} 条结果:"]
    + [f"{i}. {t}\n   {u}" + (f"\n   {s}" if s else "")
       for i, (t, u, s) in enumerate(hits, 1)])


@tool
def cn_web_search(query: str, max_results: int = 5) -> str:
    """Search the public web from mainland China WITHOUT any API key or proxy.

    Tries Bing 国内版 (cn.bing.com) first, then Baidu as a fallback, and returns the
    top results as numbered "title / URL / snippet" lines. Use it to find EXTERNAL or
    RECENT facts, then call read_web_page on the URLs worth reading. Prefer the RSS
    tools for routine topic sourcing; use this for ad-hoc fact-checking.

    Args:
        query: what to search for, e.g. "硅基流动 新用户 免费额度 2026".
        max_results: how many results to return (1-10, default 5).
    """
    q = (query or "").strip()
    if not q:
        return "[ERROR] cn_web_search needs a 'query'."
    try:
        n = max(1, min(int(max_results), 10))
    except Exception:  # noqa: BLE001
        n = 5

    errors = []

    # ── engine 1: Bing 国内版 ── (parse per b_algo chunk; fall back to global h2>a)
    try:
        url = "https://cn.bing.com/search?q=" + urllib.parse.quote(q) + f"&count={n}"
        try:
            htm = _get(url)
        except http.client.IncompleteRead as ie:
            htm = ie.partial.decode("utf-8", "replace")
        hits = []
        for c in re.split(r'<li class="b_algo"', htm)[1:]:
            m = _h2a(c)
            if not m:
                continue
            hits.append((_text(m.group(2)), _html.unescape(m.group(1)), _snip(_p1(c))))
            if len(hits) >= n:
                break
        if not hits:
            for u, t in _h2a_all(htm)[:n]:
                hits.append((_text(t), _html.unescape(u), ""))
        if hits:
            return _fmt("Bing 国内版", hits[:n])
        errors.append("bing: no results parsed")
    except Exception as e:  # noqa: BLE001
        errors.append(f"bing: {e}")

    # ── engine 2: Baidu (fallback) ── (snippet only from abstract区, else omit)
    try:
        url = "https://www.baidu.com/s?wd=" + urllib.parse.quote(q) + f"&rn={n}"
        try:
            htm = _get(url, cookie="BAIDUID=0000000000000000000000000000000000:FG=1")
        except http.client.IncompleteRead as ie:
            htm = ie.partial.decode("utf-8", "replace")
        hits = []
        for c in re.split(r'<div[^>]+class="result', htm)[1:]:
            m = _h3a(c)
            if not m:
                continue
            hits.append((_text(m.group(2)), _html.unescape(m.group(1)), _snip(_babs(c))))
            if len(hits) >= n:
                break
        if hits:
            return _fmt("百度", hits[:n]) + (
                "\n(注:百度链接为跳转地址,用 read_web_page 打开会自动跳到原文。)")
        errors.append("baidu: no results parsed")
    except Exception as e:  # noqa: BLE001
        errors.append(f"baidu: {e}")

    return ("[ERROR] cn_web_search failed (" + "; ".join(errors[:3]) + "). "
            "国内网络可直连 cn.bing.com/baidu.com,若仍失败可能是对方改版或风控。"
            "可改用 RSS 工具或直接给 read_web_page 一个已知网址。")
