# -*- coding: utf-8 -*-
"""Build the AI WeChat Content Factory .mta from the PPTX design.
Fully automatic (schedule-driven), no HITL; a single LLM reviewer gates the article.
"""
import os, sys, json
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import graph_model
import graph_codegen

P = graph_model.default_props  # shorthand for kind defaults

# ── prompts ────────────────────────────────────────────────────────────────
PROMPT_SCOUT = """你是「选题官」。每天为一个垂直领域的微信公众号挑选今天要写的 3 个选题（供人工从中择优）。
工作流程：
0. 先调用 current_date 确认今天的真实日期，据此判断时效——只选最近约 7 天内的新闻/动态，过期热点一律不选。
1. 先调用 get_writing_insights 查看复盘官沉淀的经验（哪些选题方向/赛道更受欢迎、要避开什么），据此定选题倾向。
2. 用 fetch_many_feeds / fetch_feed_articles / fetch_wemp_feed 抓取你负责赛道的竞品公众号与新闻源的最新文章，汇总候选选题（feed 地址见本次运行任务说明）。
3. 从候选中筛出「读者有明确问题、可结构化解答、时效性强、竞品已验证有人看」的选题。
4. 对候选逐个调用 is_topic_written 去重，并参考 list_written_topics 的历史；已写过或高度相似的排除。
5. 最终选定 3 个「互不重复、各有角度」的选题（优先契合复盘经验的方向）。
输出：把 3 个选题写入共享状态字段 `topic`，严格用如下格式（每行一个，含主题+角度）：
1) 主题 —— 角度
2) 主题 —— 角度
3) 主题 —— 角度
并简要说明各自为何值得写。
红线：3 个必须互不相同、都明确、都不与近期写过的主题重复。"""

PROMPT_RESEARCH = """你是「资料员」。针对共享状态 `topic` 里的 3 个选题，逐个用联网搜索 + 阅读原文收集可信、具体、最新的素材，并生成配图。
先调用 current_date 确认今天日期，检索时优先最近一周的资料，判断信息是否过时。
对【每一个】选题都执行：
1. 用 cn_web_search（国内可直连的免 key 搜索：Bing 国内版优先、百度兜底）找到 3-6 个高质量来源。注意：本 Agent 已停用内置 web_search（DuckDuckGo 在国内无法访问），一律用 cn_web_search。
2. 用 read_web_page 逐个读取，提炼关键事实/数据/观点/案例（要具体，不要空话）。若 cn_web_search 偶发失败，就直接对已知来源（如 RSS 给出的链接）用 read_web_page。
3. 配图：为该篇生成【至少 3 张】真实图片——首选 generate_image（SiliconFlow→Kolors，免费）按内容画插画/示意图/封面；每次调用都会返回一个真实的本地路径（在 article_images/ 下）。每篇务必生成 1 张适合当封面的图。（若配了 PEXELS_API_KEY，也可用 search_images + download_image 取实拍图。）
输出：把 3 个选题的素材写入共享状态字段 `research`，【按选题分块】（用「## 选题1 / ## 选题2 / ## 选题3」分隔）；每块必须包含：要点清单 + 关键数据 + 来源链接 + 一个『图片清单』，逐行列出【每张图的本地绝对/相对路径 + 该图用途/说明】，并明确标注哪张是封面。
红线：只用能核实的事实并标注来源；图片路径必须是工具真实返回的本地路径，严禁编造。"""

PROMPT_WRITER = """你是「主笔」。根据共享状态里的 `topic`（3 个选题）与 `research`，为每个选题各写一篇可直接进草稿箱的微信公众号文章，共 3 篇（供人工择优）。
动笔前先调用 current_date 确认今天真实日期：标题与正文里任何年份/时间表述都必须与当前日期一致（如现在是 2026 年就写 2026），严禁出现“2025下半年”这类过时年份；除非确有必要，标题尽量不带年份。
再调用 get_writing_insights 查看复盘官沉淀的经验（标题该怎么写、结构怎么调、要避开什么），把这些经验落实到每一篇。
必须严格遵循附加的『公众号写作风格 Skill』（标题公式、正文结构、语气、排版）。
每篇要求：
- 标题要吸引人、与读者强相关、制造点击欲。
- 正文用 HTML 片段（<h2>/<p>/<section>/<img> 等），图文并茂，至少 3 张图。【硬性要求】<img src> 只能使用该篇对应选题在 research『图片清单』里真实列出的本地路径，逐字照抄，绝不许自己编造或猜测文件名/路径；清单里没有的图就不要插 <img>（草稿管家稍后会把这些本地路径换成微信图片 URL）。
- 结构清晰、信息密度高、要有自己的观点或附加值（微信不反对 AIGC，但要有增量价值），且是你自己看得下去的质量。不要洗稿、不要空话套话、不要广告。
- 若共享状态里的 `feedback` 非空，说明上一稿被终审打回，请逐条针对 feedback 修改后重写这 3 篇。
输出格式（务必严格，草稿管家靠它切分 3 篇）：把 3 篇写入 `article`，每篇用分隔行包起来——
===ARTICLE 1===
TITLE: <该篇标题>
<该篇正文 HTML>
===ARTICLE 2===
TITLE: <该篇标题>
<该篇正文 HTML>
===ARTICLE 3===
TITLE: <该篇标题>
<该篇正文 HTML>
另外把 3 个标题（每行一个）写入 `title`。"""

