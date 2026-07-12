# -*- coding: utf-8 -*-
"""Build the Furniture Ad Collector .mta + generated agent.

Chain: Collector (scrape ad images) -> Curator (VLM-classify each & file by category).
Fully automatic, no HITL. China-friendly + SiliconFlow only (text LLM + vision tool).
Two triggers: a stylish custom desktop GUI (paste a site / pick a preset) and a daily
schedule (crawl trending furniture sites).
"""
import os, sys, json
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import graph_model
import graph_codegen

P = graph_model.default_props

# ── load the custom GUI source (inlined into the GUI node) ──────────────────
GUI_SRC = open(os.path.join(ROOT, "prototype", "custom_gui",
                            "furniture_gallery_gui.py"), "r", encoding="utf-8").read()

# ── prompts ─────────────────────────────────────────────────────────────────
PROMPT_COLLECT = """你是「采集员」。任务里通常会给一个或多个家具网站链接；也可能没给，那就自己取热门站。
工作流程：
1. 若任务里【没有】明确网址，先调用 list_trending_furniture_sites 拿到热门家具站清单（Castlery、Maisons du Monde 等）。
2. 对每个目标网站调用 scrape_ad_images 抓取广告/产品图（图片会自动下载到本地 inbox 文件夹）。一次一个站点。
3. 汇总所有【成功保存】的本地图片路径。
输出：把所有已下载图片的本地路径写入共享状态 `images`，每行一个路径；并简述从哪些站点各抓到几张。
红线：只输出工具真实返回的本地路径，绝不编造；某站点无图或抓取失败就跳过并说明原因。"""

PROMPT_CURATE = """你是「归类员」，用视觉大模型把采集到的家具广告图分门别类地归档保存。
针对共享状态 `images` 里的【每一张】图片路径，逐张处理：
1. 调用 classify_image(该路径)，得到 category（英文 slug，如 sofa/bed/dining/…）和一句中文 caption。
2. 调用 organize_image(该路径, category, caption)，把这张图移入对应类目的文件夹并记录描述。
把 images 里的图片全部处理完后，调用一次 list_collected 得到各类目的张数统计。
输出：把归档结果写入 `report`——逐条说明「某图 → 归入某类目（caption）」，末尾附上 list_collected 的类目统计总览。
红线：逐张处理不得遗漏；category 必须采用 classify_image 的返回值；只处理真实存在的文件，跳过不存在的路径并说明。"""

SCHEDULE_TASK = ("定时任务：抓取当下热门家具网站（Article、Castlery(/us)、Maisons du Monde 等）"
                 "的最新广告/产品图并自动分门别类归档。请先调用 list_trending_furniture_sites "
                 "取站点清单（已用可正常渲染的国家/地区 URL），再逐站 scrape_ad_images，"
                 "最后逐张 classify_image + organize_image。")

# ── nodes ───────────────────────────────────────────────────────────────────
def node(nid, kind, name, x, y, **props):
    p = P(kind)
    p.update(props)
    return {"id": nid, "kind": kind, "name": name, "x": x, "y": y, "props": p}

nodes = [
    # ── stages (chain) ──
    node("agent_collect", "agent", "Collector", 0, 0,
         role="single", writes=["images"]),
    node("agent_curate", "agent", "Curator", 360, 0,
         role="single", reads=["images"], writes=["report"]),
    # ── LLM (shared) ──
    node("llm_main", "llm", "LLM-Main", 0, 220),
    # ── prompts ──
    node("prompt_collect", "prompt", "P-Collect", 0, -180, role="single", text=PROMPT_COLLECT),
    node("prompt_curate", "prompt", "P-Curate", 360, -180, role="single", text=PROMPT_CURATE),
    # ── tools ──
    node("tool_collect", "tool", "T-Collect", 0, 360,
         files=["furniture_scrape_tools.py"]),
    node("tool_curate", "tool", "T-Curate", 360, 360,
         files=["vision_classify_tools.py", "image_organize_tools.py"],
         tool_props={"organize_image": {"risk": "high"}}),
    # ── emitters ──
    node("sched_daily", "schedule", "DailyCrawl", -300, 120,
         mode="daily", at="09:00", initial_task=SCHEDULE_TASK, run_at_start=False),
    node("gui_desktop", "gui", "GalleryGUI", -300, -120, custom_gui=GUI_SRC),
]

edges = [
    # flow
    {"src": "agent_collect", "dst": "agent_curate", "props": {}},
    # resources
    {"src": "llm_main", "dst": "agent_collect", "props": {}},
    {"src": "llm_main", "dst": "agent_curate", "props": {}},
    {"src": "prompt_collect", "dst": "agent_collect", "props": {}},
    {"src": "prompt_curate", "dst": "agent_curate", "props": {}},
    {"src": "tool_collect", "dst": "agent_collect", "props": {}},
    {"src": "tool_curate", "dst": "agent_curate", "props": {}},
    # emitters (both drive the entry agent)
    {"src": "sched_daily", "dst": "agent_collect", "props": {}},
    {"src": "gui_desktop", "dst": "agent_collect", "props": {}},
]

state_schema = [
    {"name": "images", "type": "str", "reducer": "overwrite", "default": "",
     "description": "采集员下载到本地的广告图路径（每行一个）。"},
    {"name": "report", "type": "str", "reducer": "overwrite", "default": "",
     "description": "归类员的归档结果与各类目张数统计。"},
]

data = {
    "nodes": nodes,
    "edges": edges,
    "state_schema": state_schema,
    "type_defs": {},
    "recursion_limit": 60,
    "run_wall_clock_s": 0,
    "storage": {"backend": "disk"},
}

g = graph_model.Graph.from_dict(data)
info = graph_codegen.analyze(g)
print("MODE:", info.get("mode"), "| ENTRY:", info.get("entry"))
print("ERRORS:", json.dumps(info.get("errors", []), ensure_ascii=False))
print("WARNINGS:", json.dumps(info.get("warnings", []), ensure_ascii=False))

if not info.get("errors"):
    out = os.path.join(ROOT, "graphs", "FurnitureAdCollector.mta")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    graph_model.save_mta(g, out, os.path.join(ROOT, "tools"))
    print("SAVED:", out)
    outdir = graph_codegen.generate_from_graph(g, "FurnitureAdCollector",
                                               gui=True, code_style="single")
    print("GENERATED:", outdir)
else:
    print("NOT SAVED — fix errors first.")
