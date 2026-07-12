"""Build ReadMe.pdf — a user guide for designing AI agents with MetaAgent.

Run:
    python build_readme.py

Output:
    ReadMe.pdf  (next to this script)

The PDF is generated with ReportLab's Platypus framework. The content is
authored as structured Python data (sections, paragraphs, lists, tables, code
blocks) so it stays close to the live behavior described in the source.
"""

from __future__ import annotations

import os

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm, mm
from reportlab.platypus import (PageBreak, Paragraph, SimpleDocTemplate, Spacer,
                                Table, TableStyle)
from reportlab.platypus.flowables import HRFlowable, KeepTogether

OUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "ReadMe.pdf")

# ── styles ─────────────────────────────────────────────────────────────────

_styles = getSampleStyleSheet()


def _style(name, parent, **kw):
    return ParagraphStyle(name, parent=_styles[parent], **kw)


TITLE = _style("DocTitle", "Title", fontSize=26, leading=30, spaceAfter=4,
               textColor=colors.HexColor("#102A43"))
SUBTITLE = _style("DocSubtitle", "Normal", fontSize=13, leading=17,
                  spaceAfter=18, textColor=colors.HexColor("#486581"),
                  alignment=TA_LEFT, fontName="Helvetica-Oblique")

H1 = _style("H1", "Heading1", fontSize=18, leading=22, spaceBefore=16,
            spaceAfter=8, textColor=colors.HexColor("#102A43"))
H2 = _style("H2", "Heading2", fontSize=14, leading=18, spaceBefore=12,
            spaceAfter=6, textColor=colors.HexColor("#243B53"))
H3 = _style("H3", "Heading3", fontSize=11.5, leading=14, spaceBefore=8,
            spaceAfter=4, textColor=colors.HexColor("#334E68"))

BODY = _style("Body", "BodyText", fontSize=10.2, leading=14, spaceAfter=6,
              alignment=TA_LEFT)
SMALL = _style("Small", "BodyText", fontSize=9, leading=12,
               textColor=colors.HexColor("#486581"))

BULLET = _style("Bullet", "BodyText", fontSize=10.2, leading=14,
                leftIndent=14, bulletIndent=2, spaceAfter=2)
SUBBULLET = _style("SubBullet", "BodyText", fontSize=10, leading=13,
                   leftIndent=28, bulletIndent=16, spaceAfter=2)

CODE = _style("Code", "Code", fontSize=8.8, leading=11,
              textColor=colors.HexColor("#102A43"),
              backColor=colors.HexColor("#F5F7FA"),
              borderColor=colors.HexColor("#BCCCDC"), borderWidth=0.6,
              borderPadding=6, leftIndent=4, rightIndent=4, spaceAfter=8)

CALLOUT_TIP = _style("Tip", "BodyText", fontSize=10, leading=13,
                     textColor=colors.HexColor("#0B6E4F"),
                     backColor=colors.HexColor("#E6FCF5"),
                     borderColor=colors.HexColor("#0B6E4F"), borderWidth=0.6,
                     borderPadding=6, spaceAfter=8)
CALLOUT_WARN = _style("Warn", "BodyText", fontSize=10, leading=13,
                      textColor=colors.HexColor("#8A2C0D"),
                      backColor=colors.HexColor("#FFF4E6"),
                      borderColor=colors.HexColor("#D9480F"), borderWidth=0.6,
                      borderPadding=6, spaceAfter=8)

# ── helpers ────────────────────────────────────────────────────────────────


def p(text, style=BODY):
    return Paragraph(text, style)


def bullets(items, style=BULLET):
    return [Paragraph(t, style, bulletText="•") for t in items]


def sub_bullets(items):
    return [Paragraph(t, SUBBULLET, bulletText="–") for t in items]


def code(text):
    text = text.strip("\n")
    safe = (text.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace("\n", "<br/>")
            .replace(" ", "&nbsp;"))
    return Paragraph(f"<font face='Courier'>{safe}</font>", CODE)


def tip(text):
    return Paragraph(f"<b>Tip.</b> {text}", CALLOUT_TIP)


def warn(text):
    return Paragraph(f"<b>Heads-up.</b> {text}", CALLOUT_WARN)


def rule():
    return HRFlowable(width="100%", thickness=0.4,
                      color=colors.HexColor("#BCCCDC"),
                      spaceBefore=4, spaceAfter=8)


def table(rows, col_widths=None, header=True):
    style_cmds = [
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), 9.2),
        ("LEADING", (0, 0), (-1, -1), 12),
        ("LINEBELOW", (0, 0), (-1, 0),
         0.6, colors.HexColor("#243B53")) if header else
        ("BOX", (0, 0), (-1, -1), 0.4, colors.HexColor("#BCCCDC")),
        ("BOX", (0, 0), (-1, -1), 0.4, colors.HexColor("#BCCCDC")),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#D9E2EC")),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F0F4F8")),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]
    body = []
    for row in rows:
        body.append([Paragraph(str(cell), SMALL) for cell in row])
    return Table(body, colWidths=col_widths, style=TableStyle(style_cmds))


def page_footer(canvas, doc):
    canvas.saveState()
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(colors.HexColor("#627D98"))
    canvas.drawString(2 * cm, 1.2 * cm,
                      "MetaAgent — agent designer guide")
    canvas.drawRightString(A4[0] - 2 * cm, 1.2 * cm, f"Page {doc.page}")
    canvas.restoreState()


# ── content ────────────────────────────────────────────────────────────────


