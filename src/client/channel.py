"""The multi-channel seam: a normalized message model + a Channel adapter ABC.

A *channel* carries messages between end-users and the agent. They differ in
shape but share ONE transport (AgentClient) and ONE coordinator (Dispatcher):

  - WS-native, INTERACTIVE (MetaAgent Client / web UI): a persistent socket,
    live token streaming, and live HITL round-trips. streaming=True,
    interactive_hitl=True. Implemented by client/ws_frontdoor.WsChannel.
  - HTTP-webhook, UNATTENDED (WeChat / DingTalk / Feishu): the platform POSTs a
    callback; reply via its send API. No token streaming; HITL auto-resolves.
    streaming=False, interactive_hitl=False. See client/channels/.

The Dispatcher (client/dispatcher.py) drives every channel through the same
pipeline; the only branch points are the two capability flags below.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class ReplyContext:
    """Per-turn outbound target threaded from inbound to the delivery methods.
    For WS channels `ws` is the live connection wrapper (has send_frame()/ask());
    for webhooks `ws` is None and `raw` carries the platform's reply token
    (e.g. DingTalk's short-lived sessionWebhook)."""
    conversation_id: str
    ws: object = None
    raw: dict = field(default_factory=dict)


@dataclass
class Inbound:
    """A normalized incoming message, platform-agnostic.

    `conversation_id` is channel-namespaced ("feishu:<chat>", "ws:<connid>") so
    keys never collide across channels. `kind` lets native WS clients send
    control verbs (cancel/ping/workspace) through the same model; IM adapters
    leave it "task"."""
    conversation_id: str
    text: str = ""
    user_id: str = ""
    images: list = field(default_factory=list)   # data: URLs, if the agent has vision
    raw: dict = field(default_factory=dict)        # original platform payload
    kind: str = "task"                              # task | cancel | ping | get_workspace | set_workspace
    folders: list | None = None                     # for set_workspace
    reply: ReplyContext | None = None               # per-turn sink (None for IM -> built from raw)


class Channel(ABC):
    """Base for a front-end. Subclasses translate a platform <-> Inbound/reply.
    Outbound methods take a ReplyContext so a live socket / short-lived webhook
    is addressable per turn."""
    name: str = "channel"
    streaming: bool = False           # WS sets True; IM leaves False (token/trace dropped)
    interactive_hitl: bool = False    # WS True; IM False -> ask_hitl auto deny/reject

    @abstractmethod
    async def start(self) -> None:
        """Begin listening (mount the webhook route / run the WS server)."""

    @abstractmethod
    async def deliver(self, rc: ReplyContext, text: str, files=None) -> None:
        """Send the agent's final answer to the user on this platform."""

    async def deliver_token(self, rc: ReplyContext, delta: str) -> None:
        """Live streamed token. Default no-op (IM can't stream)."""

    async def deliver_trace(self, rc: ReplyContext, line: str) -> None:
        """Step-trace line. Default no-op (IM doesn't show traces)."""

    async def deliver_error(self, rc: ReplyContext, error: str) -> None:
        """Run error. Default surfaces it as a normal message; WS re-emits a
        {type:error} frame instead."""
        await self.deliver(rc, f"[error] {error}")

    async def ask_hitl(self, rc: ReplyContext, kind: str, prompt: str, content: str):
        """Answer a HITL prompt. Unattended bots can't block on a human, so the
        safe default DENIES tool confirms and REJECTS reviews. WS overrides this
        with a live, id-correlated, timeout-bounded round-trip."""
        if kind == "hitl_confirm":
            return False
        return {"decision": "reject", "content": content,
                "feedback": "unattended channel: auto-rejected"}
