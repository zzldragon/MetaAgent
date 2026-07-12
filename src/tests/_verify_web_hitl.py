"""Verify the web/WebSocket HITL round-trip (roadmap item 3): a review
checkpoint now prompts the connected browser and BLOCKS the run until the
client answers, instead of auto-approving. Uses an entry-gate review
(hitl_review) + a tripped wall-clock budget so the flow is deterministic and
needs no LLM call: review fires first, then react trips the budget on approve.

Cases: approve -> run proceeds (result is the [budget] message); reject ->
run stops with the rejection message; disconnect mid-review -> the worker is
unblocked (the server doesn't hang)."""

import asyncio
import json
import os
import py_compile
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.chdir(BASE)

subprocess.run([sys.executable, "-m", "pip", "install", "--quiet",
                "websockets"], check=True)
import websockets  # noqa: E402

import graph_codegen  # noqa: E402
from graph_model import Graph  # noqa: E402

LLM = {"provider": "siliconflow", "model": "deepseek-ai/DeepSeek-V4-Flash",
       "api_key": "sk-test", "base_url": "https://api.siliconflow.cn/v1"}
PORT = 18992

# entry agent with a review gate; -1 wall clock trips the budget after approval
g = Graph()
a = g.new_node("agent", 0, 0); a.name = "solo"
a.props["hitl_review"] = True
a.props["max_wall_clock_s"] = -1
llm = g.new_node("llm", 0, 0); llm.props.update(LLM)
g.add_edge(llm.id, a.id)
ws_node = g.new_node("webserver", 0, 0); ws_node.props.update(port=PORT)
assert not graph_codegen.analyze(g)["errors"], graph_codegen.analyze(g)["errors"]

out_dir = graph_codegen.generate_from_graph(g, "demo_ws_hitl", gui=False)
py_compile.compile(os.path.join(out_dir, "server.py"), doraise=True)
cfg = json.load(open(os.path.join(out_dir, "config.json"), encoding="utf-8"))
assert cfg["server"]["auto_allow_tools"] is False, "default must prompt, not auto-allow"
print("codegen ok: server.py compiles, auto_allow_tools defaults False (will prompt)")

# Source-level invariant: on cancel/disconnect an abandoned run must default to
# REJECT (review) / DENY (tool), never approve. The live disconnect test below
# proves the worker UNBLOCKS (liveness) but cannot observe the vanished run's
# decision, so pin the default here — an approve-on-disconnect regression would
# flip these markers.
_srv = open(os.path.join(out_dir, "server.py"), encoding="utf-8").read()
assert '"decision": "reject"' in _srv and "client disconnected" in _srv, \
    "web review disconnect default must be reject"
assert "default=False" in _srv, "web tool-confirm disconnect default must be deny"
print("ok: server source pins reject/deny defaults for cancel/disconnect")

proc = subprocess.Popen([sys.executable, "server.py"], cwd=out_dir,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def wait_for_port(port, timeout=60.0):
    import socket
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise AssertionError("server exited early:\n"
                                 + (proc.stdout.read() or "")[-1000:])
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return
        except OSError:
            time.sleep(0.5)
    proc.kill()
    raise AssertionError("server never started listening")


wait_for_port(PORT)
print("server is listening")

URL = f"ws://127.0.0.1:{PORT}"


async def drive(decision, feedback=""):
    """Send a task, answer the hitl_review prompt with `decision`, return the
    final result string."""
    async with websockets.connect(URL, proxy=None) as ws:
        assert json.loads(await ws.recv())["type"] == "hello"
        await ws.send(json.dumps({"type": "task", "task": "do it"}))
        saw_review = False
        while True:
            m = json.loads(await asyncio.wait_for(ws.recv(), timeout=20))
            t = m["type"]
            if t == "hitl_review":
                saw_review = True
                assert "id" in m, m
                result = {"decision": decision, "content": m.get("content", ""),
                          "feedback": feedback}
                await ws.send(json.dumps({"type": "hitl_response",
                                          "id": m["id"], "result": result}))
            elif t == "result":
                assert saw_review, "review prompt never arrived before the result"
                return m["result"]
            elif t == "error":
                raise AssertionError(f"server error: {m['error']}")
            # else token/trace: keep going


async def disconnect_during_review():
    """Open, trigger the review, then close without answering — the server must
    resolve the pending prompt (reject default) and not hang. Verified by a
    fresh connection completing a normal approve afterward."""
    ws = await websockets.connect(URL, proxy=None)
    assert json.loads(await ws.recv())["type"] == "hello"
    await ws.send(json.dumps({"type": "task", "task": "do it"}))
    while True:
        m = json.loads(await asyncio.wait_for(ws.recv(), timeout=20))
        if m["type"] == "hitl_review":
            break
    await ws.close()                       # walk away mid-review


async def main():
    approved = await drive("approve")
    assert approved.startswith("[budget]"), approved
    print("ok: approve -> run proceeds past the gate (result is the budget trip)")

    rejected = await drive("reject", feedback="not allowed")
    assert "rejected by human" in rejected, rejected
    print("ok: reject -> run stops with the human-rejection message")

    await disconnect_during_review()
    # the server must still serve a new run (no deadlock from the abandoned prompt)
    again = await asyncio.wait_for(drive("approve"), timeout=40)
    assert again.startswith("[budget]"), again
    print("ok: disconnect mid-review is resolved; server stays responsive")
    return True


try:
    assert asyncio.run(main())
    print("\nWEB HITL ROUND-TRIP CHECKS PASSED")
finally:
    proc.kill()
    proc.wait(timeout=10)
