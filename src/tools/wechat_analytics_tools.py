"""Analytics toolset for the WeChat content factory's REVIEW / OPTIMIZE agent
(PPTX 第八步: 复盘 / 优化 / 技巧).

Lets the daily 复盘官 pull real performance signals so its optimization advice is
data-driven, and persist distilled lessons for the writing side to reuse.

Data sources:
  * WeChat official statistics (datacube) — per-article read/share numbers. Needs
    ``WECHAT_APPID`` / ``WECHAT_SECRET`` in the env and the server IP on the account
    allow-list. Only meaningful for articles that were mass-sent (群发); degrades
    gracefully when unavailable.
  * A local ``performance_log.json`` you (or another job) maintain: a list of
    ``{date, topic, title, reads, likes, shares}`` — the reliable fallback.
  * ``writing_insights.json`` — where distilled lessons are appended for reuse.

Conventions: ``from tool_registry import tool``; every top-level ``def`` is a tool,
so all helpers are LAMBDAS. Tools return strings and never raise. Uses ``requests``.
"""
from tool_registry import tool

import os
import json
import time
import requests

_API = "https://api.weixin.qq.com/cgi-bin"
_creds = lambda: (os.environ.get("WECHAT_APPID", "").strip(),
                  os.environ.get("WECHAT_SECRET", "").strip())
_token = lambda: requests.get(
    _API + "/token",
    params={"grant_type": "client_credential",
            "appid": _creds()[0], "secret": _creds()[1]},
    timeout=30).json().get("access_token", "")
_perf_path = lambda: os.environ.get(
    "WECHAT_PERF_FILE", os.path.join(os.getcwd(), "performance_log.json"))
_ins_path = lambda: os.environ.get(
    "WECHAT_INSIGHTS_FILE", os.path.join(os.getcwd(), "writing_insights.json"))
_load_json = lambda p: (json.load(open(p, encoding="utf-8"))
                        if os.path.exists(p) else [])


@tool
def fetch_article_stats(begin_date: str, end_date: str = "") -> str:
    """Fetch per-article read/share stats from WeChat's official statistics
    (datacube/getarticletotal). Use it to see which recently mass-sent articles
    performed well. Returns raw-ish JSON summary, or a clear message if stats are
    unavailable (e.g. only drafts exist, or no permission).

    Args:
        begin_date: start date 'YYYY-MM-DD' (datacube allows a limited range).
        end_date: end date 'YYYY-MM-DD' (defaults to begin_date).
    """
    tok = _token()
    if not tok:
        return "[ERROR] No access token — check WECHAT_APPID / WECHAT_SECRET / IP allow-list."
    body = {"begin_date": begin_date, "end_date": end_date or begin_date}
    try:
        r = requests.post(_API + "/datacube/getarticletotal",
                          params={"access_token": tok},
                          data=json.dumps(body).encode("utf-8"),
                          headers={"Content-Type": "application/json"},
                          timeout=60).json()
    except Exception as e:  # noqa: BLE001
        return f"[ERROR] datacube request failed: {e}"
    rows = r.get("list")
    if rows is None:
        return ("[unavailable] WeChat returned: "
                + json.dumps(r, ensure_ascii=False)
                + "  (Stats need mass-sent articles + permission; use load_performance_log instead.)")
    return "[stats]\n" + json.dumps(rows, ensure_ascii=False)[:12000]


@tool
def load_performance_log(limit: int = 30) -> str:
    """Load the local performance log (performance_log.json): recent articles with
    reads / likes / shares. This is the reliable, always-available source for
    复盘 — analyze it to find which topics, titles and sub-niches performed best.

    Args:
        limit: max recent rows to return (default 30).
    """
    try:
        rows = _load_json(_perf_path())
    except Exception as e:  # noqa: BLE001
        return f"[ERROR] Could not read performance log: {e}"
    if not rows:
        return ("[none] performance_log.json is empty. Create it as a JSON list of "
                "{date, topic, title, reads, likes, shares} to enable data-driven 复盘.")
    rows = rows[-int(limit):]
    return "[performance]\n" + json.dumps(rows, ensure_ascii=False, indent=2)[:12000]


@tool(risk="high")
def save_writing_insight(insight: str, tag: str = "") -> str:
    """Persist ONE distilled, actionable lesson to writing_insights.json so the
    writing side can reuse it (e.g. "标题带具体额度数字的打开率更高"). Call this for
    each key takeaway from today's 复盘.

    Args:
        insight: the actionable lesson, one sentence.
        tag: optional category, e.g. 标题 / 选题 / 赛道 / 合规.
    """
    if not (insight or "").strip():
        return "[ERROR] Empty insight — nothing saved."
    try:
        rows = _load_json(_ins_path())
        rows.append({"insight": insight.strip(), "tag": (tag or "").strip(),
                     "date": time.strftime("%Y-%m-%d %H:%M")})
        with open(_ins_path(), "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=2)
        return f"[saved] insight recorded ({len(rows)} total)."
    except Exception as e:  # noqa: BLE001
        return f"[ERROR] Could not save insight: {e}"


@tool
def list_writing_insights(limit: int = 20) -> str:
    """List the distilled writing insights collected so far (most recent first).
    Recall these at the start of a 复盘 so advice compounds instead of repeating.

    Args:
        limit: max entries to show (default 20).
    """
    try:
        rows = _load_json(_ins_path())
    except Exception as e:  # noqa: BLE001
        return f"[ERROR] Could not read insights: {e}"
    if not rows:
        return "[none] No insights yet."
    rows = rows[-int(limit):][::-1]
    return "\n".join(f"- [{r.get('tag','')}] {r.get('insight','')}"
                     f"  ({r.get('date','')})" for r in rows)