def build_story():
    story = []

    # Cover
    story += [
        p("MetaAgent", TITLE),
        p("An agent that builds agents — a user guide for designing, "
          "generating, and shipping your own AI agents with a visual canvas.",
          SUBTITLE),
        rule(),
        p("MetaAgent is a <b>PySide6/Qt</b> single-process desktop app. It "
          "opens on a <b>welcome launcher</b> where you create or open a "
          "project; that drops you straight into the <b>visual canvas "
          "designer</b> — the heart of the app. On the canvas you drop blocks "
          "(agents, LLMs, tools, skills, prompts, routers, worker pools, RAG, "
          "MCP, HITL, eval sets, web servers) and wire them together. A "
          "built-in <b>coding agent</b> — the <b>Tool Generator</b>, opened "
          "from the canvas (<b>Tools → Tool Generator</b>, or a Tool node's "
          "<i>Create a new tool…</i> button) — chats with you and writes "
          "Python <b>tools</b> into a shared library. When you press "
          "<b>Generate Code</b>, MetaAgent emits a "
          "complete standalone Python app under "
          "<font face='Courier'>generated_agents/&lt;name&gt;/</font> — "
          "console script, optional PySide6/Qt GUI, optional WebSocket server, "
          "<font face='Courier'>requirements.txt</font>, and a one-click "
          "PyInstaller build script. The generated agents have no LangChain "
          "dependency at runtime — only <font face='Courier'>openai</font> "
          "(or <font face='Courier'>anthropic</font>)."),
        Spacer(1, 6),
        p("This guide takes you from a clean install to a compiled agent "
          "ready to ship. It assumes you have Python 3.10+ and a SiliconFlow "
          "or other LLM API key."),
    ]

    # ── 1. Quickstart ──
    story += [
        PageBreak(),
        p("1. Quickstart", H1),
        p("Install MetaAgent and launch the desktop app:"),
        code("pip install -r requirements.txt\n"
             "python main.py"),
        p("On first launch you land on the <b>welcome launcher</b>:"),
        *bullets([
            "Open <b>Settings → API Key / Model</b> and paste your key. "
            "The defaults target SiliconFlow's OpenAI-compatible endpoint "
            "for DeepSeek-V4-Flash. Settings are saved to "
            "<font face='Courier'>config.json</font>.",
            "Click <b>New project</b> (or open a recent one). The "
            "<b>visual canvas designer</b> opens — this is the main "
            "workspace where you build everything.",
            "Need a custom tool? Open the <b>Tool Generator</b> from the "
            "canvas (<b>Tools → Tool Generator</b>, <b>Ctrl+T</b>, or a Tool "
            "node's <i>Create a new tool…</i> button) and ask, e.g. "
            "<i>\"Write a tool that fetches a URL and returns the text.\"</i> "
            "It replies with a complete <font face='Courier'>@tool</font> "
            "function; saving is Human-in-the-Loop gated, so a review dialog "
            "shows the code for approval first. (Or just start with the "
            "seeded <font face='Courier'>load_csv</font> tool.)",
            "On the canvas, add an <b>Agent</b> block, link an <b>LLM</b> "
            "and a <b>Tools</b> block to it, type an agent name, then click "
            "<b>Generate Code</b>.",
            "Run the new agent from the canvas with <b>Run GUI Agent</b>, "
            "or from a terminal:",
        ]),
        code("cd generated_agents/my_pipeline\n"
             "pip install -r requirements.txt\n"
             "python gui.py        # PySide6/Qt chat (default)\n"
             "python agent.py \"your task\"   # console one-shot\n"
             "python server.py     # WebSocket + browser chat (if enabled)"),
        tip("Every agent panel has a <b>Compile (PyInstaller)</b> button. It "
            "builds inside a clean <font face='Courier'>.buildenv</font> venv "
            "so the resulting .exe stays lean and only bundles what your "
            "tools actually import."),
    ]

    # ── 2. The two surfaces ──
    story += [
        PageBreak(),
        p("2. The two surfaces of MetaAgent", H1),
        p("MetaAgent gives you two complementary ways of working. You almost "
          "always use both."),

        p("2.1 The Tool Generator (coding agent)", H2),
        p("Opened from the canvas designer — <b>Tools → Tool Generator</b> "
          "(<b>Ctrl+T</b>), or a Tool node's <i>Create a new tool…</i> "
          "button. It hosts the <b>coding agent</b>, a senior-engineer "
          "persona that writes Python tools in MetaAgent's lightweight "
          "<font face='Courier'>@tool</font> registry style. It uses native function "
          "calling to manage its own library through three tools:"),
        table([
            ["Tool", "What it does"],
            ["<b>list_tools()</b>", "Show every .py file currently in "
             "<font face='Courier'>tools/</font>."],
            ["<b>read_tool(name)</b>", "Read a tool's source before editing "
             "it."],
            ["<b>save_tool(name, code)</b>", "Write a new or revised tool "
             "into <font face='Courier'>tools/</font>. <b>HITL-gated</b>: a "
             "review dialog shows the full code with Allow / Deny (Deny is "
             "the default button)."],
        ], col_widths=[4.5 * cm, 11.5 * cm]),
        Spacer(1, 4),
        p("Conversation memory is persisted in "
          "<font face='Courier'>chat_history.json</font> so you can close the "
          "app, reopen it later, and keep iterating on the same tool. The "
          "status bar shows live token usage and the current context size."),

        p("2.2 The visual canvas designer", H2),
        p("The canvas opens as soon as you create or open a project from the "
          "welcome launcher — it is the main workspace. The left palette has "
          "buttons that add modules to a freeform canvas; the right side is "
          "the canvas itself."),
        *bullets([
            "<b>Drag</b> a module to move it.",
            "<b>Drag from the right port</b> (●) onto another module to link "
            "them. The arrow is colored and styled automatically based on "
            "what it connects.",
            "<b>Double-click</b> a module to configure it.",
            "<b>Right-click</b> for menus (rename, delete, set entry, …).",
            "<b>Del</b> deletes the selection.",
            "<b>Insert Pattern</b> seeds the canvas from a preset (ReAct, "
            "planner-executor, planner-executor-critic, supervisor-worker).",
            "<b>Save…</b> and <b>Load…</b> persist the whole graph as JSON in "
            "<font face='Courier'>graphs/</font>.",
        ]),
        tip("The canvas validates as you go: link rules are enforced "
            "(duplicate tool files blocked, single-input kinds like "
            "<i>prompt</i>/<i>rag</i> enforced); duplicate LLMs and "
            "duplicate skill text raise a warning so you don't silently "
            "double up."),
    ]

    # ── 3. Workflow A: tools ──
    story += [
        PageBreak(),
        p("3. Workflow A — build a tool with the coding agent", H1),
        p("Tools are the only way an agent can do anything other than chat. "
          "MetaAgent keeps them as flat Python files under "
          "<font face='Courier'>tools/</font>, each with one or more "
          "<font face='Courier'>@tool</font> functions (from "
          "<font face='Courier'>tool_registry</font>)."),

        p("3.1 Ask the coding agent", H2),
        p("In the chat box, describe what you want — be specific about input "
          "and output:"),
        code("\"Write a tool that takes a CSV path and returns the column \n"
             " names plus mean of each numeric column.\""),
        p("The reply is a complete function in a fenced "
          "<font face='Courier'>```python</font> block. It always:"),
        *bullets([
            "Imports <font face='Courier'>tool</font> from "
            "<font face='Courier'>tool_registry</font> (MetaAgent's lightweight, "
            "langchain-free decorator) and decorates the function with "
            "<font face='Courier'>@tool</font>.",
            "Type-hints every argument and returns a <b>string</b> (the "
            "agent's observation channel is text).",
            "Catches exceptions inside the tool and returns "
            "<font face='Courier'>\"[ERROR] …\"</font> strings instead of "
            "raising — so the calling agent can recover.",
            "Writes the first docstring line as the one-line description an "
            "LLM uses to decide whether to call the tool.",
        ]),
        p("Refining the tool is conversational: ask for changes (\"add a "
          "<font face='Courier'>max_rows</font> argument\") and the agent "
          "replies with the <b>full</b> revised tool — never a diff."),

        p("3.2 Save it (two ways)", H2),
        *bullets([
            "<b>Let the agent save it.</b> Say \"save it\" (or accept its "
            "offer). It will call <font face='Courier'>save_tool</font>; "
            "the <b>HITL review dialog</b> pops up showing the exact code "
            "that would be written. Click <b>Allow</b> to commit, "
            "<b>Deny</b> to reject. (Toggle the gate in Settings via "
            "<font face='Courier'>hitl_confirm</font>.)",
            "<b>Manual fallback.</b> Click the <b>Save Tool(s)</b> button in "
            "the Tool Generator window — it extracts every "
            "<font face='Courier'>@tool</font> function from the last reply "
            "and writes them to <font face='Courier'>tools/&lt;name&gt;.py</font>.",
        ]),

        p("3.3 Example tool", H2),
        p("Below is the <font face='Courier'>load_csv</font> tool that ships "
          "with MetaAgent. Notice the docstring, the error-as-string pattern, "
          "and the string return."),
        code(
            "from tool_registry import tool\n"
            "import csv, io\n\n"
            "@tool\n"
            "def load_csv(path: str, max_rows: int = 20) -> str:\n"
            "    \"\"\"Load a CSV file; return columns, total row count, "
            "and the first rows.\n\n"
            "    Args:\n"
            "        path: path to the CSV file.\n"
            "        max_rows: how many data rows to include in the "
            "preview.\n"
            "    \"\"\"\n"
            "    try:\n"
            "        with open(path, newline=\"\", encoding=\"utf-8-sig\") "
            "as f:\n"
            "            rows = list(csv.reader(f))\n"
            "    except FileNotFoundError:\n"
            "        return f\"[ERROR] File not found: {path}\"\n"
            "    ...\n"),
        tip("MetaAgent inspects each tool's imports and adds any third-party "
            "dependency (e.g. <font face='Courier'>pandas</font>, "
            "<font face='Courier'>requests</font>) to the generated agent's "
            "<font face='Courier'>requirements.txt</font> automatically. "
            "You don't have to edit anything."),
    ]

    # ── 4. The canvas blocks ──
    story += [
        PageBreak(),
        p("4. Workflow B — design an agent on the canvas", H1),
        p("Every block has a color, a primary purpose, and a set of "
          "allowed links. The table below is the cheat sheet."),

        table([
            ["Block", "Color", "Purpose / link rule"],
            ["<b>Agent</b>", "blue",
             "A stage in the flow. Configure role, budgets, HITL policy. "
             "Connect resources (LLM, Tools, Prompt, Skills, RAG, MCP) into "
             "it; connect <i>out</i> to another stage to form a pipeline."],
            ["<b>LLM</b>", "green",
             "A model+key+provider. Link several into one agent to set a "
             "<b>fallback chain</b> (first link is primary; subsequent "
             "links are tried in order on failure)."],
            ["<b>Tools</b>", "orange",
             "A bundle of files from <font face='Courier'>tools/</font>. "
             "Link one Tools block into an agent; the agent gains every "
             "<font face='Courier'>@tool</font> function in the bundle. "
             "Duplicate files into the same agent are blocked at link time."],
            ["<b>Skills</b>", "purple",
             "A list of named guidance snippets, each appended to the linked "
             "agent's system prompt. The generated GUI gets a <i>Skills</i> "
             "menu to add/edit/remove these at runtime."],
            ["<b>Prompt</b>", "yellow",
             "Override the agent's persona. Has a <i>role</i> "
             "(single / planner / worker / critic / supervisor), each backed "
             "by an editable template in <font face='Courier'>templates/</font>. "
             "A prompt whose role contradicts its agent's role is rejected at "
             "generation time. At most one prompt per agent."],
            ["<b>RAG</b>", "teal",
             "Point at a local docs folder; the linked agent gets a "
             "<font face='Courier'>search_docs</font> tool backed by a "
             "BM25 index (chunking, top-k, citations). At most one RAG per "
             "agent."],
            ["<b>MCP</b>", "indigo",
             "Connect to a Model Context Protocol server via "
             "<i>stdio</i>, <i>streamable_http</i>, or <i>sse</i>. Tools are "
             "discovered at runtime and become callable. Multiple MCP "
             "clients can link to one agent."],
            ["<b>Worker Pool</b>", "indigo-grey",
             "Identical workers sharing one LLM/prompt/tools. When the "
             "upstream agent emits a list of subtasks, the pool runs them in "
             "parallel up to <i>max_workers</i> and merges the results in "
             "order."],
            ["<b>Router</b>", "blue-grey",
             "An LLM picks <i>one</i> of the router's outgoing agents per "
             "input. Adding a router switches the agent app into "
             "graph-execution mode (it walks from the entry following the "
             "router's choices)."],
            ["<b>HITL</b>", "amber",
             "A human checkpoint between stages. Pauses the run so a person "
             "can approve / edit / reject the content in flight. "
             "<i>on-reject</i> can be <b>stop</b> (end) or <b>revise</b> "
             "(re-run the upstream agent with feedback)."],
            ["<b>Eval</b>", "lime",
             "A set of test cases for the agent. Each case: an input plus "
             "an expected substring, regex, or LLM-judged criterion. Linked "
             "to one agent → tests that agent alone; standalone → tests the "
             "whole harness."],
            ["<b>WebServer</b>", "red",
             "Standalone block (no links). Its presence adds "
             "<font face='Courier'>server.py</font> — a WebSocket server "
             "with a built-in browser chat UI."],
        ], col_widths=[3.0 * cm, 2.0 * cm, 11 * cm]),

        Spacer(1, 6),
        p("4.1 The link rules in one table", H2),
        table([
            ["From → To",  "Meaning"],
            ["llm/tool/skill/prompt/rag/mcp → agent (or pool/router)",
             "<b>uses</b> — agent consumes the resource (grey arrow)."],
            ["agent/pool/router → agent/pool/router",
             "<b>flows to</b> — first runs, then the second (blue arrow)."],
            ["agent → earlier agent",
             "<b>revise loop</b> (dashed red back-edge). Bounded retry, "
             "default 2 rounds (<font face='Courier'>MAX_REVISE_ROUNDS</font>)."],
            ["eval → agent",
             "Tests <i>only</i> that agent. Eval blocks left standalone "
             "test the whole pipeline."],
            ["hitl → agent / agent → hitl → agent",
             "Insert a human checkpoint into the flow."],
        ], col_widths=[6.5 * cm, 9.5 * cm]),

        Spacer(1, 4),
        warn("Some inputs are <b>singletons</b>: an agent can have at most "
             "one <i>prompt</i> link and at most one <i>rag</i> link. LLM "
             "and MCP are intentionally <i>not</i> singletons — several "
             "LLMs form a fallback chain, several MCPs add tool servers."),
    ]

    # ── 5. Agents in depth: role, budgets, HITL ──
    story += [
        PageBreak(),
        p("5. Configuring an Agent", H1),
        p("Double-click an Agent block to open its config dialog. Three "
          "things matter: <b>role</b>, <b>budgets</b>, and the "
          "<b>HITL policy</b>."),

        p("5.1 Roles", H2),
        table([
            ["Role", "What it does in the pipeline"],
            ["<b>single</b>",
             "A standalone ReAct-style agent. Receives the user task, "
             "reasons, calls tools, returns the final answer."],
            ["<b>planner</b>",
             "Breaks the task into a short numbered plan. Does not execute. "
             "Forwards the plan to the next stage."],
            ["<b>worker</b>",
             "Executes the plan (or the raw task). Allowed to call tools."],
            ["<b>critic</b>",
             "Reviews the previous stage. If acceptable, gives the polished "
             "final answer. If not, starts the reply with "
             "<font face='Courier'>REVISE:</font> and concrete feedback, "
             "which triggers a back-edge loop (if one is wired)."],
            ["<b>supervisor</b>",
             "Delegates one instruction at a time to its workers using a "
             "strict NEXT/DONE protocol; reviews each worker result. Picks "
             "a specific worker with <font face='Courier'>NEXT &lt;name&gt;: "
             "…</font>."],
        ], col_widths=[2.8 * cm, 13.2 * cm]),
        Spacer(1, 4),
        p("Each role has an editable template under "
          "<font face='Courier'>templates/</font> "
          "(<font face='Courier'>prompt_planner.txt</font>, "
          "<font face='Courier'>prompt_worker.txt</font>, "
          "<font face='Courier'>prompt_critic.txt</font>, "
          "<font face='Courier'>prompt_supervisor.txt</font>, "
          "<font face='Courier'>system_prompt_template.txt</font>). The "
          "<font face='Courier'>{agent_name}</font> placeholder is filled in "
          "at generation."),

        p("5.2 Budgets (per agent)", H2),
        p("Every agent enforces four hard limits at runtime. Once any is hit, "
          "the agent stops with a budget error."),
        table([
            ["Field", "Default", "What it caps"],
            ["max_iterations", "10", "Number of LLM rounds in a single "
             "task."],
            ["max_tool_calls", "20", "Total tool invocations per task."],
            ["max_output_tokens", "8&nbsp;000", "Cumulative output tokens "
             "emitted by the LLM."],
            ["max_wall_clock_s", "60", "Wall-clock seconds for the whole "
             "task."],
        ], col_widths=[4 * cm, 2.5 * cm, 9.5 * cm]),

        p("5.3 HITL policy (per agent)", H2),
        *bullets([
            "<b>Pause before this stage runs</b> — review the input first.",
            "<b>Also pause when:</b> a <i>high-risk tool</i> is about to be "
            "called (write/delete/send-type), or the agent's self-reported "
            "<i>confidence</i> is below the threshold (default 0.6 — "
            "requires one extra LLM call per answer).",
            "<b>On reject:</b> <i>stop</i> ends the run; <i>revise</i> "
            "re-runs the upstream agent with the reviewer's feedback.",
        ]),
        tip("HITL gates also apply globally to the coding agent in "
            "MetaAgent itself — the <i>save_tool</i> call always pops the "
            "review dialog unless you disable "
            "<font face='Courier'>hitl_confirm</font> in Settings."),
    ]

    # ── 6. LLM configuration ──
    story += [
        PageBreak(),
        p("6. LLM blocks", H1),
        p("An LLM block represents one model. Double-click to set:"),
        table([
            ["Field", "Meaning"],
            ["Provider", "<b>siliconflow</b>, <b>deepseek</b>, <b>openai</b>, "
             "<b>gemini</b> (via Google's OpenAI-compatible endpoint), "
             "<b>anthropic</b>. Picking a provider auto-fills sensible Model "
             "+ Base URL defaults."],
            ["Model", "The model id as the provider names it, e.g. "
             "<font face='Courier'>deepseek-ai/DeepSeek-V4-Flash</font>, "
             "<font face='Courier'>gpt-4o</font>, "
             "<font face='Courier'>claude-opus-4-8</font>, "
             "<font face='Courier'>gemini-2.5-flash</font>."],
            ["API key", "Leave blank to read from an env var at run time "
             "(recommended); a value here is written verbatim into the "
             "generated <font face='Courier'>config.json</font>."],
            ["Base URL", "OpenAI-compatible endpoint without "
             "<font face='Courier'>/chat/completions</font>. Blank for "
             "openai/anthropic defaults."],
            ["Temperature / Top-p", "Blank = provider default. Both are "
             "<b>rejected</b> by Anthropic Opus 4.x — leave blank there."],
            ["Response format",
             "<b>text</b> = no constraint. <b>json_object</b> = guaranteed "
             "valid JSON (OpenAI-family only; great for a Planner). "
             "<b>json_schema</b> = output must match the schema below. The "
             "dialog shows a vendor-aware support hint as you choose."],
            ["Response schema", "Required when format = json_schema. A "
             "small object with typed properties + a "
             "<font face='Courier'>required</font> list is the most portable "
             "shape across vendors."],
            ["Request timeout (s)", "Hard cap on each API call. Blank = SDK "
             "default (~10 min). A timed-out call is retried, then fails "
             "over to the next linked LLM."],
            ["Parallel tools", "Off (default) = tool calls in one turn run "
             "sequentially. On = parallel-safe (read-only) calls run "
             "concurrently; writes stay serial. Decided by the agent's "
             "<i>primary</i> LLM."],
            ["Vision", "Enable for image-capable models (e.g. gpt-4o, "
             "claude-3.x, gemini, Qwen-VL). The generated chat (desktop + "
             "web) gains an image attach/drop control for the agent that "
             "takes the user input."],
            ["Extra API params", "Any JSON object merged verbatim into "
             "the request: <font face='Courier'>seed</font>, "
             "<font face='Courier'>stop</font>, "
             "<font face='Courier'>frequency_penalty</font>, "
             "<font face='Courier'>top_k</font>, "
             "<font face='Courier'>reasoning_effort</font>, …"],
        ], col_widths=[3.5 * cm, 12.5 * cm]),

        Spacer(1, 4),
        p("6.1 Fallback chain", H2),
        p("Link two or more LLMs into one agent. The <b>first</b> link is "
          "the primary; on a 429 / 5xx / timeout, the runtime retries "
          "(exponential backoff + jitter) and then fails over to the next "
          "LLM in link order. When the generated agent has a GUI, an "
          "<b>LLM</b> menu lets users switch the active model per agent "
          "(others remain fallbacks). The choice is persisted in "
          "<font face='Courier'>llm_choice.json</font>."),

        tip("Duplicate LLMs (same provider + model + base URL) linked into "
            "the same agent raise a non-blocking warning at link time and "
            "again at Generate: a duplicate adds no real fallback."),
    ]

    # ── 7. Pattern presets ──
    story += [
        PageBreak(),
        p("7. Pattern presets", H1),
        p("The palette's <b>Insert Pattern</b> menu seeds the canvas with a "
          "known topology and a sensible LLM stub. You can keep iterating "
          "from there. The four built-in patterns:"),

        table([
            ["Pattern", "Topology", "When to use"],
            ["<b>ReAct</b> (single)",
             "agent",
             "One-stop tool-using agent. The simplest, cheapest, fastest."],
            ["<b>Planner–Executor</b>",
             "planner → executor",
             "Long, multi-step tasks where it helps to write the plan first."],
            ["<b>Planner–Executor–Critic</b>",
             "planner → executor → critic → planner (loop)",
             "Tasks where quality matters and the critic can demand a "
             "revise. Bounded to "
             "<font face='Courier'>MAX_REVISE_ROUNDS</font> retries."],
            ["<b>Supervisor–Worker</b>",
             "supervisor ↔ worker(s)",
             "Delegation style: a supervisor hands one instruction at a "
             "time using the NEXT/DONE protocol and decides when the task "
             "is done."],
        ], col_widths=[4 * cm, 5 * cm, 7 * cm]),

        p("7.1 Adding control flow on the canvas", H2),
        *bullets([
            "<b>Routers</b> branch the flow. Link a Router → A, B, C and the "
            "Router's LLM picks one of those agents per input "
            "(triage → billing / tech / general).",
            "<b>Worker Pools</b> fan out. When the upstream agent emits a "
            "list of subtasks (JSON array or numbered/bulleted lines), the "
            "pool runs them concurrently and merges results in order. "
            "Best in chain patterns; in supervisor mode a pool degrades to "
            "a single worker per instruction.",
            "<b>HITL</b> blocks splice a human checkpoint between stages.",
            "Mixing routers / pools / loops is fine; MetaAgent runs the "
            "static analyzer at generation and surfaces warnings (cycles, "
            "missing LLMs, missing entry, …)."],
        ),
    ]

    # ── 8. Advanced blocks ──
    story += [
        PageBreak(),
        p("8. Advanced building blocks", H1),

        p("8.1 RAG", H2),
        p("Drop a RAG block, set <b>Docs folder</b>, link it into an agent. "
          "The agent gets a <font face='Courier'>search_docs</font> tool "
          "backed by a dependency-free BM25 index (chunking, top-k, source "
          "citations). When the agent has a GUI, a <b>RAG menu</b> lets the "
          "user toggle RAG on/off, add files into the knowledge base "
          "(text / markdown / csv / json / html / py, plus <b>PDF</b> via "
          "<font face='Courier'>pypdf</font> and <b>.docx</b> via "
          "<font face='Courier'>python-docx</font>), paste ad-hoc text "
          "chunks, edit/delete chunks or remove all chunks from one file, "
          "rebuild the index, and clear the store."),

        p("8.2 MCP", H2),
        p("Connect the agent to a Model Context Protocol server. Three "
          "transports:"),
        *bullets([
            "<b>stdio</b> — a local command + args that launches a server "
            "(e.g. <font face='Courier'>python my_server.py</font>).",
            "<b>streamable_http</b> — modern MCP HTTP transport, URL "
            "usually ends in <font face='Courier'>/mcp</font>.",
            "<b>sse</b> — older HTTP+SSE transport, URL usually ends in "
            "<font face='Courier'>/sse</font>.",
        ]),
        p("HTTP transports have a <b>verify-TLS</b> toggle for self-signed "
          "<font face='Courier'>https</font>. The configured server is "
          "probed with the dialog's <b>Test connection</b> button — any "
          "HTTP response (even 400/405) confirms the host/port/scheme are "
          "right; the real MCP handshake runs inside the agent."),

        p("8.3 HITL checkpoints", H2),
        p("Wire a HITL block as <i>agent → HITL → agent</i> to gate a "
          "hand-off, or <i>HITL → agent</i> to gate the start. The run "
          "pauses; the reviewer may <b>approve</b>, <b>edit</b> the content "
          "in place, or <b>reject</b> with feedback. Reject → "
          "<b>stop</b> ends the run; <b>revise</b> re-runs the upstream "
          "agent with the reviewer's feedback (bounded)."),

        p("8.4 Eval sets", H2),
        p("Add an Eval block, double-click to add cases (input + expected "
          "substring / regex / LLM-judged criterion). Link the block to one "
          "agent to test that agent alone, or leave it standalone to test "
          "the whole pipeline. Generation emits a "
          "<font face='Courier'>run_evals.py</font> harness and "
          "<font face='Courier'>evals/evalset.example.jsonl</font>. The "
          "<font face='Courier'>--floor</font> exit code makes it CI-ready."),

        p("8.5 WebServer", H2),
        p("Add a WebServer block (or tick the checkbox in the form "
          "designer). Generation emits <font face='Courier'>server.py</font> "
          "which exposes the whole agent app over WebSocket and serves a "
          "<b>built-in browser chat UI</b> at "
          "<font face='Courier'>http://host:port/</font>. Raw clients send "
          "<font face='Courier'>{\"type\": \"task\", \"task\": …}</font> "
          "and receive live <font face='Courier'>trace</font> messages plus "
          "a final <font face='Courier'>result</font>. Optional token auth, "
          "serialized runs, headless HITL (high-risk tools denied unless "
          "<font face='Courier'>server.auto_allow_tools</font> is set). "
          "Dependency: <font face='Courier'>websockets</font>."),
        tip("If the agent also has a GUI, the GUI gains a "
            "<b>Server → Enable WebSocket Server</b> check item that "
            "starts/stops <font face='Courier'>server.py</font> from inside "
            "the chat window (stopped automatically when the GUI closes)."),
    ]

    # ── 9. Generate / Run / Compile ──
    story += [
        PageBreak(),
        p("9. Generate, Run, Compile", H1),

        p("9.1 Generate Code", H2),
        p("In the canvas palette, type an <b>Agent name</b>, optionally tick "
          "<b>PySide6 desktop GUI</b>, and click <b>Generate Code</b>. MetaAgent "
          "runs the static analyzer, surfaces any errors as a blocking "
          "dialog, and on success emits a complete folder under "
          "<font face='Courier'>generated_agents/&lt;name&gt;/</font>:"),

        table([
            ["File", "Role"],
            ["<b>agent.py</b>", "Self-contained agent script with selected "
             "tools inlined, budgets enforced, native function calling, "
             "no LangChain dependency."],
            ["<b>gui.py</b>", "PySide6/Qt chat window (live step traces, "
             "Workspace menu, LLM-picker menu, Skills/RAG menus when "
             "applicable). Optional."],
            ["<b>server.py</b>", "WebSocket server + browser chat UI. "
             "Created when a WebServer block is in the graph."],
            ["<b>run_evals.py</b>", "Eval harness, when any Eval block "
             "exists."],
            ["<b>config.json</b>", "API keys / models / HITL flags "
             "/ stream / high_risk_tools. For pipelines, the "
             "<font face='Courier'>llms</font> key is "
             "<font face='Courier'>{agent_name: [primary, fallback, …]}</font>."],
            ["<b>requirements.txt</b>", "Auto-detected from inlined tool "
             "imports. Always includes "
             "<font face='Courier'>openai</font> or "
             "<font face='Courier'>anthropic</font>."],
            ["<b>build.bat</b>", "One-click PyInstaller command."],
            ["<b>README.md</b>", "How to run the generated app."],
            ["<b>history.json</b>", "Generated agent's persisted "
             "conversation (created on first run)."],
            ["<b>traces/&lt;trace_id&gt;.jsonl</b>", "Per-run structured "
             "trace: run_start, llm_step, tool_call/result, retry, "
             "failover, HITL decision, run_end with token usage."],
        ], col_widths=[4 * cm, 12 * cm]),

        p("9.2 Run", H2),
        p("From the designer, click <b>Run GUI Agent</b>. MetaAgent:"),
        *bullets([
            "Reads <font face='Courier'>requirements.txt</font> and checks "
            "every module against the Python that will host the agent "
            "(when MetaAgent is frozen, that's a system Python on PATH; "
            "from source, it's the running Python).",
            "If anything is missing, offers to "
            "<font face='Courier'>pip install -r requirements.txt</font>.",
            "Warns when an API key in "
            "<font face='Courier'>config.json</font> is still empty.",
            "Launches <font face='Courier'>gui.py</font> in its own process.",
        ]),

        p("9.3 Debug Run (live overlay)", H2),
        p("The canvas has a <b>Debug Run (live overlay)</b> button. It "
          "runs the agent in-process and animates each block with its "
          "live status:"),
        *bullets([
            "<font color='#FFA000'>amber border + ▶</font> = running",
            "<font color='#2E7D32'>green border + ✓</font> = done",
            "<font color='#C62828'>red border + ✗</font> = error",
            "Active link is brightest; traversed links stay drawn.",
        ]),

        p("9.4 Compile (PyInstaller)", H2),
        p("Click <b>Compile (PyInstaller)</b>. MetaAgent creates a clean "
          "<font face='Courier'>.buildenv</font> venv inside the agent's "
          "folder, installs only the agent's requirements + PyInstaller, "
          "then builds:"),
        *bullets([
            "<font face='Courier'>&lt;name&gt;.exe</font> for the console "
            "script (always).",
            "<font face='Courier'>&lt;name&gt;_gui.exe</font> "
            "(<font face='Courier'>--windowed</font>) when "
            "<font face='Courier'>gui.py</font> exists.",
            "Both as <font face='Courier'>--onedir</font>: faster cold "
            "start; <font face='Courier'>config.json</font> sits visibly "
            "next to the exe inside <font face='Courier'>dist/</font>.",
        ]),
        warn("Building from a fat Python pulls optional imports "
             "(pandas → scipy → torch …) and bloats the .exe. The "
             "<font face='Courier'>.buildenv</font> approach keeps the "
             "binary lean — never replace it with your global Python."),
    ]

    # ── 10. Anatomy of a generated agent ──
    story += [
        PageBreak(),
        p("10. Anatomy of a generated agent", H1),
        p("Every generated agent ships with production hardening built in. "
          "The most important pieces:"),

        table([
            ["Pillar", "How it shows up"],
            ["<b>Traces</b>",
             "Every run writes "
             "<font face='Courier'>traces/&lt;trace_id&gt;.jsonl</font>: "
             "run_start, llm_step, tool_call, tool_result, retry, failover, "
             "HITL decisions, run_end with token usage."],
            ["<b>Retry</b>",
             "Transient LLM errors (429/5xx/timeout) are retried with "
             "exponential backoff + jitter <i>before</i> failing over to the "
             "next LLM in the fallback chain."],
            ["<b>HITL safety</b>",
             "High-risk tool calls (write / delete / send / …) require "
             "confirmation: a GUI dialog, console prompt, or — for the "
             "WebSocket server — headless denial unless "
             "<font face='Courier'>server.auto_allow_tools</font> is set. "
             "Add tool names to <font face='Courier'>high_risk_tools</font> "
             "in <font face='Courier'>config.json</font> to extend the "
             "default policy. Disable with "
             "<font face='Courier'>\"hitl_confirm\": false</font>."],
            ["<b>Token streaming</b>",
             "Serial stages (planner / worker / critic / single) stream "
             "text live in console, GUI, and the browser UI. Parallel pool "
             "workers stay on their own line traces so they don't "
             "interleave. Toggle with "
             "<font face='Courier'>\"stream\": true</font>."],
            ["<b>Persistent conversation</b>",
             "Each exchange is appended to "
             "<font face='Courier'>history.json</font>. GUIs replay on "
             "startup (History menu to clear); console resumes too "
             "(<font face='Courier'>--new</font> starts fresh)."],
            ["<b>Workspace</b>",
             "The GUI has a Workspace menu — set / add / manage the "
             "folders the agent works on; tool paths are resolved against "
             "this workspace by default."],
        ], col_widths=[3 * cm, 13 * cm]),

        Spacer(1, 4),
        p("10.1 Pipeline config.json shape", H2),
        code(
            "{\n"
            "  \"llms\": {\n"
            "    \"planner\": [\n"
            "      {\"provider\": \"siliconflow\",\n"
            "       \"model\": \"deepseek-ai/DeepSeek-V4-Flash\",\n"
            "       \"api_key\": \"sk-…\",\n"
            "       \"base_url\": \"https://api.siliconflow.cn/v1\",\n"
            "       \"vision\": false,\n"
            "       \"request_timeout_s\": 120}\n"
            "    ],\n"
            "    \"executor\": [ … ]\n"
            "  },\n"
            "  \"hitl_confirm\": true,\n"
            "  \"high_risk_tools\": [],\n"
            "  \"safe_tools\": [],\n"
            "  \"stream\": true,\n"
            "  \"parallel_tools\": false,\n"
            "  \"sequential_tools\": [],\n"
            "  \"parallel_safe_tools\": []\n"
            "}"),
    ]

    # ── 11. End-to-end walkthrough ──
    story += [
        PageBreak(),
        p("11. End-to-end walkthrough — a 5-minute planner-executor", H1),
        p("Build, generate, and run a tiny planner-executor agent."),

        p("Step 1 — open a project", H3),
        p("Launch MetaAgent. On the <b>welcome launcher</b> click "
          "<b>New project</b>; the canvas designer opens."),

        p("Step 2 — write a tool", H3),
        p("Open the <b>Tool Generator</b> (<b>Tools → Tool Generator</b>, or "
          "<b>Ctrl+T</b>) and ask: <i>\"Write a tool that loads a CSV file "
          "and returns the column means.\"</i> Click <b>Allow</b> when the "
          "review dialog appears, then close the Tool Generator to return to "
          "the canvas."),

        p("Step 3 — insert the pattern", H3),
        p("In the palette, pick <i>Planner–Executor</i> from "
          "<b>Pattern preset</b> and click <b>Insert Pattern</b>. Two agents "
          "and two LLM stubs appear, with a Tools block already wired into "
          "the executor."),

        p("Step 4 — fill in your key", H3),
        p("Double-click each LLM block, paste your API key (or leave blank "
          "to read from an env var), confirm the model and base URL."),

        p("Step 5 — generate", H3),
        p("Set <b>Agent name</b> to <font face='Courier'>csv_helper</font>, "
          "make sure <b>PySide6 desktop GUI</b> is ticked, click <b>Generate Code</b>. "
          "MetaAgent reports the output folder."),

        p("Step 6 — run", H3),
        p("Click <b>Run GUI Agent</b>. Approve the install prompt if it "
          "appears. The chat window opens. Ask:"),
        code("\"Compute the mean of every numeric column in data.csv.\""),
        p("Watch the planner emit a plan, the executor call "
          "<font face='Courier'>load_csv</font>, and the answer stream back."),

        p("Step 7 — ship it", H3),
        p("Click <b>Compile (PyInstaller)</b>. After a few minutes you get "
          "<font face='Courier'>dist/csv_helper/csv_helper.exe</font> and "
          "<font face='Courier'>dist/csv_helper_gui/csv_helper_gui.exe</font> "
          "— with <font face='Courier'>config.json</font> sitting next to "
          "each. Copy them anywhere; no Python install needed."),
    ]

    # ── 12. Layout reference ──
    story += [
        PageBreak(),
        p("12. File-by-file reference", H1),
        table([
            ["File", "Role"],
            ["<font face='Courier'>main.py</font>",
             "Entry point — launches the Qt (PySide6) app."],
            ["<font face='Courier'>canvas_qt/welcome.py</font>",
             "The Qt welcome launcher: branding, New/Open, recent "
             "projects, Settings; opens the canvas in-process."],
            ["<font face='Courier'>canvas_qt/tool_generator.py</font>",
             "The Qt Tool Generator window: coding agent chat, settings "
             "dialog, HITL review dialog."],
            ["<font face='Courier'>coding_agent.py</font>",
             "The built-in coding agent: native function calling "
             "(list_tools, read_tool, save_tool), persistent memory, "
             "HITL gate."],
            ["<font face='Courier'>llm_client.py</font>",
             "Thin OpenAI-compatible chat client used by the coding agent."],
            ["<font face='Courier'>app_config.py</font>",
             "Paths and defaults for MetaAgent itself (its own "
             "<font face='Courier'>config.json</font>)."],
            ["<font face='Courier'>canvas_qt/designer.py</font>",
             "The visual designer: palette, canvas, Debug Run overlay "
             "(QGraphicsView/QGraphicsScene)."],
            ["<font face='Courier'>canvas_qt/dialogs.py</font>",
             "Per-kind node config dialogs for the canvas designer."],
            ["<font face='Courier'>graph_model.py</font>",
             "Pure data model for the canvas: nodes, edges, defaults, "
             "link validation."],
            ["<font face='Courier'>graph_codegen.py</font>",
             "Generates a multi-agent app from a canvas graph."],
            ["<font face='Courier'>codegen.py</font>",
             "Single-agent code generator and shared codegen pieces "
             "(trace, HITL, RAG, image, eval, skills, GUI, server)."],
            ["<font face='Courier'>patterns.py</font>",
             "Registry of pattern presets (ReAct, planner-executor, "
             "planner-executor-critic, supervisor-worker)."],
            ["<font face='Courier'>runner.py</font>",
             "Launch / install / PyInstaller helpers used by the canvas "
             "designer."],
            ["<font face='Courier'>runtime_overlay.py</font>",
             "Pure state machine consumed by the canvas's live Debug Run "
             "overlay."],
            ["<font face='Courier'>templates/</font>",
             "Editable role templates "
             "(<font face='Courier'>prompt_planner.txt</font>, "
             "<font face='Courier'>prompt_worker.txt</font>, …, "
             "<font face='Courier'>system_prompt_template.txt</font>)."],
            ["<font face='Courier'>tools/</font>",
             "Tool library — flat .py files, each with one or more "
             "<font face='Courier'>@tool</font> functions. Seeded with "
             "<font face='Courier'>load_csv</font>."],
            ["<font face='Courier'>graphs/</font>",
             "Saved canvas graphs (JSON)."],
            ["<font face='Courier'>generated_agents/</font>",
             "Output folder for generated agents."],
            ["<font face='Courier'>chat_history.json</font>",
             "Coding agent's persisted conversation."],
            ["<font face='Courier'>config.json</font>",
             "MetaAgent's own config: API key, model, base URL, HITL flag."],
            ["<font face='Courier'>requirements.txt</font>",
             "MetaAgent's own dependencies: "
             "<font face='Courier'>PySide6&gt;=6.6</font> (the Qt UI) and "
             "<font face='Courier'>openai&gt;=1.40.0</font>. "
             "<font face='Courier'>wxPython</font> is no longer required by "
             "the host — it is only needed to run a legacy wx-based generated "
             "agent (each generated agent ships its own "
             "<font face='Courier'>requirements.txt</font>). Add "
             "<font face='Courier'>websockets</font> for WebServer, "
             "<font face='Courier'>mcp</font> for MCP, "
             "<font face='Courier'>pypdf</font> and "
             "<font face='Courier'>python-docx</font> for RAG file "
             "ingestion."],
            ["<font face='Courier'>installer.txt</font>",
             "Reference PyInstaller command for MetaAgent itself."],
        ], col_widths=[5 * cm, 11 * cm]),
    ]

    # ── 13. Troubleshooting ──
    story += [
        PageBreak(),
        p("13. Troubleshooting", H1),

        table([
            ["Symptom", "Fix"],
            ["<i>\"API key missing\"</i> on send",
             "<b>Settings → API Key / Model</b>. The default base URL "
             "<font face='Courier'>https://api.siliconflow.cn/v1</font> "
             "must <i>not</i> include "
             "<font face='Courier'>/chat/completions</font>."],
            ["Coding agent never saves the tool",
             "Look for the <b>review dialog</b> — Deny is the default "
             "button. If you didn't see one, you may have disabled "
             "<font face='Courier'>hitl_confirm</font>."],
            ["Canvas refuses to link two nodes",
             "Check the link-rule table in §4. Most likely cause: a "
             "singleton input (prompt / rag) already exists, or a tool "
             "file would be added twice into the same agent."],
            ["<i>\"Generate failed: …\"</i> dialog",
             "MetaAgent runs a static analyzer that catches missing LLMs, "
             "missing entry, role conflicts, etc. Read the message — it "
             "names the offending node."],
            ["Pop-up: <i>\"No Python interpreter on PATH\"</i> when running "
             "a generated agent",
             "MetaAgent is running as a frozen .exe, so it can't host the "
             "generated agent in-process. Either install Python from "
             "python.org (tick <i>Add to PATH</i>), or run the agent's own "
             "compiled .exe from <font face='Courier'>dist/</font>."],
            ["<i>\"Install failed\"</i> during Run GUI Agent",
             "Open a terminal in the agent folder and run "
             "<font face='Courier'>pip install -r requirements.txt</font> "
             "yourself — the full error will be visible there."],
            ["Anthropic Opus rejects temperature/top-p",
             "Leave them blank in the LLM dialog. Anthropic Opus 4.x is "
             "strict about omitting them."],
            ["RAG <i>Add File…</i> says I'm missing a library",
             "<font face='Courier'>pip install pypdf python-docx</font> "
             "into the agent's environment. Text / markdown / csv / json / "
             "html / py never need anything."],
            ["MCP block shows <i>NOT reachable</i> in Test connection",
             "Check transport (stdio vs http vs sse), the URL path "
             "(<font face='Courier'>/mcp</font> for streamable_http, "
             "<font face='Courier'>/sse</font> for sse), whether your https "
             "is self-signed (uncheck <b>verify TLS</b>), and any proxy "
             "intercepting localhost."],
            ["Generated .exe is huge",
             "You probably skipped the clean "
             "<font face='Courier'>.buildenv</font> step or rebuilt "
             "outside MetaAgent. Use <b>Compile (PyInstaller)</b> from the "
             "designer; it builds inside a minimal venv."],
        ], col_widths=[6 * cm, 10 * cm]),

        Spacer(1, 10),
        rule(),
        p("Happy designing. Iterate quickly: ask the coding agent for a "
          "tool, drop a block on the canvas, generate, run, and refine. "
          "MetaAgent is purpose-built for that loop.", SMALL),
    ]

    return story


def main():
    doc = SimpleDocTemplate(
        OUT_PATH,
        pagesize=A4,
        leftMargin=2 * cm, rightMargin=2 * cm,
        topMargin=2 * cm, bottomMargin=2 * cm,
        title="MetaAgent — agent designer guide",
        author="MetaAgent",
        subject="User guide to designing AI agents with MetaAgent",
    )
    doc.build(build_story(), onFirstPage=page_footer, onLaterPages=page_footer)
    print(f"Wrote {OUT_PATH}")


if __name__ == "__main__":
    main()