PROMPT_REVIEW = """你是「终审编辑」，也是这条全自动流水线里唯一的质量闸门（无人工审核）。审阅共享状态里的 `article`（3 篇，用 ===ARTICLE N=== 分隔）与 `title`，逐篇判断是否达到可进草稿箱的标准。
每篇的评审清单：
1. 标题是否足够吸引人、与读者相关；
2. 正文是否结构清晰、信息具体、有增量价值（非洗稿、非空话）；
3. 是否图文并茂（≥3 张图）；
4. 合规：不含广告/诱导关注/敏感词；
5. 事实是否有来源支撑、无明显硬伤。
决策：
- 3 篇全部达标：把 `approved` 设为 true，`feedback` 写「通过」。
- 有任一篇不达标：把 `approved` 设为 false，并在 `feedback` 里【按篇、逐条、可执行】给出修改意见供主笔重写。
只审阅不改稿。务必写入 `approved` 和 `feedback` 两个字段。"""

PROMPT_PUBLISH = """你是「草稿管家」。把定稿的 3 篇文章分别存入微信公众号【草稿箱】，供人工进后台从中择优。注意：只存草稿，绝不群发、绝不发布给粉丝（本流水线不含任何发布动作）。要稳健，尽量把 3 篇都存进去，不要因为个别图片就让某篇卡住。
流程：先从 `article` 按 ===ARTICLE N=== 分隔切出 3 篇；对【每一篇】依次执行：
1. 取该篇的 TITLE 与正文 HTML。
2. 逐张处理正文图片：调用 upload_content_image 上传得到微信图片 URL，把 HTML 中对应的本地路径 <img src> 替换为该 URL（微信会过滤外链图，必须替换）。若某张返回 [ERROR]（文件不存在等），就直接把这张 <img> 标签从 HTML 中删掉、继续处理其余图片，不要中止。
3. 准备封面（草稿必须有封面 thumb_media_id）：优先选该篇一张已成功上传的图，用 upload_cover_image 得到 thumb_media_id；若一张可用图都没有，就用 generate_image 按该篇标题现画一张封面，再 upload_cover_image。
4. 调用 create_wechat_draft(title, 处理后的 content_html, thumb_media_id, digest) 为该篇创建【草稿】。
5. 该篇成功后调用 save_written_topic(该篇主题, title) 记录，防止以后重复。
对 3 篇全部执行完。输出：逐篇报告草稿创建结果（media_id）、主题、被跳过的图片数量；哪篇失败就说明原因（仅当某篇连封面都无法产生时才判定该篇失败）。全部只进草稿箱，不发布。"""

