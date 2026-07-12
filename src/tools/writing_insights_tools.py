"""Read-only bridge from the REVIEW agent back to the WRITING pipeline (the closed
loop). The daily 复盘官 distills lessons into ``writing_insights.json`` (via
wechat_analytics_tools.save_writing_insight); the 选题官 / 主笔 read them here BEFORE
choosing a topic / writing, so each day's output compounds on what worked.

Same file the review agent writes to (env ``WECHAT_INSIGHTS_FILE`` overrides the
default ``./writing_insights.json``), so both agents just need to run in the same
working directory to share the loop.

Conventions: ``from tool_registry import tool``; every top-level ``def`` is a tool,
so all helpers are LAMBDAS. Read-only; returns strings and never raises.
"""
from tool_registry import tool

import os
import json

_ins_path = lambda: os.environ.get(
    "WECHAT_INSIGHTS_FILE", os.path.join(os.getcwd(), "writing_insights.json"))
_load = lambda: (json.load(open(_ins_path(), encoding="utf-8"))
                 if os.path.exists(_ins_path()) else [])


@tool
def get_writing_insights(limit: int = 20, tag: str = "") -> str:
    """Read the accumulated 复盘 lessons (writing_insights.json) so you can apply
    what has worked and avoid what hasn't. Call this BEFORE choosing today's topic
    (选题) and BEFORE writing (写稿). Most recent first.

    Args:
        limit: max lessons to return (default 20).
        tag: optional filter, e.g. 标题 / 选题 / 赛道 / 合规 (blank = all).
    """
    try:
        rows = _load()
    except Exception as e:  # noqa: BLE001
        return f"[ERROR] Could not read insights: {e}"
    if not rows:
        return ("[none] 还没有复盘经验（复盘 Agent 跑过后这里才有内容）。先按写作风格 "
                "Skill 正常创作即可。")
    if tag.strip():
        rows = [r for r in rows if tag.strip() in (r.get("tag", "") or "")]
    rows = rows[-int(limit):][::-1]
    if not rows:
        return f"[none] 没有标签为『{tag}』的经验。"
    return "近期复盘经验（越新越靠前，写作时务必参考）：\n" + "\n".join(
        f"- [{r.get('tag','')}] {r.get('insight','')}  ({r.get('date','')})"
        for r in rows)
