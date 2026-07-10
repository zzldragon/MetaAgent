"""US-equity TradingAgents data toolset — LIVE from the web, stdlib only.

A drop-in replacement for trading_agents_tools.py (same function NAMES, so the
existing prompts keep working) with NO heavy deps (no yfinance/stockstats):
  * live QUOTE  -> Tencent  qt.gtimg.cn  (usAAPL)
  * daily HISTORY + technicals -> Nasdaq api.nasdaq.com historical
  * news / sentiment / fundamentals -> optional `ddgs` web search
Every function is FAIL-SOFT (returns an [ERROR]/[note] string, never raises).

Proxy strategy (mirrors cn_market_tools): try a DIRECT connection FIRST; only if
that fails fall back to _PROXY (default http://10.144.1.10:8080, override with the
US_MARKET_PROXY / HTTP(S)_PROXY env var). Ticker: US "AAPL".
"""

import datetime as _dt
import json
import os
import urllib.request

_up = lambda t: str(t or "").strip().upper()
_PROXY = (os.environ.get("US_MARKET_PROXY") or os.environ.get("HTTPS_PROXY")
          or os.environ.get("HTTP_PROXY") or "http://10.144.1.10:8080")


def _open(url, proxy, timeout):
    handler = urllib.request.ProxyHandler({} if not proxy
                                          else {"http": proxy, "https": proxy})
    opener = urllib.request.build_opener(handler)
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9", "Referer": "https://www.nasdaq.com/"})
    with opener.open(req, timeout=timeout) as r:
        return r.read()


def _http(url, timeout=12, enc="utf-8"):
    """DIRECT first (short probe); on failure retry via _PROXY."""
    try:
        return _open(url, None, min(timeout, 6)).decode(enc, "replace")
    except Exception:
        if _PROXY:
            return _open(url, _PROXY, timeout).decode(enc, "replace")
        raise


def _nasdaq_hist(ticker, limit=250):
    """Daily history from Nasdaq, oldest→newest: [{date,open,high,low,close,vol}]."""
    today = _dt.date.today()
    frm = (today - _dt.timedelta(days=int(limit * 1.7) + 10)).isoformat()
    url = ("https://api.nasdaq.com/api/quote/%s/historical?assetclass=stocks"
           "&fromdate=%s&limit=%d&todate=%s" % (_up(ticker), frm, limit, today.isoformat()))
    data = json.loads(_http(url))["data"]["tradesTable"]["rows"]
    num = lambda s: float(str(s).replace("$", "").replace(",", "").strip() or 0)
    out = []
    for r in reversed(data):                       # Nasdaq is newest-first
        try:
            m, d, y = r["date"].split("/")
            out.append({"date": f"{y}-{m}-{d}", "open": num(r["open"]),
                        "high": num(r["high"]), "low": num(r["low"]),
                        "close": num(r["close"]), "vol": r.get("volume", "")})
        except Exception:  # noqa: BLE001
            pass
    return out


def get_stock_quote(ticker: str) -> str:
    """Latest quote for a US ticker (live, Tencent): current price, day move %, OHLC,
    volume. Use this for the CURRENT price, not a remembered number."""
    t = _up(ticker)
    if not t:
        return "[ERROR] get_stock_quote needs a ticker, e.g. 'AAPL'."
    try:
        raw = _http("https://qt.gtimg.cn/q=us" + t, enc="gbk")
        f = raw.split('="', 1)[1].rstrip('";\n').split("~")
        if len(f) < 35:
            return f"[note] no quote for '{t}' (check the ticker)."
        return (f"{t}  price {f[3]}  chg {f[31]} ({f[32]}%)  open {f[5]}  "
                f"high {f[33]}  low {f[34]}  prevClose {f[4]}  vol {f[6]}  "
                f"{f[35] if len(f) > 35 else 'USD'}  time {f[30]} (live, Tencent)")
    except Exception as e:  # noqa: BLE001
        return f"[ERROR] quote failed for {t}: {type(e).__name__}: {e}"


def get_price_history(ticker: str, start_date: str = "", end_date: str = "") -> str:
    """Daily OHLCV history for a US ticker (live, Nasdaq). Dates YYYY-MM-DD; blank =
    last ~30 sessions. Use for the ACTUAL recent price path."""
    try:
        rows = _nasdaq_hist(ticker, 120)
    except Exception as e:  # noqa: BLE001
        return f"[ERROR] price history failed for {_up(ticker)}: {type(e).__name__}: {e}"
    if not rows:
        return f"[note] no price history for '{_up(ticker)}'."
    if start_date:
        rows = [r for r in rows if r["date"] >= start_date]
    if end_date:
        rows = [r for r in rows if r["date"] <= end_date]
    if not (start_date or end_date):
        rows = rows[-30:]
    body = "\n".join(f"{r['date']}  O {r['open']}  H {r['high']}  L {r['low']}  "
                     f"C {r['close']}  V {r['vol']}" for r in rows)
    return f"{_up(ticker)} daily OHLCV (live, Nasdaq) — {len(rows)} rows:\n{body}"


