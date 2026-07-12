"""Verify token streaming: text deltas reach the sink, tool calls still
assemble from the stream, pool/parallel threads don't stream, and STREAM=False
falls back to a single blocking call. Uses a fake OpenAI client — no network."""

import importlib.util
import os
import sys
import threading

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.chdir(BASE)

import graph_codegen
from graph_model import Graph

LLM = {"provider": "siliconflow", "model": "deepseek-ai/DeepSeek-V4-Flash",
       "api_key": "sk-test", "base_url": "https://api.siliconflow.cn/v1"}

# ── build + load a single-agent pipeline with one tool ──────────────────────
g = Graph()
a = g.new_node("agent", 0, 0); a.name = "agent"
llm = g.new_node("llm", 0, 0); llm.props.update(LLM)
g.add_edge(llm.id, a.id)
tool = g.new_node("tool", 0, 0); tool.props["files"] = ["load_csv.py"]
g.add_edge(tool.id, a.id)
out_dir = graph_codegen.generate_from_graph(g, "demo_stream", gui=False)

spec = importlib.util.spec_from_file_location(
    "demo_stream_agent", os.path.join(out_dir, "agent.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


# ── fake OpenAI client: dual-mode (stream / blocking) ───────────────────────
class U:
    prompt_tokens = 10
    completion_tokens = 5

class Delta:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls

class Choice:
    def __init__(self, delta): self.delta = delta

class Chunk:
    def __init__(self, choices=None, usage=None):
        self.choices = choices or []
        self.usage = usage

class TCFn:
    def __init__(self, name=None, arguments=None):
        self.name = name; self.arguments = arguments

class TCDelta:
    def __init__(self, index, id=None, name=None, arguments=None):
        self.index = index; self.id = id
        self.function = TCFn(name, arguments)

class Msg:
    def __init__(self, content=None, tool_calls=None):
        self.content = content; self.tool_calls = tool_calls

class BChoice:
    def __init__(self, msg): self.message = msg

class Resp:
    def __init__(self, msg): self.choices = [BChoice(msg)]; self.usage = U()

FAKE = {"mode": "text", "stream_seen": None}

class Completions:
    def create(self, **kw):
        FAKE["stream_seen"] = kw.get("stream", False)
        if kw.get("stream"):
            if FAKE["mode"] == "text":
                return iter([Chunk([Choice(Delta(content="Hel"))]),
                             Chunk([Choice(Delta(content="lo"))]),
                             Chunk([], U())])
            # tool-call stream: name in one chunk, args split across two
            return iter([
                Chunk([Choice(Delta(tool_calls=[TCDelta(0, id="c1",
                                                        name="load_csv")]))]),
                Chunk([Choice(Delta(tool_calls=[TCDelta(0,
                                                        arguments='{"path":')]))]),
                Chunk([Choice(Delta(tool_calls=[TCDelta(0,
                                                        arguments=' "x.csv"}')]))]),
                Chunk([], U())])
        return Resp(Msg(content="Hello", tool_calls=None))

class Chat:
    completions = Completions()

class FakeOpenAI:
    def __init__(self, **kw): self.chat = Chat()

mod.OpenAI = FakeOpenAI
cfg = mod.CONFIG["llms"]["agent"][0]

# 1. text streaming forwards deltas in order
mod._clients.clear(); mod._TOK.on_token = None
FAKE["mode"] = "text"
got = []
mod._TOK.on_token = got.append
text, calls = mod._call_one("agent", cfg, "sys", [{"role": "user", "content": "hi"}])
assert got == ["Hel", "lo"], got
assert text == "Hello" and calls == [], (text, calls)
assert FAKE["stream_seen"] is True
assert mod.USAGE["agent"]["input_tokens"] == 10
print("text streaming ok: deltas forwarded, text assembled, usage tracked")

# 2. tool calls assemble correctly from streamed deltas
mod._clients.clear()
FAKE["mode"] = "tools"
got = []
mod._TOK.on_token = got.append
text, calls = mod._call_one("agent", cfg, "sys", [{"role": "user", "content": "hi"}])
assert got == [], "tool-only stream should emit no text tokens"
assert calls == [{"id": "c1", "name": "load_csv", "args": {"path": "x.csv"}}], calls
print("tool-call streaming ok: name + split args assembled, parsed")

# 3. react streams + emits a header, not a duplicated text block
mod._clients.clear(); FAKE["mode"] = "text"
emits = []
got = []
mod._TOK.on_token = got.append
result = mod.react("agent", "hi", emit=emits.append)
assert result == "Hello", result
assert any("--- [agent] step 1 ---" in e for e in emits), emits
assert not any("Hello" in e for e in emits), "text must stream, not be re-emitted"
assert "".join(got) == "Hello"
print("react streaming ok: header emitted, text streamed (not duplicated)")

# 4. a separate thread (i.e. a pool worker) has NO sink -> won't stream
mod._TOK.on_token = lambda d: None
seen = {}
t = threading.Thread(target=lambda: seen.update(sink=mod._token_sink()))
t.start(); t.join()
assert seen["sink"] is None, "pool worker threads must not inherit the sink"
print("pool isolation ok: worker threads don't stream")

# 5. STREAM=False -> blocking path even with a sink set
mod._clients.clear(); mod.STREAM = False
got = []
mod._TOK.on_token = got.append
text, calls = mod._call_one("agent", cfg, "sys", [{"role": "user", "content": "hi"}])
assert FAKE["stream_seen"] is False, "STREAM=False must not request a stream"
assert text == "Hello" and got == []
mod.STREAM = True
print("stream toggle ok: STREAM=False uses one blocking call")

# config carries the flag
import json
gen_cfg = json.load(open(os.path.join(out_dir, "config.json"), encoding="utf-8"))
assert gen_cfg.get("stream") is True
print("config ok: stream flag present")

print("\nALL STREAMING CHECKS PASSED")
