"""Tiny UI translation layer.

`t(text)` returns the current-language rendering of an ENGLISH source string
(English IS the key, so an untranslated string falls back to itself — no missing
labels ever). Currently translates the welcome window; the canvas designer is a
larger surface (translate incrementally by wrapping its strings in t() and adding
entries to _ZH). Language is persisted in app_config ("en" | "zh").
"""

from __future__ import annotations

_LANG = "en"

# English source -> Simplified Chinese. Add entries as more UI is wrapped in t().
_ZH = {
    # ── welcome: menus ──
    "&File": "文件(&F)",
    "&New Project": "新建项目(&N)",
    "&Open Project...": "打开项目(&O)...",
    "E&xit": "退出(&X)",
    "&Settings": "设置(&S)",
    "&Language": "语言(&L)",
    "English": "English",
    "简体中文": "简体中文",
    "&LLM Settings...": "大模型设置(&L)...",
    # ── welcome: body ──
    "Start": "开始",
    "New project": "新建项目",
    "Open the canvas with an empty graph": "打开画布并新建空白图",
    "Open bundle": "打开项目包",
    "Load a .mta bundle or .json graph": "加载 .mta 项目包或 .json 图",
    "Recent projects": "最近项目",
    "Design, generate and run multi-agent systems — visually.":
        "可视化设计、生成并运行多智能体系统。",
    "The coding agent that writes tools now lives in the canvas "
    "designer — Tools → Tool Generator, or a Tool node's "
    "“Create a new tool…” button.":
        "编写工具的编码智能体现已内置于画布设计器 —— 工具 → 工具生成器，"
        "或在工具节点点击“新建工具…”按钮。",
    "No recent projects yet — start a new project or open "
    "a bundle above.":
        "暂无最近项目 —— 新建项目，或在上方打开一个项目包。",
    # ── settings dialog ──
    "Settings — LLM (Tool Generator & Estimation)": "设置 —— 大模型（工具生成器与预估）",
    "Provider:": "服务商：",
    "API key:": "API 密钥：",
    "Model:": "模型：",
    "Base URL:": "接口地址（Base URL）：",
    "Proxy (optional):": "代理（可选）：",
    "HITL:": "人工确认：",
    "Save": "保存",
    "Cancel": "取消",
    "Language changed": "语言已更改",
    "The canvas designer opens in the selected language. Some screens are "
    "still English-only for now.":
        "画布设计器将以所选语言打开。部分界面目前仍仅支持英文。",

    # ── canvas designer: window + menu bar ──
    "Visual Agent Designer (Qt)": "可视化智能体设计器 (Qt)",
    "&Graph": "图(&G)",
    "&Save...": "保存(&S)...",
    "&Load...": "加载(&L)...",
    "&Merge graph from...": "合并图(&M)...",
    "Edit Shared &State...": "编辑共享状态(&S)...",
    "Define &Types...": "定义类型(&T)...",
    "Storage / &Persistence...": "存储/持久化(&P)...",
    "&Edit": "编辑(&E)",
    "&Undo": "撤销(&U)",
    "&Redo": "重做(&R)",
    "Undo.": "已撤销。",
    "Redo.": "已重做。",
    "Nothing to undo.": "没有可撤销的操作。",
    "Nothing to redo.": "没有可重做的操作。",
    "&Generate": "生成(&G)",
    "&Generate Code": "生成代码(&G)",
    "as &Single File (portable)": "单文件（便携）(&S)",
    "as Python &Package (modules)": "Python 包（模块）(&P)",
    "&Run GUI Agent": "运行 GUI 智能体(&R)",
    "Run &Scheduler (ambient)": "运行调度器（常驻）(&S)",
    "Debug Sc&heduler (live overlay)": "调试调度器（实时叠加）(&H)",
    "&Debug Run (live overlay)": "调试运行（实时叠加）(&D)",
    "&Chat Run (multi-turn)...": "对话运行（多轮）(&C)...",
    "Replay &Trace...": "回放轨迹(&T)...",
    "&Compile (PyInstaller)": "编译（PyInstaller）(&C)",
    "&Open Output Folder": "打开输出文件夹(&O)",
    "&Dump System Prompts...": "导出系统提示词(&D)...",
    "&Tools": "工具(&T)",
    "&AI Assistant": "AI 助手(&A)",
    "&Tool Generator...": "工具生成器(&T)...",
    "&Designer Agent...": "设计器智能体(&D)...",
    "&Patterns": "模式(&P)",
    "&View": "视图(&V)",
    "&Auto-fit view": "自动适应视图(&A)",
    "&Fit to view now": "立即适应视图(&F)",
    "Show &run trace panel": "显示运行轨迹面板(&R)",
    "Show &chat run panel": "显示对话运行面板(&C)",
    "&Estimation": "评估(&E)",
    "Estimate &Prompts": "评估提示词(&P)",
    "Estimate &Graph": "评估图(&G)",
    "Estimate &Tool": "评估工具(&T)",
    "Estimate &All": "全部评估(&A)",
    "&LLM API Key / Model / URL...": "大模型 API 密钥/模型/URL(&L)...",
    "&Theme": "主题(&T)",
    "&Dark": "深色(&D)",
    "&Light (bright)": "浅色（明亮）(&L)",
    "Configure — LLM settings & theme": "配置 —— 大模型设置与主题",
    # ── canvas: palette + right-click menu ──
    "Add module:": "添加模块：",
    "Configure '%s'...": "配置 '%s'...",
    "Check Code": "查看代码",
    "Add && link a module": "添加并链接模块",
    "Add %s": "添加 %s",
    "Add %s here": "在此添加 %s",
    "Delete module": "删除模块",
    "Delete all its links": "删除其所有链接",
    "Configure link...": "配置链接...",
    "Delete link": "删除链接",
    # ── node kind labels (KIND_META) ──
    "Agent": "智能体", "LLM": "大模型", "Tools": "工具", "Skills": "技能",
    "Prompt": "提示词", "RAG": "RAG", "Memory": "记忆", "WebServer": "Web 服务",
    "MCP": "MCP", "Worker Pool": "工作池", "Router": "路由", "HITL": "人工确认",
    "Eval": "评估", "GUI": "图形界面", "Schedule": "定时", "If/Else": "条件",
    "While": "循环", "Set State": "设置状态", "Guardrail": "护栏", "End": "结束",
    "Fan-out": "并行分发", "Join": "汇合",
}

_TABLES = {"zh": _ZH}


def set_language(lang: str) -> None:
    global _LANG
    _LANG = "zh" if str(lang).lower() in ("zh", "zh-hans", "zh-cn", "cn", "chinese") else "en"


def get_language() -> str:
    return _LANG


def t(text: str) -> str:
    """Translate an English source string to the current language (identity for
    English or any untranslated string)."""
    if _LANG == "en":
        return text
    return _TABLES.get(_LANG, {}).get(text, text)
