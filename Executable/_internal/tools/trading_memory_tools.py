"""TradingAgents reflection/memory toolset — the "learn from past decisions" loop.

Closes the biggest gap vs TauricResearch/TradingAgents: after a run the portfolio
manager RECORDS its decision; on a later run it REVIEWS prior same-ticker decisions
and their REALIZED RETURN (did BUY/SELL actually pay off?) before ruling — so the
desk improves over time. A persistent JSON decision log; realized return via
yfinance. Fail-soft / offline-safe: no store yet, no yfinance, or no network all
return a clear "[note] ..." rather than raising.
"""
from tool_registry import tool

import datetime as _dt
import json
import os

try:                                 # optional — offline-safe
    import yfinance as yf
except Exception:
    yf = None

# store next to the agent (or in the runtime workspace when one is set)
_store_dir = lambda: (get_workspace()[0]
                      if "get_workspace" in globals() and get_workspace()
                      else ".")
_store_path = lambda: os.path.join(_store_dir(), "trading_memory.json")
_today = lambda: _dt.date.today().isoformat()
# NOTE: helpers MUST be lambdas, not `def` — the codegen registers EVERY top-level
# `def` in a tool file as a tool (see DEF_NAME_RE), so a plain `def _load()` would
# become a phantom tool with no schema and crash the agent's LLM call. The @tool
# functions below are the only real tools. `_load` is fail-soft at its call sites.
_load = lambda: (json.load(open(_store_path(), encoding="utf-8"))
                 if os.path.exists(_store_path()) else [])


@tool
def record_decision(ticker: str, date: str, decision: str) -> str:
    """Append this run's final trading decision to the persistent decision log so
    future runs can learn from it. `decision` should include the action
    (BUY/SELL/HOLD) and a one-line rationale. Call this once, after you have ruled."""
    try:
        log = _load()
        if not isinstance(log, list):
            log = []
    except Exception:                    # missing / corrupt store → start fresh
        log = []
    log.append({"ticker": (ticker or "").upper(), "date": date or _today(),
                "decision": decision, "recorded_at": _today()})
    try:
        with open(_store_path(), "w", encoding="utf-8") as f:
            json.dump(log, f, indent=2, ensure_ascii=False)
    except OSError as e:
        return f"[note] could not persist decision: {e}"
    return f"Recorded decision for {ticker} ({date}). Log now holds {len(log)} decisions."


@tool
def get_past_decisions(ticker: str = "", limit: int = 8) -> str:
    """Review prior trading decisions from the persistent log (most recent first).
    Pass a ticker to filter to that symbol, or blank for cross-ticker lessons. Use
    this BEFORE ruling, to learn from what the desk decided before."""
    try:
        log = _load()
        if not isinstance(log, list):
            log = []
    except Exception:                    # missing / corrupt store → treat as empty
        log = []
    if not log:
        return "[note] No past decisions on record yet — this is a fresh desk."
    tkr = (ticker or "").upper()
    rows = [d for d in log if not tkr or d.get("ticker") == tkr]
    if not rows:
        return f"[note] No prior decisions on record for {ticker}."
    rows = rows[::-1][:max(1, int(limit))]
    out = [f"Past decisions{f' for {tkr}' if tkr else ''} (most recent first):"]
    for d in rows:
        out.append(f"  • {d.get('date')} {d.get('ticker')}: "
                   f"{str(d.get('decision', '')).strip()[:400]}")
    return "\n".join(out)


@tool
def get_realized_return(ticker: str, since_date: str) -> str:
    """Compute the realized price return for a ticker from `since_date` (YYYY-MM-DD)
    to today — i.e. how a decision made then would have played out. Use it with
    get_past_decisions to judge whether prior BUY/SELL calls actually paid off."""
    if yf is None:
        return ("[note] Realized return needs yfinance — run: pip install yfinance. "
                "Judge past calls qualitatively instead.")
    try:
        start = _dt.date.fromisoformat(since_date)
    except ValueError:
        return f"[note] since_date must be YYYY-MM-DD, got {since_date!r}."
    try:
        end = (_dt.date.today() + _dt.timedelta(days=1)).isoformat()
        df = yf.Ticker(ticker).history(start=start.isoformat(), end=end, auto_adjust=False)
        if df is None or df.empty or len(df) < 2:
            return f"[note] Not enough price data for {ticker} since {since_date}."
        first, last = float(df["Close"].iloc[0]), float(df["Close"].iloc[-1])
        pct = (last - first) / first * 100 if first else 0.0
        verdict = ("up" if pct > 1 else "down" if pct < -1 else "roughly flat")
        return (f"{ticker} realized return since {since_date}: {pct:+.1f}% "
                f"({first:.2f} -> {last:.2f}, {verdict}). A BUY made then would be "
                f"{'validated' if pct > 1 else 'refuted' if pct < -1 else 'neutral'}.")
    except Exception as e:
        return f"[note] realized return unavailable for {ticker}: {e}"