SKILL_STYLE_TEXT = """【公众号写作风格 Skill —— 赛道：免费薅 AI 羊毛 / AI 工具白嫖】
本 Skill 由「免费薅 AI 羊毛」系列前 3 篇（① GitHub Copilot 白嫖 → ② DeepSeek R1 三种姿势 → ③ 10 家大模型免费额度实测）复盘沉淀而来，请把它当成写这个号的「统一模板」。
定位：帮读者「零成本用上各种 AI 平台、模型、算力、额度」。核心情绪：白嫖的快乐 + FOMO（别人都薅到了就你还没）。人设：一个爱折腾、乐于分享、踩过坑的 AI 玩家，说人话但懂技术。

一、写作全流程（动笔前按此走一遍，别跳步）：
1. 锚定当下：先用 current_date 确认今天真实日期；只写近一周的热点，正文里的年份/时间一律用真实当前年份（现在是 2026 就写 2026，严禁出现「2025 下半年」这类穿越）。
2. 参考复盘：调用 get_writing_insights，看哪些选题方向/标题写法更受欢迎、要避开什么，落实到本篇。
3. 立选题定角度：一句话说清「主题 + 角度」，确认读者有明确问题、能结构化解答、时效强；用 is_topic_written 去重，不写近期重复过的。
4. 找料 + 实测：用 cn_web_search + read_web_page 找可核实的事实/数据/来源；能自己上手实测一下就实测（哪怕一句「我实测上传 200 页 PDF，3 秒出结果」），实测感是这个号的灵魂。
5. 配图：每篇 ≥3 张，含 1 张封面；用 generate_image 生成，风格见第六节。
6. 按「标题公式 + 正文结构」成稿，最后对照第七节红线自检。

二、标题公式（信息密度高、直给结论、制造点击欲，任选其一混搭）：
- 反差/权威背书：「连英伟达都开始送算力了！上百个大模型免费薅」
- 数字 + 福利 + 换算：「白嫖 XX 元代金券 ≈ 1600 万 Token，够用一整年」
- 悬念 + 相关性：「白嫖的大模型能干嘛？我拿它搭了条全自动『绘本生产线』」
- 结论前置 + 吐槽：「我把 10 家 AI 免费额度薅了个遍，结论：充钱的是大冤种」
- 紧迫/限时：「趁还没关，XX 的免费额度赶紧领」；口语钩子：「别再花钱买 XX 了，这个平台白送」
禁止：标题党但内容对不上、夸大到不可复现、带过时年份。标题要能兑现。

三、正文结构（每篇尽量一致，读者与算法都好识别）：
1. 承接 + 钩子：开头用一句「扎心提问」或「反差事实」抓人（例：「你手机里是不是有个 AI 正按月扣你钱？」）；若属系列，一句话接上前几篇（「前两篇我们薅了 X 和 Y，这次来个大的——」）。30 秒内让读者觉得「这波我不亏」。
2. 这是什么 & 值多少：平台/模型一句话说清；把免费额度换算成「能干多少活」（如 16 元≈1600 万 Token、每天上千次随便调），价值可感知。
3. 主体（按题材选其一）：
   · 「盘点/横评」型：分组（如 国外 / 国内），每项用 🥇🥈🥉 分级 + 一句话辛辣点评 + 一个具体额度/实测数字；主体后给「推荐指数（五星制）」一栏收口。
   · 「单品手把手」型：分步骤（注册 → 认证 → 拿 API-Key），每关键步配图，写清坑点（+86 手机号可用、Key 只显示一次要立刻复制、IP 白名单等），步骤要「照着做就能成」。
4. 白嫖组合拳：给一套「谁配谁最香」的实用搭配（如 日常长文→通义/Kimi、写码推理→DeepSeek、生图→豆包、海外尝鲜→Gemini），让读者领完立刻能用、有正反馈。
5. 避坑 & 大实话：诚实吐槽真实限制（免费额度随时变、免费版一般不给旗舰、别把敏感数据丢免费 API），当朋友提醒，建立信任。
6. 写在最后：一句话总结「这波能薅到啥」+ 系列串联（「三篇下来你已白嫖了：✅…✅…」）+ 自然互动（在看/收藏/评论区聊）+ 预告下一篇。

四、语气 & 金句（这个号的辨识度）：诙谐、有人情味、口语化，多短句、敢下结论。善用「拟人化点评」把干巴巴的参数写活（例：Claude「手艺一流，额度感人，应急专用」；Copilot「像个刚入职很守规矩的实习生」；省下的钱「够加好几顿鸡腿」）。「薅羊毛 / 白嫖 / 这波不亏 / 大冤种」等梗适量不刷屏；专有名词（LLM / API-Key / Token / Multi-Agent 等）要出现但顺手用一句大白话解释。

五、排版：emoji 小标题（如「💰」「🌍」「🇨🇳」「⚠️」「🎯」）、有序步骤、要点列表、加粗关键结论与数字；图文并茂，关键步骤/效果处必配图；链接、代金券、下载地址等重点信息用醒目方式单独放。正文用 HTML 片段（<h2>/<h3>/<p>/<ul>/<img> 等）。

六、配图风格（避免生图翻车）：统一走「现代扁平科技插画、蓝紫渐变、干净有质感」；用图形隐喻表达主题（礼盒=大礼包、金银铜奖台=排行、金币宝山/宝箱=白嫖收益、悬浮 App 图标=各家模型）；【务必】图里不要放中文文字（生图会把汉字画成乱码），需要「概念」就用图形而非文字；每篇 ≥3 张、含 1 张封面。

七、红线（务必遵守）：只写真实、可复现的羊毛；给官方真实链接、标注来源；不夸大、不承诺收益；不洗稿、不硬广、不过度诱导；不写近期重复过的主题；时间/年份必须与当前真实日期一致；每篇都要有自己的实测/观点/附加值（微信不反对 AIGC，但要有增量）。"""

