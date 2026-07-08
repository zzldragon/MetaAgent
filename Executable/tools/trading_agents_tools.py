"""TradingAgents data toolset — prices, technical indicators, fundamentals,
news and social sentiment for equities / crypto.

A faithful-but-lean port of the data layer from TauricResearch/TradingAgents.
Every function is FAIL-SOFT and OFFLINE-SAFE: it lazily uses an optional library
(yfinance for prices/fundamentals/news, stockstats for indicators) and returns a
clear "[note] ..." string when the library or network is unavailable, so the agent
keeps running and reports the gap instead of crashing.

Make them live with:  pip install yfinance stockstats pandas
Ticker format follows Yahoo Finance: US "AAPL", HK "0700.HK", Tokyo "7203.T",
crypto "BTC-USD".
"""
from tool_registry import tool

import datetime as _dt

try:                                 # optional — offline-safe (pulled into requirements)
    import yfinance as yf
except Exception:
    yf = None
try:
    import pandas as pd
except Exception:
    pd = None
try:
    import stockstats            # noqa: F401 — technical indicators
except Exception:
    stockstats = None

# ── helpers (LAMBDAS only — every top-level `def` is registered as a tool) ────
_today = lambda: _dt.date.today().isoformat()
_need_yf = lambda: ("[note] Market data needs yfinance — run: pip install yfinance. "
                    "No data returned; state this limitation in your analysis.")
_ago = lambda days: (_dt.date.today() - _dt.timedelta(days=int(days))).isoformat()


@tool
def get_price_history(ticker: str, start_date: str = "", end_date: str = "") -> str:
    """Daily OHLCV price history for a ticker (Yahoo Finance). Dates are YYYY-MM-DD;
    blank end = today, blank start = ~6 months before end. Use for price action,
    trends, ranges and volume. Yahoo suffixes apply (e.g. 0700.HK, BTC-USD)."""
    if yf is None or pd is None:
        return _need_yf()
    try:
        end = end_date or _today()
        start = start_date or (_dt.date.fromisoformat(end) - _dt.timedelta(days=180)).isoformat()
        df = yf.Ticker(ticker).history(start=start, end=end, auto_adjust=False)
        if df is None or df.empty:
            return f"[note] No price data for {ticker} in {start}..{end}."
        tail = df.tail(30)[["Open", "High", "Low", "Close", "Volume"]].round(2)
        first, last = df["Close"].iloc[0], df["Close"].iloc[-1]
        chg = (last - first) / first * 100 if first else 0.0
        return (f"Daily OHLCV for {ticker} ({start}..{end}); period change "
                f"{chg:+.1f}%. Last {len(tail)} rows:\n" + tail.to_string())
    except Exception as e:
        return f"[note] price history unavailable for {ticker}: {e}"


@tool
def get_stock_quote(ticker: str) -> str:
    """Latest quote snapshot for a ticker (Yahoo Finance): current price, day move,
    52-week range, volume and market cap. Use for a quick read of where price is."""
    if yf is None:
        return _need_yf()
    try:
        info = yf.Ticker(ticker).info or {}
        if not info:
            return f"[note] No quote for {ticker}."
        fields = [("currentPrice", "Price"), ("previousClose", "Prev close"),
                  ("dayHigh", "Day high"), ("dayLow", "Day low"),
                  ("fiftyTwoWeekHigh", "52w high"), ("fiftyTwoWeekLow", "52w low"),
                  ("volume", "Volume"), ("marketCap", "Market cap")]
        lines = [f"Quote for {ticker} ({info.get('shortName', ticker)}):"]
        for k, label in fields:
            if info.get(k) is not None:
                lines.append(f"  {label}: {info[k]}")
        return "\n".join(lines) if len(lines) > 1 else f"[note] Sparse quote for {ticker}."
    except Exception as e:
        return f"[note] quote unavailable for {ticker}: {e}"


@tool
def get_technical_indicators(ticker: str,
                             indicators: str = "macd,rsi_14,close_50_sma,close_200_sma,boll,atr",
                             lookback_days: int = 200) -> str:
    """Compute technical indicators for a ticker via the stockstats library over
    recent Yahoo Finance history. `indicators` is a comma-separated list of
    stockstats columns (e.g. macd, rsi_14, close_50_sma, close_200_sma, boll, atr,
    kdjk). Returns each indicator's latest value plus its short-term trend."""
    if yf is None or pd is None:
        return _need_yf()
    if stockstats is None:
        return "[note] Technical indicators need stockstats — run: pip install stockstats."
    try:
        raw = yf.Ticker(ticker).history(start=_ago(max(60, lookback_days)),
                                        end=_today(), auto_adjust=False)
        if raw is None or raw.empty:
            return f"[note] No price data for {ticker} to compute indicators."
        df = raw.reset_index()
        df.columns = [str(c).lower() for c in df.columns]
        sdf = (stockstats.wrap(df) if hasattr(stockstats, "wrap")
               else stockstats.StockDataFrame.retype(df))
        out = [f"Technical indicators for {ticker} (as of {_today()}):"]
        for ind in [i.strip() for i in indicators.split(",") if i.strip()]:
            try:
                s = sdf[ind]
                latest = float(s.iloc[-1])
                prev = float(s.iloc[-6]) if len(s) > 6 else float(s.iloc[0])
                trend = "rising" if latest > prev else "falling" if latest < prev else "flat"
                out.append(f"  {ind}: {round(latest, 4)} ({trend} over ~5 bars)")
            except Exception as ie:
                out.append(f"  {ind}: [unavailable: {ie}]")
        return "\n".join(out)
    except Exception as e:
        return f"[note] indicators unavailable for {ticker}: {e}"


