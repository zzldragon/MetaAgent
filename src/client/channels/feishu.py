"""Feishu / Lark channel (event subscription + im.message API).

Docs: https://open.feishu.cn/document/  (Events: im.message.receive_v1)
Set up: create a bot app, subscribe to "Receive messages", point the event
request URL at  https://<your-host><path>  (default /feishu).

config: {"app_id": "cli_...", "app_secret": "...",
         "verification_token": "...",      # plaintext mode: checks the event token
         "encrypt_key": "...",             # encrypted mode: AES-decrypt + signature
         "path": "/feishu"}
"""
from __future__ import annotations

import json

from client.channel import Inbound
from client.channels import _util
from client.channels.base_webhook import WebhookChannel


class FeishuChannel(WebhookChannel):
    name = "feishu"

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._tok = _util.TokenCache()

    # ── inbound ──────────────────────────────────────────────────────────────
    def verify(self, headers, raw_body, query):
        # Encrypted mode: X-Lark-Signature = sha256(ts+nonce+encrypt_key+body).
        # Plaintext mode: the verification token is checked in url_verification/
        # parse (Feishu carries it in the payload), so accept here.
        ek = self.config.get("encrypt_key")
        if not ek:
            return True
        h = _util.ci(headers)
        return _util.const_eq(
            h.get("x-lark-signature", ""),
            _util.feishu_signature(h.get("x-lark-request-timestamp", ""),
                                   h.get("x-lark-request-nonce", ""), ek, raw_body))

    def _payload(self, raw_body, query):
        data = json.loads(raw_body or b"{}")
        ek = self.config.get("encrypt_key")
        if ek and "encrypt" in data:          # encrypted envelope -> real event JSON
            data = json.loads(_util.feishu_decrypt(data["encrypt"], ek))
        return data

    def url_verification(self, payload):
        if payload.get("type") != "url_verification":
            return None
        vt = self.config.get("verification_token")
        # require the token when configured — a MISSING token must be rejected too
        # (omitting it must not bypass the check).
        if vt and not _util.const_eq(payload.get("token", ""), vt):
            return {"_status": 403, "error": "bad verification token"}
        return {"challenge": payload.get("challenge", "")}

    def parse(self, payload):
        header = payload.get("header") or {}
        if header.get("event_type") != "im.message.receive_v1":
            return None
        vt = self.config.get("verification_token")   # v2 events carry token in header
        if vt and not _util.const_eq(header.get("token", ""), vt):
            return None
        msg = (payload.get("event") or {}).get("message") or {}
        if msg.get("message_type") != "text":
            return None                       # TODO: images/files/post → vision parts
        try:
            text = json.loads(msg.get("content", "{}")).get("text", "").strip()
        except json.JSONDecodeError:
            text = ""
        if not text:
            return None
        sender = ((payload.get("event") or {}).get("sender") or {}).get("sender_id") or {}
        open_id = sender.get("open_id", "")
        # Per-USER isolation: key by chat AND sender (see dingtalk). chat_id for
        # the reply is recovered from raw in deliver(), not by splitting this key.
        chat_id = msg.get("chat_id", "")
        return Inbound(conversation_id=f"feishu:{chat_id}:{open_id}",
                       text=text, user_id=open_id, raw=payload)

    def _event_id(self, payload):
        return (payload.get("header") or {}).get("event_id")

    def insecure_reason(self):
        if not self.config.get("encrypt_key") and not self.config.get("verification_token"):
            return "no encrypt_key or verification_token — callbacks are unverified"
        return None

    # ── outbound ─────────────────────────────────────────────────────────────
    async def _tenant_token(self) -> str:
        async def fetch():
            r = await _util.http_post_json(
                "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                json={"app_id": self.config.get("app_id"),
                      "app_secret": self.config.get("app_secret")})
            return r.get("tenant_access_token", ""), r.get("expire", 7200)
        return await self._tok.get(fetch)

    async def deliver(self, rc, text, files=None):
        chat_id = (rc.raw.get("event", {}).get("message", {}).get("chat_id", "")
                   if isinstance(rc.raw, dict) else "")
        if not chat_id:
            return
        token = await self._tenant_token()
        resp = await _util.http_post_json(
            "https://open.feishu.cn/open-apis/im/v1/messages",
            params={"receive_id_type": "chat_id"},
            headers={"Authorization": "Bearer " + token,
                     "Content-Type": "application/json; charset=utf-8"},
            json={"receive_id": chat_id, "msg_type": "text",
                  "content": json.dumps({"text": _util.clip(text, 8000)},
                                        ensure_ascii=False)})
        _util.ok_result(resp, where="feishu")   # log (not raise) on API error