SCHEDULE_TASK = """开始今天的公众号选题与写作。
目标赛道：免费薅 AI 羊毛 / AI 工具白嫖（免费额度、代金券、免费模型/算力、0 成本用法）。
竞品/新闻 RSS 源（逗号分隔，可继续加 we-mp-rss / wechat2rss 订阅的同赛道公众号）：https://wechat2rss.xlab.app/feed/a1cd365aa14ed7d64cabfc8aa086da40ecaba34d.xml, https://wechat2rss.xlab.app/feed/51e92aad2728acdd1fda7314be32b16639353001.xml, https://www.oschina.net/news/rss, https://www.solidot.org/index.rss, https://sspai.com/feed, https://www.ruanyifeng.com/blog/atom.xml 。
流程：抓取竞品最新文章 → 去重后选定 3 个今日选题 → 分别检索资料并生成≥3张配图 → 按写作风格 Skill 各写 1 篇（共 3 篇）→ 终审 → 通过后把 3 篇分别存入公众号草稿箱（只存草稿、不群发）→ 记录选题。"""


def node(nid, kind, name, x, y, **props):
    p = P(kind)
    p.update(props)
    return {"id": nid, "kind": kind, "name": name, "x": x, "y": y, "props": p}


nodes = [
    # ── stages ──
    node("agent_scout", "agent", "TopicScout", 0, 0,
         role="single", writes=["topic"], require_writes=True),
    node("agent_research", "agent", "Researcher", 320, 0,
         role="single", writes=["research"], web_search=False),
    node("agent_writer", "agent", "Writer", 640, 0,
         role="single", writes=["title", "article"], require_writes=True),
    node("agent_review", "agent", "Reviewer", 960, 0,
         role="single", writes=["approved", "feedback"], require_writes=True),
    node("agent_publish", "agent", "DraftSaver", 1280, -120,
         role="single", reads=["title", "article", "topic"]),
    # ── control ──
    node("cond_gate", "condition", "ReviewGate", 1120, 40, branches=[
        {"expr": "approved or revise_count >= 1", "to": "DraftSaver"},
        {"expr": "", "to": "ReviseBump"},
    ]),
    node("set_bump", "setstate", "ReviseBump", 960, 180,
         assignments=[{"field": "revise_count", "value": "=revise_count + 1"}]),
    node("end_done", "end", "Done", 1520, -120),
    # ── LLMs ──
    node("llm_main", "llm", "LLM-Main", 0, 200),
    node("llm_writer", "llm", "LLM-Writer", 640, 200, temperature="0.7"),
    node("llm_review", "llm", "LLM-Reviewer", 960, 320, temperature="0.2"),
    # ── prompts ──
    node("prompt_scout", "prompt", "P-Scout", 0, -180, role="single", text=PROMPT_SCOUT),
    node("prompt_research", "prompt", "P-Research", 320, -180, role="single", text=PROMPT_RESEARCH),
    node("prompt_writer", "prompt", "P-Writer", 640, -180, role="single", text=PROMPT_WRITER),
    node("prompt_review", "prompt", "P-Review", 960, -180, role="single", text=PROMPT_REVIEW),
    node("prompt_publish", "prompt", "P-Publish", 1280, -300, role="single", text=PROMPT_PUBLISH),
    # ── skill ──
    node("skill_style", "skill", "WritingStyle", 500, 200, skills=[{
        "name": "gongzhonghao_style",
        "description": "微信公众号写作风格（标题公式+正文结构+语气+排版），由竞品蒸馏得到，写稿时必须遵循。",
        "text": SKILL_STYLE_TEXT,
    }]),
    # ── tools ──
    node("tool_scout", "tool", "T-Scout", 0, 360,
         files=["wechat_rss_tools.py", "topic_memory_tools.py",
                "writing_insights_tools.py", "datetime_tools.py"]),
    node("tool_research", "tool", "T-Research", 320, 200,
         files=["research_tools.py", "image_gen_tools.py", "cn_search_tools.py",
                "datetime_tools.py"]),
    node("tool_writer", "tool", "T-Writer", 640, 360,
         files=["writing_insights_tools.py", "datetime_tools.py"]),
    node("tool_publish", "tool", "T-Publish", 1280, 120,
         files=["wechat_publish_tools.py", "topic_memory_tools.py",
                "image_gen_tools.py"],
         tool_props={"create_wechat_draft": {"risk": "high"},
                     "save_written_topic": {"risk": "high"}}),
    # ── emitters ──
    node("sched_daily", "schedule", "DailyRun", -260, 0,
         mode="daily", at="08:00", initial_task=SCHEDULE_TASK, run_at_start=False),
    node("gui_desktop", "gui", "DesktopGUI", -260, -160),
]

