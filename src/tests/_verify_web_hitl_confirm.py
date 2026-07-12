"""Verify the web/WebSocket TOOL-CONFIRM round-trip (item 3, the half
_verify_web_hitl.py didn't cover). The review round-trip and the confirm
round-trip share the _hitl_request bridge but differ on the wire: confirm sends
a 'hitl_confirm' frame and the client replies with a bare boolean (vs a dict).

We can't trigger a tool call without an LLM, so this runs the generated server
IN-PROCESS with core.llm monkeypatched to deterministically emit one high-risk
tool call, then connect a real websocket client and answer the confirm prompt.
Cases: deny -> the tool is blocked ([denied] surfaced); approve -> the tool runs."""

import asyncio
import importlib.util
import json
import os
import shutil
import subprocess
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.chdir(BASE)

subprocess.run([sys.executable, "-m", "pip", "install", "--quiet",
                "websockets"], check=True)
import websockets  # noqa: E402

import app_config  # noqa: E402
import graph_codegen  # noqa: E402
from graph_model import Graph  # noqa: E402

PORT = 18993
LLM = {"provider": "siliconflow", "model": "deepseek-ai/DeepSeek-V4-Flash",
       "api_key": "sk-test", "base_url": "https://api.siliconflow.cn/v1"}
TOOL_NAME = "save_note_hitltest"          # 'save' → is_high_risk → confirm gate
TOOL_SRC = ('from tool_registry import tool\n\n\n'
            '@tool\n'
            f'def {TOOL_NAME}(text: str) -> str:\n'
            '    """Save a note (high-risk name, for the HITL confirm test)."""\n'
            '    return f"saved: {text}"\n')

# write the tool into the shared library so it gets inlined at generation, then
# remove the source (agent.py has it baked in).
tool_file = os.path.join(app_config.TOOLS_DIR, TOOL_NAME + ".py")
with open(tool_file, "w", encoding="utf-8") as f:
    f.write(TOOL_SRC)
try:
    g = Graph()
    a = g.new_node("agent", 0, 0); a.name = "solo"
    llm = g.new_node("llm", 0, 0); llm.props.update(LLM)
    g.add_edge(llm.id, a.id)
    t = g.new_node("tool", 0, 0); t.props["files"] = [TOOL_NAME + ".py"]
    g.add_edge(t.id, a.id)
    wsn = g.new_node("webserver", 0, 0); wsn.props.update(port=PORT)
    assert not graph_codegen.analyze(g)["errors"], graph_codegen.analyze(g)["errors"]
    out = graph_codegen.generate_from_graph(g, "demo_ws_confirm", gui=False)
finally:
    if os.path.exists(tool_file):
        os.remove(tool_file)

# Start from clean runtime state. The disk-storage backend persists every run()
# to sessions/<id>.json; since this test reuses the same generated dir, history
# from prior invocations would otherwise pile up (was seen at 81KB) and skew this
# deterministic, stubbed-LLM round-trip. (Regeneration deliberately keeps user
# chat history in production, so this isolation lives in the test, not codegen.)
for _sub in ("sessions", "memory", "checkpoints"):
    shutil.rmtree(os.path.join(out, _sub), ignore_errors=True)

# Import the generated agent UNDER the name "agent" so the server's
# `import agent as core` resolves to this (about-to-be-patched) module object.
aspec = importlib.util.spec_from_file_location("agent", os.path.join(out, "agent.py"))
core = importlib.util.module_from_spec(aspec)
sys.modules["agent"] = core
aspec.loader.exec_module(core)
assert TOOL_NAME in core.AGENTS["solo"]["tools"], core.AGENTS["solo"]["tools"]

# Deterministic, network-free LLM: round 1 emits the high-risk tool call; round 2
# echoes the last tool result so the client can SEE approve vs deny.
state = {"round": 0}


def fake_llm(agent_name, system, messages, emit=print):
    state["round"] += 1
    if state["round"] == 1:
        return ("", [{"id": "c1", "name": TOOL_NAME, "args": {"text": "hello"}}])
    last = messages[-1].get("content", "") if messages else ""
    return ("final:" + last, [])


core.llm = fake_llm

sspec = importlib.util.spec_from_file_location("server", os.path.join(out, "server.py"))
server = importlib.util.module_from_spec(sspec)
sspec.loader.exec_module(server)
assert server.core is core, "server must use the patched agent module"
assert server.AUTO_ALLOW is False, "default must prompt, not auto-allow"
print("setup ok: in-process server bound to a patched, network-free agent")

URL = f"ws://127.0.0.1:{PORT}"


async def drive(allow: bool):
    """Send a task, answer the hitl_confirm prompt with `allow`, return result."""
    state["round"] = 0
    saw_confirm = False
    async with websockets.connect(URL, proxy=None) as ws:
        assert json.loads(await ws.recv())["type"] == "hello"
        await ws.send(json.dumps({"type": "task", "task": "please save a note"}))
        while True:
            m = json.loads(await asyncio.wait_for(ws.recv(), timeout=20))
            t = m["type"]
            if t == "hitl_confirm":
                saw_confirm = True
                assert "id" in m and "prompt" in m, m
                await ws.send(json.dumps({"type": "hitl_response",
                                          "id": m["id"], "result": allow}))
            elif t == "result":
                assert saw_confirm, "no hitl_confirm frame arrived before the result"
                return m["result"]
            elif t == "error":
                raise AssertionError(f"server error: {m['error']}")


async def main():
    # HITL is per-run now (the server passes each connection's confirm/review
    # bridge into core.run), so there's no global handler to pre-install.
    async with websockets.serve(server.handle, "127.0.0.1", PORT):
        denied = await drive(False)
        assert "denied" in denied.lower(), denied
        print("ok: deny over the wire -> tool blocked ([denied] surfaced)")

        approved = await drive(True)
        assert "saved: hello" in approved, approved
        print("ok: approve over the wire -> tool actually ran")
    return True


assert asyncio.run(main())
print("\nWEB HITL CONFIRM (tool) ROUND-TRIP CHECKS PASSED")
