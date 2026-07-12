# -*- coding: utf-8 -*-
"""Build the standalone daily REVIEW/OPTIMIZE agent (PPTX 第八步) as its own .mta.
A single memory + schedule agent: pulls performance data, distills lessons, and
remembers them so selection/writing improves over time. Fully automatic, no HITL.
"""
import os, sys, json
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import graph_model, graph_codegen

P = graph_model.default_props

PROMPT_REFLECT = """你是「复盘官」，每天给这个『免费薅 AI 羊毛』公众号做复盘与优化，并把经验沉淀下来，让后续选题和写作越来越准。
流程：
1. 先 recall 长期记忆、并用 list_writing_insights 回看已沉淀的经验，避免重复结论。
2. 用 list_written_topics 看最近写了哪些主题；用 load_performance_log / fetch_article_stats 拿到近期文章的阅读、点赞、分享数据（拿不到就用能拿到的部分）。
3. 分析：哪些【选题方向 / 标题风格 / 赛道细分】表现最好、哪些最差；有没有触发限流或低创作的迹象；读者更吃哪类『羊毛』。
4. 用内置 web 搜索快速核对微信最新的规则/敏感词/引流边界变化（官方公告、行业动态），避免踩雷。
5. 产出可执行的优化动作：下一步选题倾向、标题该怎么调、赛道要不要微调、要避开什么。
6. 用 remember 把新经验沉淀进长期记忆；并用 save_writing_insight 把最重要的 1-3 条写进 writing_insights.json，供写作环节复用。
输出：一份简短复盘报告（数据要点 + 结论 + 明确的下一步优化动作）。
红线：结论必须基于数据、可执行；不要空泛地喊『坚持更新』。"""

SCHEDULE_TASK = """开始今天的公众号复盘。目标赛道：免费薅 AI 羊毛 / AI 工具白嫖。
请：回看历史经验与已写主题 → 拉取近期文章表现数据 → 找出表现最好/最差的选题与标题规律 → 核对微信最新规则变化 → 输出下一步可执行的优化动作，并把关键经验写入长期记忆与 writing_insights.json。"""


def node(nid, kind, name, x, y, **props):
    p = P(kind); p.update(props)
    return {"id": nid, "kind": kind, "name": name, "x": x, "y": y, "props": p}


nodes = [
    node("agent_reflect", "agent", "Reflector", 0, 0, role="single", web_search=True),
    node("llm_reflect", "llm", "LLM-Reflect", 0, 200, temperature="0.3"),
    node("prompt_reflect", "prompt", "P-Reflect", 0, -180, role="single", text=PROMPT_REFLECT),
    node("mem_insights", "memory", "Insights", -280, 120,
         description="公众号复盘经验：选题/标题/赛道/合规规律，跨天累积。", top_k=8),
    node("tool_reflect", "tool", "T-Reflect", 300, 120,
         files=["wechat_analytics_tools.py", "topic_memory_tools.py"],
         tool_props={"save_writing_insight": {"risk": "high"}}),
    node("sched_review", "schedule", "DailyReview", -280, -40,
         mode="daily", at="21:00", initial_task=SCHEDULE_TASK, run_at_start=False),
    node("gui_reflect", "gui", "DesktopGUI", -280, -200),
]

edges = [
    {"src": "llm_reflect", "dst": "agent_reflect", "props": {}},
    {"src": "prompt_reflect", "dst": "agent_reflect", "props": {}},
    {"src": "mem_insights", "dst": "agent_reflect", "props": {}},
    {"src": "tool_reflect", "dst": "agent_reflect", "props": {}},
    {"src": "sched_review", "dst": "agent_reflect", "props": {}},
    {"src": "gui_reflect", "dst": "agent_reflect", "props": {}},
]

data = {
    "nodes": nodes, "edges": edges, "state_schema": [], "type_defs": {},
    "recursion_limit": 0, "run_wall_clock_s": 0,
    "storage": {"backend": "disk"},
}

g = graph_model.Graph.from_dict(data)
info = graph_codegen.analyze(g)
print("MODE:", info.get("mode"), "| ENTRY:", info.get("entry"))
print("ERRORS:", json.dumps(info.get("errors", []), ensure_ascii=False))
print("WARNINGS:", json.dumps(info.get("warnings", []), ensure_ascii=False))
if not info.get("errors"):
    out = os.path.join(ROOT, "graphs", "AiWeChatReviewAgent.mta")
    graph_model.save_mta(g, out, os.path.join(ROOT, "tools"))
    print("SAVED:", out)
    graph_codegen.generate_from_graph(g, "AiWeChatReviewAgent")
    print("GENERATED ok")
else:
    print("NOT SAVED — fix errors first.")
