"""Shared base for IM webhook channels.

The HTTP transport lives in client/gateway.py (aiohttp). A platform adapter only
needs to fill three platform-specific seams:

    verify(headers, raw_body) -> bool      reject forged callbacks
    parse(payload) -> Inbound | None       map the callback to a user message
    deliver(conversation_id, text, files)  call the platform's send-message API

`handle_webhook` orchestrates verify -> parse -> Hub.dispatch, so every channel
shares the same agent-driving logic.
"""
from __future__ import annotations

import asyncio
import json
from collections import OrderedDict

from client.channel import Channel, Inbound, ReplyContext
from client.channels import _util
from client.dispatcher import Dispatcher

_SEEN_MAX = 2048        # bounded per-event-id dedup window


class WebhookChannel(Channel):
    def __init__(self, dispatcher: Dispatcher, config: dict | None = None):
        self.dispatcher = dispatcher
        self.config = config or {}
        # platform path the gateway mounts this channel on, e.g. /feishu
        self.path = self.config.get("path", "/" + self.name)
        self._seen: "OrderedDict[str, bool]" = OrderedDict()   # event-id dedup
        self._tasks: set = set()                               # keep bg tasks alive

    async def start(self) -> None:
        # nothing to do here; client/gateway.py mounts self.path -> handle_webhook
        return None

    # ── platform-specific seams (override in subclasses) ──────────────────────
    def verify(self, headers: dict, raw_body: bytes, query: dict) -> bool:
        """Validate the callback's signature/token. Default: accept (DEV ONLY —
        override before exposing a public URL)."""
        return True

    def _payload(self, raw_body: bytes, query: dict) -> dict:
        """Decode the callback body to a dict. Default: JSON. Encrypted channels
        (Feishu with an Encrypt Key) override this to decrypt first. (WeCom uses
        XML and overrides handle_webhook wholesale.)"""
        return json.loads(raw_body or b"{}")

    def parse(self, payload: dict) -> Inbound | None:
        """Map a platform callback to an Inbound, or None to ignore (non-message
        events, the bot's own echoes, etc.). MUST be overridden."""
        raise NotImplementedError

    def _event_id(self, payload: dict) -> str | None:
        """A stable per-message id for dedup (Feishu event_id, WeCom MsgId, …).
        None disables dedup for that message."""
        return None

    def insecure_reason(self) -> str | None:
        """A human note if this channel would accept UNVERIFIED callbacks with the
        current config (no secret set), so the gateway can warn loudly at startup."""
        return None

    # ── orchestration (shared) ────────────────────────────────────────────────
    def _is_dup(self, event_id) -> bool:
        """True if this event id was already handled (drop a platform retry). The
        id is recorded (at-most-once): platform retries of an already-accepted
        message must not re-run the agent or re-reply."""
        if not event_id:
            return False
        if event_id in self._seen:
            return True
        self._seen[event_id] = True
        while len(self._seen) > _SEEN_MAX:
            self._seen.popitem(last=False)
        return False

    def _spawn(self, coro) -> None:
        """Run a coroutine in the background, keeping a reference so it isn't
        garbage-collected mid-flight (asyncio only holds a weak ref)."""
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    def _run_inbound(self, payload: dict, inb: Inbound, raw=None) -> None:
        """Dedup, then dispatch the agent run in the BACKGROUND — the HTTP callback
        must ack fast (Feishu ~3s / WeCom 5s / DingTalk) or the platform retries
        and we'd double-run + double-reply. The reply goes out via deliver()."""
        if self._is_dup(self._event_id(payload)):
            return
        inb.reply = ReplyContext(inb.conversation_id, ws=None,
                                 raw=raw if raw is not None else payload)
        self._spawn(self.dispatcher.dispatch(self, inb))

    async def handle_webhook(self, headers: dict, raw_body: bytes, query: dict | None = None):
        """Entry point the gateway calls per HTTP POST callback. Returns a dict the
        gateway serializes: `{"_status": N, ...}` → JSON, or `{"_raw": text}` →
        plain text (platforms that want an echo/challenge or a bare 'success')."""
        query = query or {}
        if not self.verify(headers, raw_body, query):
            return {"_status": 403, "error": "signature verification failed"}
        try:
            payload = self._payload(raw_body, query)
        except Exception:                                 # bad JSON / decrypt failure
            return {"_status": 400, "error": "invalid body"}
        if not isinstance(payload, dict):                 # non-object JSON body
            return {"_status": 400, "error": "expected a JSON object"}

        challenge = self.url_verification(payload)        # platform handshake
        if challenge is not None:
            return challenge

        inb = self.parse(payload)
        if inb is None:
            return {"_status": 200}                       # ack non-message events
        self._run_inbound(payload, inb)                   # background; ack now
        return {"_status": 200}

    async def handle_get(self, query: dict) -> str:
        """Platform URL-verification GET. Default echoes `echostr` (or 'ok').
        WeCom overrides to decrypt+verify the challenge before echoing."""
        return query.get("echostr", "ok")

    def url_verification(self, payload: dict):
        """Some platforms (e.g. Feishu) send a one-time URL-verification challenge
        (in the POST body) that must be echoed back. Return the response dict, or
        None if N/A."""
        return None
