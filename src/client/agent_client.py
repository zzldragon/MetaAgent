"""AgentClient — the shared transport core.

Speaks the WebSocket protocol of a MetaAgent-generated `server.py` (web-server
node enabled). Transport-only and framework-agnostic: the PySide6 GUI, and the
WeChat/DingTalk/Feishu webhook channels, all drive the agent through THIS class.

Protocol (JSON text frames over ws://host:port), mirrored from the generated
server's docstring:
    -> {"type":"auth","token":...}                 (only if hello.auth_required)
    -> {"type":"task","task":...,"images":[...],"session_id":...}  run the agent
       (session_id optional: pin the conversation; else per-connection)
    -> {"type":"hitl_response","id":n,"result":...}  answer a confirm/review
    -> {"type":"cancel"} / {"type":"ping"}
    <- {"type":"hello","auth_required":bool,"vision":bool,"workspace":[...]}
    <- {"type":"token","text":...} / {"type":"trace","text":...}
    <- {"type":"hitl_confirm","id":n,"prompt":...}
    <- {"type":"hitl_review","id":n,"prompt":...,"content":...}
    <- {"type":"result","result":...,"files":[...]} / {"type":"error","error":...}
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field


class AgentError(Exception):
    """The agent reported an error frame, or the protocol handshake failed."""


@dataclass
class RunResult:
    text: str
    files: list = field(default_factory=list)


async def _maybe_await(value):
    """Allow callbacks to be sync OR async (so a GUI can emit a Qt signal and a
    webhook channel can `await` a platform API in the same callback slot)."""
    if asyncio.iscoroutine(value):
        return await value
    return value


class AgentClient:
    """One persistent, multi-turn connection to a generated agent's web server.

    Usage:
        async with AgentClient("ws://127.0.0.1:8765", token="") as ac:
            res = await ac.run("hello", on_token=print)
            print(res.text)
    """

    def __init__(self, url: str, token: str = "", *, open_timeout: float = 10.0,
                 max_size: int | None = None, proxy=None):
        self.url = url
        self.token = token
        self._open_timeout = open_timeout
        self._max_size = max_size          # None = no frame-size cap (big results/files)
        # proxy=None: connect DIRECTLY (the default). A corporate HTTP(S)_PROXY env
        # var otherwise makes `websockets` route even ws://127.0.0.1 through the
        # proxy — which can't reach a local agent, so the client fails while the
        # browser web UI (browsers bypass proxy for localhost) still works. Pass an
        # explicit proxy URL only if the agent really sits behind one.
        self._proxy = proxy
        self._ws = None
        self.hello: dict = {}

    # ── lifecycle ────────────────────────────────────────────────────────────
    @property
    def connected(self) -> bool:
        return self._ws is not None

    @property
    def vision(self) -> bool:
        return bool(self.hello.get("vision"))

    @property
    def agent_name(self) -> str:
        return self.hello.get("agent", "agent")

    async def connect(self) -> dict:
        import inspect
        import websockets                   # imported lazily so importing this module is cheap
        kwargs = {"open_timeout": self._open_timeout, "max_size": self._max_size}
        # disable proxy when supported (websockets >= 14); harmlessly omitted on
        # older versions, which don't proxy at all
        if "proxy" in inspect.signature(websockets.connect).parameters:
            kwargs["proxy"] = self._proxy
        self._ws = await websockets.connect(self.url, **kwargs)
        try:                                  # don't hang forever if no hello arrives
            hello = await asyncio.wait_for(self._recv(), self._open_timeout)
        except asyncio.TimeoutError:
            raise AgentError(
                f"connected, but no 'hello' within {self._open_timeout:.0f}s — is "
                "this a MetaAgent web-server agent on the right port?")
        if hello.get("type") != "hello":
            raise AgentError(f"expected 'hello', got {hello.get('type')!r}")
        self.hello = hello
        if hello.get("auth_required"):
            await self._send(type="auth", token=self.token)
            try:
                ack = await asyncio.wait_for(self._recv(), self._open_timeout)
            except asyncio.TimeoutError:
                raise AgentError("no auth response from server")
            if ack.get("type") != "auth_ok":
                raise AgentError("authentication failed: "
                                 + str(ack.get("error", "bad token")))
        return hello

    async def close(self) -> None:
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass

    async def __aenter__(self) -> "AgentClient":
        await self.connect()
        return self

    async def __aexit__(self, *_exc) -> None:
        await self.close()

    # ── messaging ────────────────────────────────────────────────────────────
    async def _send(self, **frame) -> None:
        if self._ws is None:
            raise AgentError("not connected")
        await self._ws.send(json.dumps(frame, ensure_ascii=False))

    async def _recv(self) -> dict:
        raw = await self._ws.recv()
        return json.loads(raw)

    async def cancel(self) -> None:
        """Ask the in-flight run to stop (safe to call mid-run)."""
        if self._ws is not None:
            await self._send(type="cancel")

    async def ping(self) -> None:
        await self._send(type="ping")

    async def workspace(self, *, folders=None) -> list:
        """Get (folders=None) or set the agent's workspace folders; returns the
        resulting list. Used by the dispatcher's WS front-door to proxy
        get/set_workspace. MUST NOT run concurrently with run() on the same
        socket (two _recv loops would steal each other's frames) — the caller
        serializes it under the per-conversation lock."""
        if folders is None:
            await self._send(type="get_workspace")
        else:
            await self._send(type="set_workspace", folders=list(folders))
        while True:
            msg = await self._recv()
            if msg.get("type") == "workspace":
                return msg.get("folders", [])

    async def run(self, task: str, *, images=None, session_id=None,
                  on_token=None, on_trace=None, on_hitl=None) -> RunResult:
        """Send `task` and drain frames until the terminal result/error.

        `session_id` pins the server-side conversation (per-user / per-chat
        memory isolation). Leave it None to let the server use a per-connection
        id — fine for a single persistent socket (one GUI, or the dispatcher's
        per-conversation socket), but a gateway multiplexing many users over
        reconnecting sockets should pass a stable id so history survives.

        Callbacks may be sync or async:
            on_token(delta:str)              live streamed tokens
            on_trace(line:str)               step traces
            on_hitl(kind, prompt, content) -> bool | dict   approve a tool / review
                kind is "hitl_confirm" (return bool) or "hitl_review"
                (return {"decision","content","feedback"}). Default: deny/reject.
        Returns RunResult(text, files); raises AgentError on an error frame.
        """
        _frame = dict(type="task", task=task, images=list(images or []))
        if session_id:
            _frame["session_id"] = session_id
        await self._send(**_frame)
        while True:
            msg = await self._recv()
            mtype = msg.get("type")
            if mtype == "token":
                if on_token:
                    await _maybe_await(on_token(msg.get("text", "")))
            elif mtype == "trace":
                if on_trace:
                    await _maybe_await(on_trace(msg.get("text", "")))
            elif mtype in ("hitl_confirm", "hitl_review"):
                decision = await self._decide_hitl(on_hitl, msg)
                await self._send(type="hitl_response", id=msg.get("id"),
                                 result=decision)
            elif mtype == "result":
                return RunResult(msg.get("result", ""), msg.get("files") or [])
            elif mtype == "error":
                raise AgentError(msg.get("error", "unknown error"))
            # pong / workspace / auth_ok and any unknown frame: ignore and keep reading

    async def _decide_hitl(self, on_hitl, msg):
        kind = msg.get("type")
        if on_hitl is None:                  # no handler -> safe default (deny/reject)
            return False if kind == "hitl_confirm" else {
                "decision": "reject", "content": msg.get("content", ""),
                "feedback": "no HITL handler"}
        decision = await _maybe_await(
            on_hitl(kind, msg.get("prompt", ""), msg.get("content", "")))
        if kind == "hitl_review" and not isinstance(decision, dict):
            decision = {"decision": "approve" if decision else "reject",
                        "content": msg.get("content", ""), "feedback": ""}
        return decision
