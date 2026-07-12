"""Verify the WebServer module: graph rules, generation, and a real
WebSocket round-trip against the generated server (no LLM calls — the test
agent's wall-clock budget is tripped so run() returns a budget message)."""

import asyncio
import importlib.util
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
PORT = 18999
TOKEN = "secret123"

# 1. Graph rules
g = Graph()
solo = g.new_node("agent", 0, 0)
solo.name = "solo"
solo.props["max_wall_clock_s"] = -1   # trip the budget: no LLM needed
llm = g.new_node("llm", 0, 0)
llm.props.update(LLM)
assert g.add_edge(llm.id, solo.id) is None

ws1 = g.new_node("webserver", 0, 0)
ws1.props.update(port=PORT, auth_token=TOKEN)
ws2 = g.new_node("webserver", 0, 0)
errs = graph_codegen.analyze(g)["errors"]
assert any("Only one WebServer" in e for e in errs), errs
g.remove_node(ws2.id)
ws1.props["port"] = 99999
errs = graph_codegen.analyze(g)["errors"]
assert any("invalid port" in e for e in errs), errs
ws1.props["port"] = PORT
assert not graph_codegen.analyze(g)["errors"]
print("webserver graph rules ok")

# 2. Generation artifacts
out_dir = graph_codegen.generate_from_graph(g, "demo_ws", gui=True)
py_compile.compile(os.path.join(out_dir, "server.py"), doraise=True)
with open(os.path.join(out_dir, "config.json"), encoding="utf-8") as f:
    cfg = json.load(f)
assert cfg["server"]["port"] == PORT and cfg["server"]["auth_token"] == TOKEN
reqs = open(os.path.join(out_dir, "requirements.txt")).read()
assert "websockets" in reqs, reqs
print("webserver codegen ok: server.py + config + requirements")

# 3. Live round-trip
proc = subprocess.Popen([sys.executable, "server.py"], cwd=out_dir,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        text=True)


def wait_for_port(port: int, timeout: float = 60.0) -> None:
    """The server's imports (openai SDK) can take several seconds."""
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

# 3a. Web UI: a browser GET / returns the chat page on the same port
import urllib.error
import urllib.request

# Bypass any system/env proxy: a proxy that intercepts localhost answers
# "403 Forbidden (Tunnel connection failed)", which is an environment artifact,
# not a server problem. Talk to 127.0.0.1 directly.
urllib.request.install_opener(
    urllib.request.build_opener(urllib.request.ProxyHandler({})))

html = urllib.request.urlopen(
    f"http://127.0.0.1:{PORT}/", timeout=10).read().decode("utf-8")
assert "<!DOCTYPE html>" in html, html[:120]
assert "demo_ws" in html                      # agent name in title/header
assert "new WebSocket" in html                # the JS client
assert '"type": "task"' in html.replace("'", '"') or "type: " in html or "task" in html
assert 'id="stop"' in html and "cancel" in html   # Stop button + cancel wire
print("web ui has a Stop button")
try:
    urllib.request.urlopen(f"http://127.0.0.1:{PORT}/nope", timeout=10)
    raise AssertionError("expected 404")
except urllib.error.HTTPError as e:
    assert e.code == 404, e.code
print("web ui ok: GET / serves the chat page, unknown paths 404")


async def round_trip():
    async with websockets.connect(f"ws://127.0.0.1:{PORT}", proxy=None) as ws:
        hello = json.loads(await ws.recv())
        assert hello["type"] == "hello" and hello["auth_required"], hello

        # wrong token -> error + close
        await ws.send(json.dumps({"type": "auth", "token": "nope"}))
        err = json.loads(await ws.recv())
        assert err["type"] == "error" and "auth" in err["error"], err

    async with websockets.connect(f"ws://127.0.0.1:{PORT}", proxy=None) as ws:
        await ws.recv()  # hello
        await ws.send(json.dumps({"type": "auth", "token": TOKEN}))
        assert json.loads(await ws.recv())["type"] == "auth_ok"

        await ws.send(json.dumps({"type": "ping"}))
        assert json.loads(await ws.recv())["type"] == "pong"

        await ws.send(json.dumps({"type": "task", "task": "hello agent"}))
        kinds, result = [], None
        while True:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
            kinds.append(msg["type"])
            if msg["type"] == "result":
                result = msg["result"]
                break
            assert msg["type"] == "trace", msg
        assert result.startswith("[budget]"), result
        assert "trace" in kinds, kinds  # the [trace] path line at minimum

        # a cancel message is processed live (the server acks with a trace)
        await ws.send(json.dumps({"type": "cancel"}))
        cm = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        assert cm["type"] == "trace" and "cancelling" in cm["text"], cm
    return True


try:
    assert asyncio.run(round_trip())
    print("websocket round-trip ok: auth, ping, task -> traces -> result, cancel")
finally:
    proc.kill()
    proc.wait(timeout=10)

# 4. The generated GUI can enable/disable the server (Server menu)
import socket

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication  # noqa: E402

sys.path.insert(0, out_dir)
import gui  # noqa: E402  (generated gui.py)

app = QApplication.instance() or QApplication([])
frame = gui.ChatFrame()
titles = [a.text() for a in frame.menuBar().actions()]
assert any("Server" in t for t in titles), ("Server menu missing", titles)

frame._srv_action.setChecked(True)
frame._start_server()
assert frame._server_proc is not None
deadline = time.time() + 60
up = False
while time.time() < deadline:
    if frame._server_proc.poll() is not None:
        raise AssertionError("server subprocess exited early")
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=1):
            up = True
            break
    except OSError:
        time.sleep(0.5)
assert up, "GUI-started server never listened"
print("gui server toggle ok: started and listening")

srv_proc = frame._server_proc
frame._stop_server()
srv_proc.wait(timeout=10)
down = False
deadline = time.time() + 15
while time.time() < deadline:        # poll for the port to close (mirror start)
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=1):
            pass
    except OSError:
        down = True
        break
    time.sleep(0.5)
assert down, "server still listening after stop"
print("gui server toggle ok: stopped")
frame.close()

print("\nALL WEBSERVER CHECKS PASSED")