edges = [
    # flow
    {"src": "agent_scout", "dst": "agent_research", "props": {}},
    {"src": "agent_research", "dst": "agent_writer", "props": {}},
    {"src": "agent_writer", "dst": "agent_review", "props": {}},
    {"src": "agent_review", "dst": "cond_gate", "props": {}},
    {"src": "cond_gate", "dst": "agent_publish", "props": {}},
    {"src": "cond_gate", "dst": "set_bump", "props": {}},
    {"src": "set_bump", "dst": "agent_writer", "props": {}},
    {"src": "agent_publish", "dst": "end_done", "props": {}},
    # resources
    {"src": "llm_main", "dst": "agent_scout", "props": {}},
    {"src": "llm_main", "dst": "agent_research", "props": {}},
    {"src": "llm_main", "dst": "agent_publish", "props": {}},
    {"src": "llm_writer", "dst": "agent_writer", "props": {}},
    {"src": "llm_review", "dst": "agent_review", "props": {}},
    {"src": "prompt_scout", "dst": "agent_scout", "props": {}},
    {"src": "prompt_research", "dst": "agent_research", "props": {}},
    {"src": "prompt_writer", "dst": "agent_writer", "props": {}},
    {"src": "prompt_review", "dst": "agent_review", "props": {}},
    {"src": "prompt_publish", "dst": "agent_publish", "props": {}},
    {"src": "skill_style", "dst": "agent_writer", "props": {}},
    {"src": "tool_scout", "dst": "agent_scout", "props": {}},
    {"src": "tool_research", "dst": "agent_research", "props": {}},
    {"src": "tool_writer", "dst": "agent_writer", "props": {}},
    {"src": "tool_publish", "dst": "agent_publish", "props": {}},
    # emitters
    {"src": "sched_daily", "dst": "agent_scout", "props": {}},
    {"src": "gui_desktop", "dst": "agent_scout", "props": {}},
]

state_schema = [
    {"name": "topic", "type": "str", "reducer": "overwrite", "default": "",
     "description": "今日选定的 3 个选题（每行一个：主题+角度）。"},
    {"name": "research", "type": "str", "reducer": "overwrite", "default": "",
     "description": "3 个选题各自的素材（按选题分块）：要点/数据/来源/已生成图片本地路径。"},
    {"name": "title", "type": "str", "reducer": "overwrite", "default": "",
     "description": "3 篇文章的标题（每行一个）。"},
    {"name": "article", "type": "str", "reducer": "overwrite", "default": "",
     "description": "3 篇文章正文，用 ===ARTICLE N=== 分隔，每篇含 TITLE 行 + HTML 正文（<img> 为本地路径，存草稿时替换为微信URL）。"},
    {"name": "approved", "type": "bool", "reducer": "overwrite", "default": False,
     "description": "终审是否通过。"},
    {"name": "feedback", "type": "str", "reducer": "overwrite", "default": "",
     "description": "终审的修改意见（供主笔重写）。"},
    {"name": "revise_count", "type": "int", "reducer": "overwrite", "default": 0,
     "description": "已完成的重写轮数（限制修改循环）。"},
]

data = {
    "nodes": nodes,
    "edges": edges,
    "state_schema": state_schema,
    "type_defs": {},
    "recursion_limit": 40,
    "run_wall_clock_s": 0,
    "storage": {"backend": "disk"},
}

g = graph_model.Graph.from_dict(data)
info = graph_codegen.analyze(g)
print("MODE:", info.get("mode"), "| ENTRY:", info.get("entry"))
print("ERRORS:", json.dumps(info.get("errors", []), ensure_ascii=False))
print("WARNINGS:", json.dumps(info.get("warnings", []), ensure_ascii=False))

if not info.get("errors"):
    out = os.path.join(ROOT, "graphs", "AiWeChatContentFactory.mta")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    graph_model.save_mta(g, out, os.path.join(ROOT, "tools"))
    print("SAVED:", out)
else:
    print("NOT SAVED — fix errors first.")
