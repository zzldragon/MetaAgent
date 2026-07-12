"""DingTalk channel (outgoing-robot callback + reply).

Docs: https://open.dingtalk.com/document/  (robot receive message)
The bot POSTs a JSON callback per @mention; the callback headers carry `timestamp`
+ `sign` (HMAC-SHA256 of the app secret). Reply quickly via the per-message
`sessionWebhook` (valid ~minutes) — no access token needed.

config: {"app_secret": "...", "path": "/dingtalk"}   (app_secret optional in dev)
"""
from __future__ import annotations

import time

from client.channel import Inbound
from client.channels import _util
from client.channels.base_webhook import WebhookChannel


class DingTalkChannel(WebhookChannel):
    name = "dingtalk"

    def verify(self, headers, raw_body, query):
        secret = self.config.get("app_secret")
        if not secret:
            return True                        # dev: no secret configured
        h = _util.ci(headers)
        ts, sign = h.get("timestamp", ""), h.get("sign", "")
        try:                                   # reject stale callbacks (replay), 1h window
            if abs(time.time() * 1000 - int(ts)) > 3600_000:
                return False
        except (TypeError, ValueError):
            return False
        return _util.const_eq(sign, _util.dingtalk_sign(secret, ts))

    def parse(self, payload):
        # callback: {"msgtype":"text","text":{"content":".."},"conversationId":..,
        #            "senderStaffId":..,"sessionWebhook":..}
        if payload.get("msgtype") != "text":
            return None
        text = (payload.get("text", {}).get("content") or "").strip()
        if not text:
            return None
        # Per-USER isolation: key by conversation AND sender, so group members
        # get separate agent memory. Delivery uses the raw sessionWebhook, so the
        # composite key doesn't affect it.
        conv_id = payload.get("conversationId", "")
        staff = payload.get("senderStaffId", "")
        return Inbound(conversation_id=f"dingtalk:{conv_id}:{staff}",
                       text=text, user_id=staff, raw=payload)

    def _event_id(self, payload):
        return payload.get("msgId")

    def insecure_reason(self):
        return None if self.config.get("app_secret") else \
            "no app_secret — callback signatures are not verified"

    async def deliver(self, rc, text, files=None):
        webhook = (rc.raw or {}).get("sessionWebhook")
        if not webhook:                        # window expired / not a webhook turn
            return
        resp = await _util.http_post_json(
            webhook, json={"msgtype": "text",
                           "text": {"content": _util.clip(text, 8000)}})
        _util.ok_result(resp, where="dingtalk")
