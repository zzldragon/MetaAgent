"""Verify the PictureBook Canvas agent — a re-creation of PictureBookGenerator
(LangGraph) improved with PARALLEL page illustration.

Builds the graph in memory (author -> illustrator worker-pool -> bookbinder),
checks the committed graphs/PictureBookAgent.mta has the same shape, then
generates -> compiles -> runs it offline with the LLM and the SiliconFlow image
API stubbed. Proves: the page images are produced CONCURRENTLY by the pool (a
Barrier would dead-lock under serial execution); the CJK font path + caption
wrapping render a Chinese book; each book gets its own output subfolder; and the
review-fix guards (fail-loud missing char-ref, idempotent char-ref) hold.
No network. reportlab-guarded so it stays green where reportlab is absent."""

import importlib.util
import json
import os
import py_compile
import re
import shutil
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.chdir(BASE)

import graph_codegen
from app_config import TOOLS_DIR
from graph_model import Graph, load_mta

MTA = os.path.join(BASE, "graphs", "PictureBookAgent.mta")
LLM = dict(provider="siliconflow", model="Qwen/Qwen3-8B",
           api_key="", base_url="https://api.siliconflow.cn/v1")
TOOLS = ["picturebook_tools.py"]

AUTHOR_PROMPT = "You are a children's picture-book author. Build the book via tools."
ILLUSTRATOR_PROMPT = ("You illustrate ONE page. The PAGE NUMBER is the integer on the LAST "
                      "line of your message. Call generate_page_image(page=<it>) once.")
BOOKBINDER_PROMPT = "Assemble the book: call make_picture_book_pdf() once."

AUTHOR_BUDGETS = {"max_iterations": 60, "max_tool_calls": 120, "max_wall_clock_s": 900}
ILLUS_BUDGETS = {"max_iterations": 20, "max_tool_calls": 120, "max_wall_clock_s": 900}
BINDER_BUDGETS = {"max_iterations": 10, "max_tool_calls": 20, "max_wall_clock_s": 300}


def build():
    """Construct the PictureBook graph (also how graphs/PictureBookAgent.mta was made)."""
    g = Graph()

    def agent_with(name, role, prompt_text, budgets, *, pool=False, max_workers=4, tools=None):
        node = g.new_node("workerpool" if pool else "agent", 0, 0)
        node.name = name
        node.props["role"] = role
        node.props.update(budgets)
        node.props["hitl_triggers"] = []          # creative gen tools — no HITL gating
        if pool:
            node.props["max_workers"] = max_workers
        lm = g.new_node("llm", 0, 0); lm.name = f"llm_{name}"; lm.props.update(LLM)
        g.add_edge(lm.id, node.id)
        pr = g.new_node("prompt", 0, 0); pr.name = f"prompt_{name}"
        pr.props["role"] = role if role in ("single", "worker") else "single"
        pr.props["text"] = prompt_text
        g.add_edge(pr.id, node.id)
        t = g.new_node("tool", 0, 0); t.name = f"tools_{name}"
        t.props["files"] = list(tools or TOOLS)   # role-scoped tool files
        g.add_edge(t.id, node.id)
        return node

    # role-scoped tools: only the AUTHOR gets start_book (the owner file defines _BOOK
    # + setup tools); the others get one tool each, sharing _BOOK via the inlined ns.
    author = agent_with("author", "single", AUTHOR_PROMPT, AUTHOR_BUDGETS,
                        tools=["picturebook_tools.py"])
    illustrator = agent_with("illustrator", "single", ILLUSTRATOR_PROMPT, ILLUS_BUDGETS,
                             tools=["picturebook_illustrate_tools.py"])
    bookbinder = agent_with("bookbinder", "single", BOOKBINDER_PROMPT, BINDER_BUDGETS,
                           tools=["picturebook_pdf_tools.py"])
    g.add_edge(author.id, illustrator.id)
    g.add_edge(illustrator.id, bookbinder.id)
    gui = g.new_node("gui", 0, 0); gui.name = "desktop_gui"
    g.add_edge(gui.id, author.id)
    return g


def _assert_shape(g, label):
    info = graph_codegen.analyze(g)
    assert not info["errors"], (label, info["errors"])
    assert info["mode"] == "chain", (label, info["mode"])
    names = [g.nodes[a].name for a in info["pipeline"]]
    assert names == ["author", "illustrator", "bookbinder"], (label, names)


