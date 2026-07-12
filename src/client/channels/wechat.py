"""WeChat Work (WeCom) channel — enterprise self-built app callback.

Docs: https://developer.work.weixin.qq.com/document/  (receive messages)
WeCom callbacks are AES-encrypted XML (NOT JSON), so this channel overrides the
base POST/GET flow. URL verification is a GET with msg_signature/timestamp/nonce/
echostr (decrypt echostr, echo the plaintext). Message callbacks POST
<xml><Encrypt>..</Encrypt>..</xml>; verify msg_signature, AES-decrypt to the inner
message XML, then reply asynchronously via the message/send API (so we ack the
callback immediately and aren't bound by the 5s passive-reply window).

config: {"corp_id": "ww..", "token": "..", "encoding_aes_key": "<43 chars>",
         "agent_id": 1000002, "secret": "..", "path": "/wechat"}
Needs the `cryptography` package (AES-256-CBC).
"""
from __future__ import annotations

from client.channel import Inbound
from client.channels import _util
from client.channels.base_webhook import WebhookChannel


class WeChatWorkChannel(WebhookChannel):
    name = "wechat"

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._tok = _util.TokenCache()
        aes = self.config.get("encoding_aes_key")
        self._aes = _util.wecom_aes_key(aes) if aes else None

    # ── signature over the encrypted payload (echostr on GET, <Encrypt> on POST) ─
    def _sig_ok(self, query: dict, encrypt: str) -> bool:
        expected = _util.wecom_signature(
            self.config.get("token", ""), query.get("timestamp", ""),
            query.get("nonce", ""), encrypt)
        return _util.const_eq(query.get("msg_signature", ""), expected)

    def _recv_ok(self, receive_id: str) -> bool:
        cid = self.config.get("corp_id")
        return (not cid) or receive_id == cid

    # ── URL verification (GET): decrypt echostr and echo the plaintext ──────────
    async def handle_get(self, query):
        echostr = query.get("echostr", "")
        if not (self._aes and echostr and self._sig_ok(query, echostr)):
            return ""                          # reject: no echo
        try:
            plain, recv = _util.wecom_decrypt(echostr, self._aes)
        except Exception:
            return ""
        return plain if self._recv_ok(recv) else ""

    # ── message callback (POST): verify -> decrypt -> parse -> dispatch ─────────
    async def handle_webhook(self, headers, raw_body, query=None):
        query = query or {}
        if self._aes is None:
            return {"_status": 500, "error": "wechat: encoding_aes_key not set"}
        # regex-extract <Encrypt> (don't XML-parse unauthenticated input); the
        # inner message XML below is parsed only AFTER decryption authenticates it.
        encrypt = _util.wecom_extract_encrypt(raw_body or b"")
        if not (encrypt and self._sig_ok(query, encrypt)):
            return {"_raw": "", "_status": 403}
        try:
            inner_xml, recv = _util.wecom_decrypt(encrypt, self._aes)
        except Exception:
            return {"_raw": "", "_status": 400}
        if not self._recv_ok(recv):
            return {"_raw": "", "_status": 403}
        msg = _util.xml_to_dict(inner_xml)     # inner XML is authenticated (decrypted)
        inb = self.parse(msg)
        if inb is None:
            return {"_raw": ""}                # ack non-text events (empty 200)
        # dedup (MsgId) + reply asynchronously via message/send; ack the callback now
        self._run_inbound(msg, inb, raw=msg)
        return {"_raw": ""}

    def parse(self, payload):
        # `payload` is the DECRYPTED inner message XML as a flat dict.
        if payload.get("MsgType") != "text":
            return None
        text = (payload.get("Content") or "").strip()
        if not text:
            return None
        user = payload.get("FromUserName", "")   # per-app userid (1:1 with the app)
        return Inbound(conversation_id="wechat:" + user, text=text,
                       user_id=user, raw=payload)

    def _event_id(self, payload):
        return payload.get("MsgId")

    def insecure_reason(self):
        return None if self._aes else \
            "no encoding_aes_key — callbacks cannot be verified/decrypted"

    # ── outbound: message/send with a cached access token (refresh on invalid) ──
    async def _access_token(self) -> str:
        async def fetch():
            r = await _util.http_get_json(
                "https://qyapi.weixin.qq.com/cgi-bin/gettoken",
                params={"corpid": self.config.get("corp_id"),
                        "corpsecret": self.config.get("secret")})
            tok = r.get("access_token", "")
            if not tok:                         # gettoken failed — don't cache ""
                _util.log.warning("wechat gettoken failed: errcode=%s errmsg=%s",
                                  r.get("errcode"), r.get("errmsg"))
                raise RuntimeError("wechat gettoken failed")
            return tok, r.get("expires_in", 7200)
        return await self._tok.get(fetch)

    async def _send(self, to_user, text):
        token = await self._access_token()
        return await _util.http_post_json(
            "https://qyapi.weixin.qq.com/cgi-bin/message/send",
            params={"access_token": token},
            json={"touser": to_user, "msgtype": "text",
                  "agentid": self.config.get("agent_id"),
                  "text": {"content": _util.clip_bytes(text, 2048)}})  # BYTES, not chars

    async def deliver(self, rc, text, files=None):
        to_user = rc.conversation_id.split(":", 1)[-1]   # strip "wechat:"
        try:
            resp = await self._send(to_user, text)
        except RuntimeError:                    # gettoken failed (logged already)
            return
        # WeCom returns 200 with an errcode; on an early-invalidated token
        # (42001 expired / 40014 invalid) bust the cache and retry once.
        if isinstance(resp, dict) and resp.get("errcode") in (40014, 42001):
            self._tok.invalidate()
            try:
                resp = await self._send(to_user, text)
            except RuntimeError:
                return
        _util.ok_result(resp, where="wechat")
