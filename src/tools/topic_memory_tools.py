"""Written-topic memory for the AI WeChat content factory.

Keeps a persistent JSON log of topics already turned into articles so the daily
run never repeats itself (PPTX 第八步: "写过的主题保存到某个文件防止重复写" — repeating
a recent topic can trigger WeChat's low-originality penalty).

Storage: a ``written_topics.json`` file in the process working directory (next to
the running agent). Override with env ``WECHAT_TOPICS_FILE``.

Conventions: ``from tool_registry import tool``; every top-level ``def`` is a tool,
so all helpers are LAMBDAS. Tools return strings and never raise.
"""
from tool_registry import tool

import os
import json
import time

_path = lambda: os.environ.get(
    "WECHAT_TOPICS_FILE", os.path.join(os.getcwd(), "written_topics.json"))
_load = lambda: (json.load(open(_path(), encoding="utf-8"))
                 if os.path.exists(_path()) else [])
_norm = lambda s: "".join((s or "").lower().split())


@tool
def list_written_topics(limit: int = 50) -> str:
    """List topics already written & saved (most recent first) so you can avoid
    duplicates when choosing today's topic.

    Args:
        limit: max entries to show (default 50).
    """
    try:
        rows = _load()
    except Exception as e:  # noqa: BLE001
        return f"[ERROR] Could not read topics file: {e}"
    if not rows:
        return "[none] No topics written yet — anything is fair game."
    rows = rows[-int(limit):][::-1]
    return "\n".join(f"- {r.get('date','?')}: {r.get('topic','')}"
                     f"  (title: {r.get('title','')})" for r in rows)


@tool
def is_topic_written(topic: str) -> str:
    """Check whether a topic (or a very similar one) has already been written.
    Returns 'YES — already written ...' or 'NO — new topic'. Call this BEFORE
    committing to a topic.

    Args:
        topic: the candidate topic / title to check.
    """
    key = _norm(topic)
    if not key:
        return "[ERROR] Empty topic."
    try:
        rows = _load()
    except Exception as e:  # noqa: BLE001
        return f"[ERROR] Could not read topics file: {e}"
    for r in rows:
        rk = _norm(r.get("topic", ""))
        if rk and (rk == key or rk in key or key in rk):
            return f"YES — already written on {r.get('date','?')}: {r.get('topic','')}"
    return "NO — new topic, safe to write."


@tool(risk="high")
def save_written_topic(topic: str, title: str = "") -> str:
    """Record a topic as written so future runs won't repeat it. Call this once,
    AFTER the article has been successfully sent to the WeChat draft box.

    Args:
        topic: the topic that was written.
        title: the final article title (optional, for the log).
    """
    if not (topic or "").strip():
        return "[ERROR] Empty topic — nothing saved."
    try:
        rows = _load()
        rows.append({"topic": topic.strip(), "title": (title or "").strip(),
                     "date": time.strftime("%Y-%m-%d %H:%M")})
        with open(_path(), "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=2)
        return f"[saved] '{topic.strip()}' recorded ({len(rows)} total)."
    except Exception as e:  # noqa: BLE001
        return f"[ERROR] Could not save topic: {e}"