# 1. build() is the canonical 3-stage chain the rich run below exercises. The
# committed bundle is the SHIPPED agent, which has since been enhanced beyond the
# bare chain into a conditional graph (a DoesUserAskForAPictureBook gate ->
# illustrator/end, plus a webserver), so it now analyzes as "graph", not "chain".
# Assert it still loads, is VALID, keeps the author->illustrator->bookbinder
# spine, and generates+compiles — then run the parallel-illustration test on the
# canonical chain from build().
built = build()
_assert_shape(built, "built")
assert os.path.isfile(MTA), f"deliverable missing: {MTA}"
g2, _ = load_mta(MTA, TOOLS_DIR)
info2 = graph_codegen.analyze(g2)
assert not info2["errors"], ("committed .mta", info2["errors"])
kinds2 = {n.name: n.kind for n in g2.nodes.values()}
assert kinds2.get("author") == "agent", kinds2
assert kinds2.get("illustrator") == "agent", kinds2   # fan-out now in-tool, not a workerpool
assert kinds2.get("bookbinder") == "agent", kinds2
assert kinds2.get("checker") == "agent" and kinds2.get("gate") == "while", kinds2
# The loop counter must be written DETERMINISTICALLY, not by an unreliable LLM: the
# checker is FORCED to call sync_page_status (tool_choice=specific), which writes the
# true pages_illustrated into shared state itself and is return_direct. Without this
# the LLM printed JSON that was never applied -> the counter stuck at 0 -> the gate
# always looped to max_iterations. Verified against the committed .mta node props.
_n2 = {n.name: n for n in g2.nodes.values()}
assert (_n2["llm_checker"].props.get("tool_choice"),
        _n2["llm_checker"].props.get("tool_choice_name")) == ("specific", "sync_page_status"), \
    _n2["llm_checker"].props
assert _n2["tools_checker"].props.get("tool_props", {}).get(
    "sync_page_status", {}).get("return_direct") is True, _n2["tools_checker"].props
# the author must write the book in the USER's language (title + page sentences),
# while image prompts stay English (the image model expects English).
_auth = _n2["prompt_author"].props.get("text", "")
assert all(w in _auth for w in ("Chinese", "Japanese", "French")), \
    "author prompt must carry the language-matching rule"
# the single-tool worker LLMs FORCE their exact tool (tool_choice=specific). 'any'
# (-> OpenAI 'required') was intermittently ignored by DeepSeek, leaving the
# bookbinder empty and producing NO pdf; the explicit named-function force is honored.
assert (_n2["llm_bookbinder"].props.get("tool_choice"),
        _n2["llm_bookbinder"].props.get("tool_choice_name")) == (
        "specific", "make_picture_book_pdf"), _n2["llm_bookbinder"].props
assert (_n2["llm_illustrator"].props.get("tool_choice"),
        _n2["llm_illustrator"].props.get("tool_choice_name")) == (
        "specific", "illustrate_missing_pages"), _n2["llm_illustrator"].props
_shipped = graph_codegen.generate_from_graph(g2, "verify_picturebook_shipped", gui=True)
# generated config carries the forced-tool + return-direct wiring end-to-end
_scfg = json.load(open(os.path.join(_shipped, "config.json"), encoding="utf-8"))
assert "sync_page_status" in (_scfg.get("return_direct_tools") or []), _scfg.get("return_direct_tools")
assert (_scfg.get("llms", {}).get("checker") or [{}])[0].get("tool_choice") == "specific"
try:
    py_compile.compile(os.path.join(_shipped, "agent.py"), doraise=True)
    py_compile.compile(os.path.join(_shipped, "gui.py"), doraise=True)
finally:
    shutil.rmtree(_shipped, ignore_errors=True)
print("ok 1: build() is a 3-stage chain; committed .mta is a valid graph "
      "(spine intact, generates+compiles)")