@tool
def get_fundamentals(ticker: str) -> str:
    """Key fundamental metrics for a company (Yahoo Finance): valuation (P/E, P/B,
    market cap), profitability (margins, ROE), growth, and balance-sheet health.
    Use to judge intrinsic value and spot red flags."""
    if yf is None:
        return _need_yf()
    try:
        info = yf.Ticker(ticker).info or {}
        if not info:
            return f"[note] No fundamentals for {ticker}."
        keys = [("longName", "Name"), ("sector", "Sector"), ("industry", "Industry"),
                ("marketCap", "Market cap"), ("trailingPE", "Trailing P/E"),
                ("forwardPE", "Forward P/E"), ("priceToBook", "P/B"),
                ("profitMargins", "Profit margin"), ("returnOnEquity", "ROE"),
                ("revenueGrowth", "Revenue growth"), ("earningsGrowth", "Earnings growth"),
                ("debtToEquity", "Debt/Equity"), ("freeCashflow", "Free cash flow"),
                ("dividendYield", "Dividend yield"), ("beta", "Beta")]
        lines = [f"Fundamentals for {ticker}:"]
        for k, label in keys:
            if info.get(k) is not None:
                lines.append(f"  {label}: {info[k]}")
        return "\n".join(lines) if len(lines) > 1 else f"[note] Sparse fundamentals for {ticker}."
    except Exception as e:
        return f"[note] fundamentals unavailable for {ticker}: {e}"


@tool
def get_company_news(ticker: str, limit: int = 10) -> str:
    """Recent news headlines for a company/ticker (Yahoo Finance). Use to gauge
    catalysts and event risk. Returns up to `limit` headlines with their source."""
    if yf is None:
        return _need_yf()
    try:
        items = getattr(yf.Ticker(ticker), "news", None) or []
        if not items:
            return f"[note] No recent news for {ticker}."
        out = [f"Recent news for {ticker}:"]
        for it in items[:max(1, int(limit))]:
            c = it.get("content", it) if isinstance(it, dict) else {}
            title = c.get("title") or (it.get("title") if isinstance(it, dict) else None) or "(no title)"
            prov = c.get("provider")
            pub = prov.get("displayName") if isinstance(prov, dict) else (
                it.get("publisher", "") if isinstance(it, dict) else "")
            out.append(f"  • {title}" + (f" — {pub}" if pub else ""))
        return "\n".join(out)
    except Exception as e:
        return f"[note] news unavailable for {ticker}: {e}"


@tool
def get_macro_news(limit: int = 10) -> str:
    """Broad market / macro news headlines (via the S&P 500 index feed) to read the
    macro backdrop — rates, inflation, growth, risk appetite. Keyless, best-effort."""
    if yf is None:
        return _need_yf()
    try:
        items = getattr(yf.Ticker("^GSPC"), "news", None) or []
        if not items:
            return "[note] No macro headlines available right now."
        out = ["Macro / market headlines:"]
        for it in items[:max(1, int(limit))]:
            c = it.get("content", it) if isinstance(it, dict) else {}
            title = c.get("title") or (it.get("title") if isinstance(it, dict) else None) or "(no title)"
            out.append(f"  • {title}")
        return "\n".join(out)
    except Exception as e:
        return f"[note] macro news unavailable: {e}"


@tool
def get_social_sentiment(ticker: str) -> str:
    """Social / analyst sentiment proxy for a ticker: analyst recommendation trend
    and price targets (Yahoo Finance). NOTE: full Reddit / StockTwits ingestion
    needs their API keys (not wired here) — this is a keyless approximation."""
    if yf is None:
        return _need_yf()
    try:
        info = yf.Ticker(ticker).info or {}
        lines = [f"Sentiment proxy for {ticker} "
                 f"(analyst signal; Reddit/StockTwits need API keys):"]
        if info.get("recommendationKey"):
            lines.append(f"  Analyst consensus: {info['recommendationKey']} "
                         f"(mean {info.get('recommendationMean', '?')})")
        for k, label in [("targetMeanPrice", "Mean target"),
                         ("targetHighPrice", "High target"),
                         ("targetLowPrice", "Low target"),
                         ("currentPrice", "Current price"),
                         ("numberOfAnalystOpinions", "# analysts")]:
            if info.get(k) is not None:
                lines.append(f"  {label}: {info[k]}")
        return "\n".join(lines) if len(lines) > 1 else f"[note] No sentiment signal for {ticker}."
    except Exception as e:
        return f"[note] sentiment unavailable for {ticker}: {e}"