def get_technical_indicators(ticker: str,
                             indicators: str = "sma_50,sma_200,rsi_14",
                             lookback_days: int = 200) -> str:
    """Technicals computed from LIVE Nasdaq history (stdlib): SMA-50, SMA-200,
    RSI-14 and a trend read. Use for the CURRENT technical picture."""
    try:
        closes = [r["close"] for r in _nasdaq_hist(ticker, max(60, int(lookback_days) + 20))]
    except Exception as e:  # noqa: BLE001
        return f"[ERROR] indicators failed for {_up(ticker)}: {type(e).__name__}: {e}"
    if len(closes) < 20:
        return f"[note] not enough history to compute indicators for '{_up(ticker)}'."
    price = closes[-1]
    sma = lambda k: (sum(closes[-k:]) / k) if len(closes) >= k else None

    def rsi(period=14):
        if len(closes) <= period:
            return None
        g = l = 0.0
        for i in range(-period, 0):
            d = closes[i] - closes[i - 1]
            g += max(d, 0.0); l += max(-d, 0.0)
        if l == 0:
            return 100.0
        return 100 - 100 / (1 + (g / period) / (l / period))

    s50, s200, r14 = sma(50), sma(200), rsi(14)
    trend = ("above both SMAs (bullish)" if s50 and s200 and price > s50 > s200 else
             "below both SMAs (bearish)" if s50 and s200 and price < s50 < s200 else "mixed")
    parts = [f"{_up(ticker)} technicals (live): price {price:.2f}"]
    if s50:
        parts.append(f"SMA50 {s50:.2f}")
    if s200:
        parts.append(f"SMA200 {s200:.2f}")
    if r14 is not None:
        parts.append(f"RSI14 {r14:.1f} ("
                     + ("overbought" if r14 >= 70 else "oversold" if r14 <= 30 else "neutral") + ")")
    parts.append("trend: " + trend)
    return "  |  ".join(parts)


# ── web-search-backed (news / sentiment / fundamentals) ──────────────────────
def _ddgs_search(query, n):
    try:
        try:
            from ddgs import DDGS
        except Exception:
            from duckduckgo_search import DDGS
    except Exception:
        return None

    def go(proxy):
        try:
            client = DDGS(proxy=proxy) if proxy else DDGS()
        except TypeError:
            client = DDGS(proxies=proxy) if proxy else DDGS()
        with client as d:
            return list(d.text(query, region="us-en", max_results=n))

    try:
        return go(None)
    except Exception:
        return go(_PROXY) if _PROXY else []


def _fmt(hits, header):
    if hits is None:
        return ("[note] web search needs the 'ddgs' package (pip install ddgs); "
                "meanwhile use the agent's built-in web_search tool.")
    if not hits:
        return "No results."
    return header + "\n" + "\n".join(
        f"- {str(h.get('title') or '').strip()}\n  "
        f"{str(h.get('href') or h.get('url') or '').strip()}\n  "
        f"{str(h.get('body') or '').strip()}" for h in hits)


def get_fundamentals(ticker: str) -> str:
    """Key fundamentals via LIVE web search (valuation, earnings, margins). Cite sources."""
    return _fmt(_ddgs_search(f"{_up(ticker)} stock fundamentals P/E market cap revenue earnings", 5),
                f"[live web search — verify] {_up(ticker)} fundamentals:")


def get_company_news(ticker: str, limit: int = 10) -> str:
    """Recent company/ticker news via LIVE web search. Cite URLs."""
    try:
        n = max(1, min(int(limit or 10), 10))
    except (TypeError, ValueError):
        n = 8
    return _fmt(_ddgs_search(f"{_up(ticker)} stock news latest", n),
                f"[live web search] {_up(ticker)} recent news:")


def get_macro_news(limit: int = 10) -> str:
    """Broad market / macro headlines (Fed, rates, S&P 500) via LIVE web search."""
    try:
        n = max(1, min(int(limit or 10), 10))
    except (TypeError, ValueError):
        n = 8
    return _fmt(_ddgs_search("US stock market Fed interest rates S&P 500 economy today", n),
                "[live web search] macro / market news:")


def get_social_sentiment(ticker: str) -> str:
    """Social / retail sentiment via LIVE web search (Reddit WSB, StockTwits, X)."""
    return _fmt(_ddgs_search(f"{_up(ticker)} stock reddit wallstreetbets stocktwits sentiment", 6),
                f"[live web search] {_up(ticker)} social sentiment:")