# 2. generate + compile + import  (the canonical chain from build())
out = graph_codegen.generate_from_graph(built, "verify_picturebook", gui=True)
try:
    py_compile.compile(os.path.join(out, "agent.py"), doraise=True)
    py_compile.compile(os.path.join(out, "gui.py"), doraise=True)
    spec = importlib.util.spec_from_file_location("vpb_agent", os.path.join(out, "agent.py"))
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)

    assert mod.PATTERN_MODE == "chain" and mod.PIPELINE == ["author", "illustrator", "bookbinder"]
    # role-scoped tools: only the AUTHOR may reset the book (start_book) — never the
    # illustrator/bookbinder (a rogue start_book call wipes the whole book). Fan-out
    # now lives INSIDE illustrate_missing_pages (in-tool ThreadPool), not a workerpool.
    assert "illustrate_missing_pages" in mod.AGENTS["illustrator"]["tools"]
    assert "make_picture_book_pdf" in mod.AGENTS["bookbinder"]["tools"]
    assert "start_book" in mod.AGENTS["author"]["tools"]
    assert "start_book" not in mod.AGENTS["illustrator"]["tools"]
    assert "start_book" not in mod.AGENTS["bookbinder"]["tools"]
    assert set(graph_codegen._tool_names("picturebook_tools.py")) == {
        "start_book", "set_character", "add_page", "generate_character_ref", "book_status"}
    assert graph_codegen._tool_names("picturebook_illustrate_tools.py") == ["illustrate_missing_pages"]
    assert graph_codegen._tool_names("picturebook_pdf_tools.py") == ["make_picture_book_pdf"]
    assert graph_codegen._tool_names("picturebook_check_tools.py") == [
        "sync_page_status", "pages_report"]
    # image-gen stages must NOT keep the fatal default 60s wall clock
    assert mod.AGENTS["author"]["budgets"]["max_wall_clock_s"] >= 600
    assert mod.AGENTS["illustrator"]["budgets"]["max_wall_clock_s"] >= 600
    assert mod.AGENTS["illustrator"]["budgets"]["max_tool_calls"] >= 100
    print("ok 2: role-scoped tools (author-only start_book; shared _BOOK across files); "
          "illustrator = illustrate_missing_pages (in-tool fan-out), budgets raised")

    # 3. offline run — stub the image API (no network) with a VALID 1x1 PNG. A
    # Barrier(N) PROVES concurrency: each page worker waits for all N; serial
    # execution would time out (BrokenBarrierError).
    import base64
    import threading
    PNG = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
    tmp = tempfile.mkdtemp(prefix="pbook_")
    gen_calls = {"ref": 0, "pages": []}
    N = 3
    page_barrier = threading.Barrier(N, timeout=20)

    def fake_gen(prompt, out_path, source_path="", **kw):
        if "character_ref" in out_path:
            gen_calls["ref"] += 1
        else:
            gen_calls["pages"].append(out_path)
            page_barrier.wait()          # all N page workers must be in flight at once
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        with open(out_path, "wb") as f:
            f.write(PNG)
        return out_path
    mod._PB.gen_image = staticmethod(fake_gen)

    TITLE = "勇敢的小狐狸"      # a Chinese book → exercises CJK fonts + char-level wrap
    SENTENCES = ["从前有一只勇敢的小狐狸，他住在一片温暖的森林里，每天都在寻找新的朋友和新的冒险旅程。",
                 "小狐狸遇到了一只迷路的小兔子，他决定帮助小兔子找到回家的路，于是他们一起出发了。",
                 "最后他们成为了最好的朋友，小狐狸明白了分享与友谊比任何宝藏都更加珍贵。"]
    astep = {"n": 0}

    def fake_call(agent_name, cfg, system, messages):
        if agent_name == "author":
            i = astep["n"]; astep["n"] += 1
            seq = [{"name": "start_book", "args": {"title": TITLE,
                    "language": "Chinese", "output_dir": tmp}},
                   {"name": "set_character", "args": {"main_character": "a small red fox",
                    "character_ref_prompt": "Character reference sheet, warm style, a small red fox."}}]
            seq += [{"name": "add_page", "args": {"page": p, "sentence": SENTENCES[p - 1],
                     "image_prompt": f"scene {p}", "main_char_present": (p != 1)}}
                    for p in range(1, N + 1)]
            seq += [{"name": "generate_character_ref", "args": {}}]
            if i < len(seq):
                tc = seq[i]
                return ("", [{"id": f"a{i}", "name": tc["name"], "args": tc["args"]}])
            return ("[" + ", ".join(str(p) for p in range(1, N + 1)) + "]", [])
        if agent_name == "illustrator":     # single agent — calls the batch tool once
            if any(m.get("role") == "tool" for m in messages):
                return ("done", [])
            return ("", [{"id": "g", "name": "illustrate_missing_pages", "args": {}}])
        if agent_name == "bookbinder":
            tool_msgs = [m for m in messages if m.get("role") == "tool"]
            if tool_msgs:                       # echo the PDF tool's result outward
                return (str(tool_msgs[-1].get("content", "")), [])
            return ("", [{"id": "b", "name": "make_picture_book_pdf", "args": {}}])
        return ("?", [])
    mod._call_one = fake_call

    result = mod.run("写一个关于勇敢小狐狸的绘本。", emit=lambda s: None)

    BOOK = getattr(mod, "_BOOK", {})
    run_dir = BOOK.get("output_dir", "")
    assert gen_calls["ref"] == 1, "character reference generated exactly once"
    assert len(gen_calls["pages"]) == N, f"all {N} page images generated (pool fan-out): {gen_calls}"
    assert all(BOOK["pages"][p]["image_path"] for p in range(1, N + 1)), "every page illustrated"
    assert not page_barrier.broken, "the page barrier tripped — pages ran CONCURRENTLY"
    assert run_dir == os.path.join(tmp, mod._PB.slug(TITLE)), f"per-book subfolder: {run_dir}"
    print(f"ok 3: {N} page images generated CONCURRENTLY (barrier cleared) into a per-book subfolder")

    # 4. PDF assembly (reportlab-guarded): CJK fonts registered + a non-trivial PDF
    try:
        import reportlab  # noqa: F401
        have_rl = True
    except ImportError:
        have_rl = False
    assert mod._PB.has_cjk(TITLE) and not mod._PB.has_cjk("hello")
    if have_rl:
        assert "Saved picture book" in result, result
        pdfs = [f for f in os.listdir(run_dir) if f.endswith(".pdf")]
        assert pdfs and os.path.getsize(os.path.join(run_dir, pdfs[0])) > 1000
        fonts = mod._PB.register_fonts()
        if any(os.path.isfile(p) for p in (r"C:\Windows\Fonts\msyh.ttc",
                                           r"C:\Windows\Fonts\simhei.ttf",
                                           "/System/Library/Fonts/PingFang.ttc")):
            assert fonts == ("Book", "BookBold"), f"expected registered CJK font, got {fonts}"
        print(f"ok 4: CJK picture-book PDF assembled ({os.path.getsize(os.path.join(run_dir, pdfs[0]))} "
              f"bytes), fonts={fonts}")
    else:
        assert "reportlab is not installed" in result, result
        print("ok 4: reportlab absent — make_picture_book_pdf returns a clear message")

    # 5. robustness guards (review fixes) on a fresh book
    def fake_gen2(prompt, out_path, source_path="", **kw):     # no barrier (serial)
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        with open(out_path, "wb") as f:
            f.write(PNG)
        return out_path
    mod._PB.gen_image = staticmethod(fake_gen2)
    T = mod.TOOLS
    T["start_book"]("守护者", "Chinese", tmp)
    T["set_character"]("a knight", "knight reference sheet")
    T["add_page"](1, "第一页", "scene", True)            # main-char page needs the ref
    guard = T["illustrate_missing_pages"]()               # ref not generated yet
    assert "character reference" in guard and "missing" in guard.lower(), guard
    assert "saved" in T["generate_character_ref"]().lower()
    assert "already exists" in T["generate_character_ref"]()   # idempotent: no re-bill
    r5 = T["illustrate_missing_pages"]()                  # now the page succeeds
    assert "1/1" in r5, r5
    assert "already illustrated" in T["illustrate_missing_pages"]()   # idempotent skip
    print("ok 5: char-ref-missing surfaced in report; generate_character_ref idempotent; "
          "illustrate_missing_pages then completes + is idempotent")

    # 6. API key resolution — a MetaAgent-generated config stores keys per-LLM
    # under config['llms'][agent][i]['api_key'], NOT a top-level key. The image
    # tools must find it there (regression: "no api key" despite keys being set).
    real_cfg = mod._PB.cfg
    GEN = {"llms": {"author": [{"api_key": "sk-gen-123",
                                "base_url": "https://api.siliconflow.cn/v1"}],
                    "illustrator": [{"api_key": "sk-gen-123",
                                     "base_url": "https://api.siliconflow.cn/v1"}]}}
    mod._PB.cfg = staticmethod(lambda: GEN)
    assert mod._PB.api_key() == "sk-gen-123", "key must come from config['llms']"
    mod._PB.cfg = staticmethod(lambda: {"deepseek_api_key": "sk-top"})   # standalone
    assert mod._PB.api_key() == "sk-top"
    mod._PB.cfg = staticmethod(lambda: {"llms": {}})                     # no key set
    os.environ.pop("SILICONFLOW_API_KEY", None); os.environ.pop("DEEPSEEK_API_KEY", None)
    assert mod._PB.api_key() == ""
    os.environ["SILICONFLOW_API_KEY"] = "sk-env"
    assert mod._PB.api_key() == "sk-env"                                 # env fallback
    os.environ.pop("SILICONFLOW_API_KEY", None)
    mod._PB.cfg = real_cfg
    print("ok 6: image API key resolves from config['llms'] (+ top-level / env fallbacks)")

    shutil.rmtree(tmp, ignore_errors=True)
    print("\nALL PICTUREBOOK CHECKS PASSED")
finally:
    shutil.rmtree(out, ignore_errors=True)
