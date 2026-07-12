"""Current-date anchor for the content factory.

LLMs have no reliable sense of "today" and tend to invent stale years (e.g. writing
"2025下半年" in mid-2026). This tool returns the machine's real local date so the
Scout/Researcher/Writer can anchor topic recency and any in-text time wording to
reality.

Conventions: ``from tool_registry import tool``; every top-level ``def`` is a tool,
so all helpers are LAMBDAS. Tools return strings and never raise.
"""
from tool_registry import tool

import datetime as _dt

_WD = ["一", "二", "三", "四", "五", "六", "日"]


@tool
def current_date() -> str:
    """Return TODAY's real date (local time). Call this FIRST when selecting topics or
    writing, and anchor everything to it: prefer news/updates from the last ~7 days,
    and make every in-text year/time match this real date — never write a stale year
    like "2025" when it is actually 2026.
    """
    try:
        now = _dt.datetime.now()
        wd = _WD[now.weekday()]
        wk_ago = (now - _dt.timedelta(days=7)).strftime("%Y-%m-%d")
        return (f"今天是 {now.strftime('%Y年%m月%d日')}（{now.strftime('%Y-%m-%d')}，周{wd}）。"
                f"选题请优先最近 7 天（{wk_ago} 至今）的资讯；"
                f"正文里任何年份/时间表述都要以此为准，严禁臆造过时年份。")
    except Exception as e:  # noqa: BLE001
        return f"[ERROR] current_date failed: {e}"
