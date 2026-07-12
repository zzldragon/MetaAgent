"""WS front-door: the dispatcher's north-side WebSocket server.

Re-emits the SAME protocol the generated agent server speaks, so the existing
MetaAgent Client (PySide6) and the browser web UI connect UNCHANGED — only the
URL points at the dispatcher (e.g. ws://dispatcher:9000) instead of the agent.
Internally every connection is one conversation ("ws:<connid>") driven through
the shared Dispatcher (and thus the global run-gate that serializes all clients).

Run:
    python -m client.ws_frontdoor --agent ws://AGENT_HOST:8765 [--token AGENT] \
        [--auth-token DISPATCH] --host 0.0.0.0 --port 9000
Then point the GUI at ws://<dispatcher-host>:9000  (or `python -m client --url …`).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import uuid

from client.channel import Channel, Inbound, ReplyContext
from client.dispatcher import Dispatcher


class WsConnection:
    """Wraps one downstream socket: frame sender + HITL id-correlation. The agent
    issues its own HITL ids, but those must NOT leak downstream (they could
    collide across a connection's turns), so we allocate our OWN ids here and map
    each to a Future that the client's hitl_response resolves."""
    def __init__(self, ws, conv_id: str, hitl_timeout: float = 120.0):
        self.ws = ws
        self.conv_id = conv_id
        self.hitl_timeout = hitl_timeout
        self._hitl: dict[int, asyncio.Future] = {}
        self._seq = 0

    async def send_frame(self, frame: dict) -> None:
        await self.ws.send(json.dumps(frame, ensure_ascii=False))

    async def ask(self, kind: str, prompt: str, content: str):
        """Send a hitl_confirm/review downstream and await the client's answer.
        Times out to a safe deny/reject — CRITICAL because the run holds the
        global gate while waiting, so an unanswered prompt must not wedge every
        other client forever."""
        self._seq += 1
        rid = self._seq
        fut = asyncio.get_event_loop().create_future()
        self._hitl[rid] = fut
        await self.send_frame({"type": kind, "id": rid, "prompt": prompt,
                               "content": content})
        try:
            return await asyncio.wait_for(fut, self.hitl_timeout)
        except asyncio.TimeoutError:
            return (False if kind == "hitl_confirm"
                    else {"decision": "reject", "content": content,
                          "feedback": "timeout"})
        finally:
            self._hitl.pop(rid, None)

    def resolve(self, rid, result) -> None:
        fut = self._hitl.get(rid)
        if fut is not None and not fut.done():
            fut.set_result(result)

    def resolve_all(self) -> None:
        """On disconnect: deny every parked prompt so the upstream run unwinds
        (ask_hitl returns -> AgentClient.run sends the response -> the agent
        releases its run lock)."""
        for fut in list(self._hitl.values()):
            if not fut.done():
                fut.set_result(False)
        self._hitl.clear()


class WsChannel(Channel):
    name = "ws"
    streaming = True
    interactive_hitl = True

    async def start(self) -> None:
        return None

    async def deliver(self, rc: ReplyContext, text: str, files=None) -> None:
        await rc.ws.send_frame({"type": "result", "result": text, "files": files or []})

    async def deliver_token(self, rc: ReplyContext, delta: str) -> None:
        await rc.ws.send_frame({"type": "token", "text": delta})

    async def deliver_trace(self, rc: ReplyContext, line: str) -> None:
        await rc.ws.send_frame({"type": "trace", "text": line})

    async def deliver_error(self, rc: ReplyContext, error: str) -> None:
        await rc.ws.send_frame({"type": "error", "error": error})

    async def ask_hitl(self, rc: ReplyContext, kind, prompt, content):
        return await rc.ws.ask(kind, prompt, content)


def make_handler(dispatcher: Dispatcher, channel: WsChannel, hitl_timeout: float):
    async def handler(ws):
        conv = "ws:" + uuid.uuid4().hex            # ephemeral per connection (no cross-reconnect history)
        conn = WsConnection(ws, conv, hitl_timeout)
        authed = not dispatcher.auth_token
        await conn.send_frame(await dispatcher.synth_hello(
            auth_required=bool(dispatcher.auth_token)))
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except (ValueError, TypeError):
                    await conn.send_frame({"type": "error", "error": "invalid JSON"})
                    continue
                mtype = msg.get("type")
                if not authed:                      # mirror server.py: nothing before auth
                    if mtype == "auth" and msg.get("token") == dispatcher.auth_token:
                        authed = True
                        await conn.send_frame({"type": "auth_ok"})
                        continue
                    await conn.send_frame({"type": "error", "error": "auth required"})
                    break
                if mtype == "task":
                    rc = ReplyContext(conv, ws=conn, raw=msg)
                    inb = Inbound(conv, (msg.get("task") or ""),
                                  images=msg.get("images") or [], reply=rc, kind="task")
                    asyncio.create_task(dispatcher.dispatch(channel, inb))  # don't block the read loop
                elif mtype == "hitl_response":
                    conn.resolve(msg.get("id"), msg.get("result"))
                elif mtype == "cancel":
                    await dispatcher.cancel(conv)
                elif mtype == "ping":
                    await conn.send_frame({"type": "pong"})
                elif mtype in ("get_workspace", "set_workspace"):
                    await dispatcher.workspace(conv, msg, conn)
                else:
                    await conn.send_frame({"type": "error",
                                           "error": f"unknown type: {mtype}"})
        finally:
            conn.resolve_all()                      # unwedge any parked HITL
            await dispatcher.on_disconnect(conv)    # cancel in-flight + free the agent
    return handler


async def serve(agent_url, token="", auth_token="", host="0.0.0.0", port=9000,
                hitl_timeout=120.0):
    import websockets
    dispatcher = Dispatcher(agent_url, token, auth_token=auth_token)
    channel = WsChannel()
    handler = make_handler(dispatcher, channel, hitl_timeout)
    async with websockets.serve(handler, host, port, max_size=None):
        print(f"dispatcher WS front-door on ws://{host}:{port}  ->  agent {agent_url}"
              + ("  (auth required)" if auth_token else ""))
        try:
            await asyncio.Event().wait()
        finally:
            await dispatcher.close()


def main(argv=None):
    p = argparse.ArgumentParser(description="Unified WS front-door for a MetaAgent agent.")
    p.add_argument("--agent", required=True, help="upstream agent WS URL, e.g. ws://127.0.0.1:8765")
    p.add_argument("--token", default="", help="upstream agent auth token, if set")
    p.add_argument("--auth-token", default="", help="token THIS dispatcher requires of clients")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=9000)
    p.add_argument("--hitl-timeout", type=float, default=120.0,
                   help="auto-deny a HITL prompt after N seconds (frees the shared run gate)")
    args = p.parse_args(argv)
    asyncio.run(serve(args.agent, args.token, args.auth_token, args.host, args.port,
                      args.hitl_timeout))


if __name__ == "__main__":
    main()
