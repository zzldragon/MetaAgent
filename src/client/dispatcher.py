"""Dispatcher — the unified coordinator that fronts ONE agent for ALL channels.

Every client type (MetaAgent Client, web UI, WeChat/Feishu/DingTalk) funnels its
inbound messages here; the Dispatcher runs the agent (via a per-conversation
AgentClient) and routes the reply/stream/HITL back through the originating
channel. One coordinator is REQUIRED, not cosmetic: the generated agent enforces
a process-global run lock (server.py `_RUN_LOCK`) and a 2nd concurrent run blocks
SILENTLY — so only a single front-door can serialize across clients visibly.

Concurrency model:
  * one AgentClient PER conversation_id (preserves multi-turn context),
  * a per-conversation lock (one run at a time within a conversation),
  * a single global `_run_gate` mirroring the agent's `_RUN_LOCK` — converts the
    agent's silent cross-client stall into an explicit FIFO queue with a notice.
"""
from __future__ import annotations

import asyncio

from client.agent_client import AgentClient, AgentError
from client.channel import Channel, Inbound, ReplyContext

_QUEUED_NOTICE = "[queued: another run is in progress…]"


class SessionManager:
    def __init__(self, url: str, token: str = ""):
        self.url = url
        self.token = token
        self._sessions: dict[str, AgentClient] = {}
        self._conv_locks: dict[str, asyncio.Lock] = {}
        self.run_gate = asyncio.Lock()        # GLOBAL: serialize across all conversations
        self._hello: dict | None = None

    def conv_lock(self, conv: str) -> asyncio.Lock:
        return self._conv_locks.setdefault(conv, asyncio.Lock())

    async def client_for(self, conv: str) -> AgentClient:
        ac = self._sessions.get(conv)
        if ac is None or not ac.connected:
            ac = AgentClient(self.url, self.token, proxy=None)   # proxy=None: never via a corp proxy
            await ac.connect()
            self._sessions[conv] = ac
        return ac

    async def drop(self, conv: str) -> None:
        ac = self._sessions.pop(conv, None)
        self._conv_locks.pop(conv, None)
        if ac is not None:
            try:
                await ac.close()
            except Exception:
                pass

    async def upstream_hello(self) -> dict:
        """Cache one real hello from the agent so the WS front-door can answer
        a downstream client's hello with the agent's true agent/vision/workspace
        before any conversation's AgentClient exists."""
        if self._hello is None:
            ac = AgentClient(self.url, self.token, proxy=None)
            await ac.connect()
            self._hello = dict(ac.hello)
            await ac.close()
        return self._hello

    async def close(self) -> None:
        for conv in list(self._sessions):
            await self.drop(conv)


class Dispatcher:
    def __init__(self, url: str, token: str = "", *, auth_token: str = ""):
        self.sessions = SessionManager(url, token)
        self.auth_token = auth_token          # the dispatcher's OWN downstream auth (separate from the agent token)
        self._inflight: dict[str, AgentClient] = {}    # conv -> ac currently running (for cancel)
        self._cancelled: set[str] = set()              # convs asked to cancel (covers a QUEUED run too)

    async def synth_hello(self, *, auth_required: bool) -> dict:
        up = await self.sessions.upstream_hello()
        return {"type": "hello", "agent": up.get("agent", "agent"),
                "vision": bool(up.get("vision")), "workspace": up.get("workspace", []),
                "auth_required": bool(auth_required)}

    async def dispatch(self, ch: Channel, inb: Inbound) -> None:
        conv = inb.conversation_id
        rc = inb.reply or ReplyContext(conv, raw=inb.raw)
        self._cancelled.discard(conv)
        # register at ENQUEUE time so cancel can catch a run still waiting on locks
        self._inflight.setdefault(conv, None)
        async with self.sessions.conv_lock(conv):
            if conv in self._cancelled:                # cancelled while queued behind the conv lock
                self._cancelled.discard(conv)
                self._inflight.pop(conv, None)
                return
            try:
                ac = await self.sessions.client_for(conv)
                self._inflight[conv] = ac
                if self.sessions.run_gate.locked() and ch.streaming:
                    await ch.deliver_trace(rc, _QUEUED_NOTICE)
                async with self.sessions.run_gate:     # GLOBAL gate: mirror the agent's single run lock
                    if conv in self._cancelled:        # cancelled while queued behind the global gate
                        self._cancelled.discard(conv)
                        return
                    res = await ac.run(
                        inb.text, images=inb.images, session_id=conv,
                        on_token=(lambda d: ch.deliver_token(rc, d)) if ch.streaming else None,
                        on_trace=(lambda l: ch.deliver_trace(rc, l)) if ch.streaming else None,
                        on_hitl=(lambda k, p, c: ch.ask_hitl(rc, k, p, c)))
                await ch.deliver(rc, res.text, res.files)
            except AgentError as e:
                await self.sessions.drop(conv)          # evict a broken session
                await ch.deliver_error(rc, str(e))
            finally:
                self._inflight.pop(conv, None)
                self._cancelled.discard(conv)

    async def cancel(self, conv: str) -> None:
        """Stop a conversation's run — whether it is in-flight OR still queued."""
        self._cancelled.add(conv)                       # catches a queued (not-yet-started) run
        ac = self._inflight.get(conv)
        if ac is not None:
            try:
                await ac.cancel()
            except Exception:
                pass

    async def workspace(self, conv: str, msg: dict, conn) -> None:
        """Proxy get/set_workspace. Serialized under the SAME per-conversation
        lock as run() so two _recv loops never share one AgentClient socket."""
        async with self.sessions.conv_lock(conv):
            ac = await self.sessions.client_for(conv)
            folders = await ac.workspace(
                folders=msg.get("folders") if msg.get("type") == "set_workspace" else None)
        await conn.send_frame({"type": "workspace", "folders": folders})

    async def on_disconnect(self, conv: str) -> None:
        """A downstream client dropped: cancel the in-flight run (so the agent
        releases its global lock / pending HITL) and discard the session. The WS
        front-door also resolves that connection's parked HITL futures so the
        upstream run unwinds."""
        await self.cancel(conv)
        await self.sessions.drop(conv)

    async def close(self) -> None:
        await self.sessions.close()


# Back-compat: the IM webhook gateway/base previously used `Hub`. Dispatcher is a
# superset (same dispatch(channel, inbound) entry point), so alias it.
Hub = Dispatcher
