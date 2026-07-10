"""China A-share (Shanghai / Shenzhen) market tools — LIVE data from the web, not
memorized knowledge. Every function fetches fresh data at call time and fails soft
(returns an [ERROR]/[note] string, never raises) so one bad call can't abort a run.

Pure stdlib HTTP (urllib) for quotes; optional `ddgs` for news search (falls back
to a clear note if the package isn't installed). Honors HTTP(S)_PROXY env vars.
"""

import os
import json
import urllib.parse
import urllib.request

# helpers must be lambdas (MetaAgent tool convention: top-level `def` = a tool)
_norm = lambda s: "".join(ch for ch in str(s or "").strip() if ch.isalnum())
_prefix = lambda c: ("" if c[:2].lower() in ("sh", "sz") else
                     ("sh" if c[:1] in ("6", "9") else "sz"))   # 6/9 => Shanghai

# Fallback proxy for reaching the Chinese web behind a firewall. Strategy: try a
# DIRECT connection first; only if that fails fall back to this proxy. Override
# with the CN_MARKET_PROXY (or standard HTTP(S)_PROXY) env var.
_PROXY = (os.environ.get("CN_MARKET_PROXY") or os.environ.get("HTTPS_PROXY")
          or os.environ.get("HTTP_PROXY") or "http://10.144.1.10:8080")


def _open(url, proxy, timeout):
    # ProxyHandler({}) = force DIRECT (ignore env proxies); a dict = use `proxy`.
    handler = urllib.request.ProxyHandler({} if not proxy
                                          else {"http": proxy, "https": proxy})
    opener = urllib.request.build_opener(handler)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0",
                                               "Referer": "https://finance.sina.com.cn"})
    with opener.open(req, timeout=timeout) as r:
        return r.read()


def _http(url, timeout=12):
    """Fetch `url` trying a DIRECT connection first (short probe); if that fails,
    retry via _PROXY. So a network where the CN sites are directly reachable uses
    NO proxy, and one behind a firewall falls back to the proxy automatically."""
    try:
        return _open(url, None, min(timeout, 6))          # direct probe
    except Exception:
        if _PROXY:
            return _open(url, _PROXY, timeout)            # firewalled -> via proxy
        raise


def get_ashare_quote(symbol):
    """Live quote for a China A-share by code (e.g. '600000' or 'sh600000' for
    Shanghai, '000001' for Shenzhen). Returns name, price, change %, open/high/low,
    volume and turnover from Tencent's realtime feed. Use this for the CURRENT price
    instead of any remembered number."""
    code = _norm(symbol).lower()
    if not code:
        return "[ERROR] get_ashare_quote needs a stock code, e.g. '600519'."
    full = code if code[:2] in ("sh", "sz") else (_prefix(code) + code)
    try:
        raw = _http("https://qt.gtimg.cn/q=" + full).decode("gbk", "replace")
    except Exception as e:  # noqa: BLE001
        return f"[ERROR] quote fetch failed for {full}: {type(e).__name__}: {e}"
    try:
        payload = raw.split('="', 1)[1].rstrip('";\n')
        f = payload.split("~")
        if len(f) < 40:
            return f"[note] no quote data for {full} (check the code / market hours)."
        return (f"{f[1]} ({full})  价 {f[3]}  涨跌 {f[31]} ({f[32]}%)  "
                f"今开 {f[5]}  最高 {f[33]}  最低 {f[34]}  昨收 {f[4]}  "
                f"成交量(手) {f[6]}  成交额(万) {f[37]}  时间 {f[30]}")
    except Exception as e:  # noqa: BLE001
        return f"[ERROR] could not parse quote for {full}: {e}"


def get_ashare_index(name="上证指数"):
    """Live level of a major China index. name ∈ {'上证指数'/'sh', '深证成指'/'sz',
    '创业板指'/'cyb', '沪深300'/'hs300'}. Use for the CURRENT market backdrop."""
    m = {"上证指数": "sh000001", "sh": "sh000001", "shanghai": "sh000001",
         "深证成指": "sz399001", "sz": "sz399001",
         "创业板指": "sz399006", "cyb": "sz399006",
         "沪深300": "sh000300", "hs300": "sh000300"}
    code = m.get(str(name).strip(), "sh000001")
    try:
        raw = _http("https://qt.gtimg.cn/q=" + code).decode("gbk", "replace")
        f = raw.split('="', 1)[1].rstrip('";\n').split("~")
        return f"{f[1]} 点位 {f[3]}  涨跌 {f[31]} ({f[32]}%)  时间 {f[30]}"
    except Exception as e:  # noqa: BLE001
        return f"[ERROR] index fetch failed ({code}): {e}"


def search_cn_market_news(query, max_results=5):
    """Search the CHINESE web for the LATEST news / analysis on an A-share company,
    sector or macro topic. Returns titles + urls + snippets. Use this for recent
    events instead of relying on training data. Cite the URLs you use."""
    q = str(query or "").strip()
    if not q:
        return "[ERROR] search_cn_market_news needs a query."
    try:
        n = max(1, min(int(max_results or 5), 10))
    except (TypeError, ValueError):
        n = 5
    try:
        try:
            from ddgs import DDGS
        except Exception:
            from duckduckgo_search import DDGS
    except Exception:
        return ("[note] news search needs the 'ddgs' package (pip install ddgs); "
                "meanwhile use the agent's built-in web_search tool.")

    def _ddgs(proxy):
        try:
            client = DDGS(proxy=proxy) if proxy else DDGS()
        except TypeError:                                  # older signature
            client = DDGS(proxies=proxy) if proxy else DDGS()
        with client as ddgs:
            return list(ddgs.text(q + " A股 最新", region="cn-zh", max_results=n))

    try:                                                   # DIRECT first...
        hits = _ddgs(None)
    except Exception:
        try:                                               # ...then via the proxy
            hits = _ddgs(_PROXY) if _PROXY else []
        except Exception as e:  # noqa: BLE001
            return f"[ERROR] news search failed: {type(e).__name__}: {e}"
    if not hits:
        return "无相关新闻：" + q
    out = ["[实时联网搜索结果，请核实后引用]"]
    for h in hits:
        title = str(h.get("title") or "").strip()
        url = str(h.get("href") or h.get("url") or "").strip()
        body = str(h.get("body") or "").strip()
        out.append(f"- {title}\n  {url}\n  {body}")
    return "\n".join(out)
