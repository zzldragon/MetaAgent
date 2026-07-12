"""Offline verification of the IM webhook channels (Feishu / DingTalk / WeChat Work):
platform crypto round-trips, signature verify (+ tamper rejection), parse → per-user
Inbound, WeCom URL-verification + encrypted-callback flow, and deliver() against the
real send-API shapes with HTTP mocked. No network, no platform creds.
"""
import asyncio
import base64
import hashlib
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.chdir(BASE)

from client.channels import _util
from client.channels.dingtalk import DingTalkChannel
from client.channels.feishu import FeishuChannel
from client.channels.wechat import WeChatWorkChannel


class FakeDisp:
    def __init__(self):
        self.inbs = []

    async def dispatch(self, ch, inb):
        self.inbs.append(inb)


# ── helpers that build valid signed/encrypted requests the platform way ──────
def feishu_encrypt(plain: str, key: str) -> str:
    k = hashlib.sha256(key.encode()).digest()
    iv = os.urandom(16)
    ct = _util._aes_encrypt(k, iv, _util._pkcs7_pad(plain.encode("utf-8"), 16))
    return base64.b64encode(iv + ct).decode("ascii")


def new_aes_key() -> tuple[str, bytes]:
    encoding = base64.b64encode(os.urandom(32)).decode("ascii")[:43]   # WeCom 43-char
    return encoding, _util.wecom_aes_key(encoding)


# ── 1. crypto round-trips ────────────────────────────────────────────────────
enc_key = "abcdefghij0123456789"
plain = json.dumps({"hello": "世界"})
assert _util.feishu_decrypt(feishu_encrypt(plain, enc_key), enc_key) == plain

encoding, aeskey = new_aes_key()
xml = "<xml><MsgType>text</MsgType><Content>hi 你好</Content></xml>"
blob = _util.wecom_encrypt(xml, aeskey, "corpX")
msg, recv = _util.wecom_decrypt(blob, aeskey)
assert msg == xml and recv == "corpX", (msg, recv)
# byte-aware clip (WeCom limit is 2048 BYTES; a Chinese char is 3 bytes)
clipped = _util.clip_bytes("你" * 2000, 2048)
assert len(clipped.encode("utf-8")) <= 2048 and clipped.endswith("…")
assert _util.clip_bytes("hi", 2048) == "hi"
print("1. Feishu + WeCom AES round-trips + byte-clip ok (incl. non-ASCII)")

# ── 2. signature helpers ─────────────────────────────────────────────────────
assert _util.wecom_signature("t", "100", "n", "E") == _util.wecom_signature("t", "100", "n", "E")
sig = _util.dingtalk_sign("sec", "1700000000000")
assert sig and _util.const_eq(sig, _util.dingtalk_sign("sec", "1700000000000"))
assert not _util.const_eq(sig, _util.dingtalk_sign("sec", "1700000000001"))
print("2. signature helpers deterministic + distinguishing ok")

# ── 3. DingTalk verify / parse / deliver ─────────────────────────────────────
import time as _t
dt = DingTalkChannel(FakeDisp(), {"app_secret": "sec"})
ts = str(int(_t.time() * 1000))
good = {"timestamp": ts, "sign": _util.dingtalk_sign("sec", ts)}
assert dt.verify(good, b"", {}) is True
assert dt.verify({"timestamp": ts, "sign": "WRONG"}, b"", {}) is False
assert dt.verify({"timestamp": "1", "sign": _util.dingtalk_sign("sec", "1")}, b"", {}) is False  # stale
assert DingTalkChannel(FakeDisp(), {}).verify({}, b"", {}) is True                                 # no secret = dev
inb = dt.parse({"msgtype": "text", "text": {"content": "hi"},
                "conversationId": "cidABC", "senderStaffId": "userA",
                "sessionWebhook": "https://oapi.dingtalk.com/robot/send?x=1"})
assert inb.conversation_id == "dingtalk:cidABC:userA" and inb.user_id == "userA"

_calls = []
async def _fake_post(url, *, json=None, headers=None, params=None, data=None):
    _calls.append({"url": url, "json": json, "headers": headers, "params": params})
    if "gettoken" in url:
        return {"access_token": "WXTOK", "expires_in": 7200}
    if "tenant_access_token" in url:
        return {"tenant_access_token": "FSTOK", "expire": 7200}
    return {"errcode": 0}
async def _fake_get(url, *, params=None, headers=None):
    _calls.append({"url": url, "params": params})
    if "gettoken" in url:
        return {"access_token": "WXTOK", "expires_in": 7200}
    return {}
_util.http_post_json, _util.http_get_json = _fake_post, _fake_get


async def main():
    # DingTalk deliver → the sessionWebhook, text content
    _calls.clear()
    await dt.deliver(inb.reply or _mk_rc(inb), "the answer")
    assert _calls and "oapi.dingtalk.com/robot/send" in _calls[0]["url"]
    assert _calls[0]["json"]["text"]["content"] == "the answer"
    print("3. DingTalk verify/parse/deliver(sessionWebhook) ok")

    # ── 4. Feishu encrypted verify + decrypt + url_verification + parse + deliver
    fs = FeishuChannel(FakeDisp(), {"app_id": "cli_x", "app_secret": "s",
                                    "encrypt_key": enc_key})
    body = json.dumps({"encrypt": feishu_encrypt(
        json.dumps({"type": "url_verification", "challenge": "CHAL"}), enc_key)}).encode()
    hdr = {"X-Lark-Request-Timestamp": "100", "X-Lark-Request-Nonce": "nn",
           "X-Lark-Signature": _util.feishu_signature("100", "nn", enc_key, body)}
    assert fs.verify(hdr, body, {}) is True
    assert fs.verify({**hdr, "X-Lark-Signature": "bad"}, body, {}) is False
    assert fs.url_verification(fs._payload(body, {})) == {"challenge": "CHAL"}
    ev = {"header": {"event_type": "im.message.receive_v1"},
          "event": {"message": {"message_type": "text", "chat_id": "chatZ",
                                "content": json.dumps({"text": "hello lark"})},
                    "sender": {"sender_id": {"open_id": "ou_1"}}}}
    fi = fs.parse(ev)
    assert fi.conversation_id == "feishu:chatZ:ou_1" and fi.user_id == "ou_1"
    _calls.clear()
    fi.reply = _mk_rc(fi, raw=ev)
    await fs.deliver(fi.reply, "reply text")
    assert any("tenant_access_token" in c["url"] for c in _calls)      # token fetched
    send = [c for c in _calls if c["url"].endswith("/im/v1/messages")][0]
    assert send["headers"]["Authorization"] == "Bearer FSTOK"
    assert send["params"] == {"receive_id_type": "chat_id"}
    assert json.loads(send["json"]["content"])["text"] == "reply text"
    assert send["json"]["receive_id"] == "chatZ"
    print("4. Feishu verify(sig)+decrypt+challenge+parse+deliver(im.messages) ok")

    # ── 5. WeChat Work: URL verify (GET) + encrypted callback (POST) + deliver ──
    wx = WeChatWorkChannel(FakeDisp(), {"corp_id": "corpX", "token": "TK",
                                        "encoding_aes_key": encoding,
                                        "agent_id": 1000002, "secret": "sec"})
    # GET url-verification: encrypt an echostr, sign it, expect the plaintext back
    echo_plain = "1616140317555161061"
    echo_enc = _util.wecom_encrypt(echo_plain, aeskey, "corpX")
    q = {"timestamp": "100", "nonce": "nn",
         "msg_signature": _util.wecom_signature("TK", "100", "nn", echo_enc),
         "echostr": echo_enc}
    assert await wx.handle_get(q) == echo_plain
    assert await wx.handle_get({**q, "msg_signature": "bad"}) == ""     # reject
    # POST encrypted message callback → dispatcher gets the parsed Inbound
    inner = ("<xml><MsgType><![CDATA[text]]></MsgType>"
             "<Content><![CDATA[ping]]></Content>"
             "<FromUserName><![CDATA[zhang]]></FromUserName>"
             "<MsgId>12345</MsgId></xml>")     # real WeChat: CDATA + MsgId
    enc = _util.wecom_encrypt(inner, aeskey, "corpX")
    post_body = ("<xml><ToUserName><![CDATA[corpX]]></ToUserName>"
                 "<Encrypt><![CDATA[" + enc + "]]></Encrypt></xml>").encode()
    pq = {"timestamp": "200", "nonce": "mm",
          "msg_signature": _util.wecom_signature("TK", "200", "mm", enc)}
    res = await wx.handle_webhook({}, post_body, pq)
    await asyncio.sleep(0.05)                   # dispatch runs in the background
    assert res.get("_raw") == "" and len(wx.dispatcher.inbs) == 1, res
    got = wx.dispatcher.inbs[0]
    assert got.conversation_id == "wechat:zhang" and got.text == "ping"
    # a platform RETRY of the same MsgId is deduped (no second agent run)
    await wx.handle_webhook({}, post_body, pq)
    await asyncio.sleep(0.05)
    assert len(wx.dispatcher.inbs) == 1, "duplicate MsgId was not deduped"
    # bad signature → 403, no dispatch
    wx.dispatcher.inbs.clear()
    assert (await wx.handle_webhook({}, post_body, {**pq, "msg_signature": "x"})).get("_status") == 403
    await asyncio.sleep(0.05)
    assert not wx.dispatcher.inbs
    # deliver → message/send with a cached access token (byte-clipped content)
    _calls.clear()
    await wx.deliver(got.reply, "wx answer")
    assert any("gettoken" in c["url"] for c in _calls)
    send = [c for c in _calls if c["url"].endswith("/message/send")][0]
    assert send["params"]["access_token"] == "WXTOK"
    assert send["json"] == {"touser": "zhang", "msgtype": "text",
                            "agentid": 1000002, "text": {"content": "wx answer"}}
    print("5. WeChat GET-verify + encrypted POST + dedup + deliver(message/send) ok")

    # ── 6. WeChat token-refresh on 42001 (invalidate + retry once) ───────────
    seq = iter([{"errcode": 42001}, {"errcode": 0}])
    gt = [0]
    async def _rp(url, *, json=None, headers=None, params=None, data=None):
        return next(seq) if url.endswith("/message/send") else {"errcode": 0}
    async def _rg(url, *, params=None, headers=None):
        gt[0] += 1
        return {"access_token": f"T{gt[0]}", "expires_in": 7200}
    _util.http_post_json, _util.http_get_json = _rp, _rg
    wx2 = WeChatWorkChannel(FakeDisp(), {"corp_id": "corpX", "token": "TK",
                                         "encoding_aes_key": encoding, "agent_id": 1,
                                         "secret": "s"})
    await wx2.deliver(_mk_rc_id("wechat:zhang"), "hi")
    assert gt[0] == 2, ("access_token not refreshed after 42001", gt[0])
    _util.http_post_json, _util.http_get_json = _fake_post, _fake_get   # restore
    print("6. WeChat access-token refresh on 42001 ok")

    # ── 7. Feishu plaintext mode: verification_token is REQUIRED (no bypass) ──
    fp = FeishuChannel(FakeDisp(), {"app_id": "x", "app_secret": "s",
                                    "verification_token": "VT"})
    base_ev = {"event": {"message": {"message_type": "text", "chat_id": "c",
                                     "content": json.dumps({"text": "hi"})},
                         "sender": {"sender_id": {"open_id": "o"}}}}
    assert fp.parse({**base_ev, "header": {"event_type": "im.message.receive_v1"}}) is None
    assert fp.parse({**base_ev, "header": {"event_type": "im.message.receive_v1", "token": "WRONG"}}) is None
    assert fp.parse({**base_ev, "header": {"event_type": "im.message.receive_v1", "token": "VT"}}) is not None
    assert fp.url_verification({"type": "url_verification", "challenge": "c"}).get("_status") == 403
    assert fp.url_verification({"type": "url_verification", "challenge": "c", "token": "VT"}) == {"challenge": "c"}
    # null sub-object doesn't crash parse (returns None cleanly)
    assert fp.parse({"header": {"event_type": "im.message.receive_v1", "token": "VT"},
                     "event": {"message": None, "sender": None}}) is None
    print("7. Feishu verification_token required (missing/wrong rejected) + null-safe ok")


def _mk_rc(inb, raw=None):
    from client.channel import ReplyContext
    return ReplyContext(inb.conversation_id, ws=None, raw=raw or inb.raw)


def _mk_rc_id(conv):
    from client.channel import ReplyContext
    return ReplyContext(conv, ws=None, raw={})


asyncio.run(main())
print("\nALL GATEWAY-CHANNEL CHECKS PASSED")
