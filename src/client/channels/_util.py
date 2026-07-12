"""Shared plumbing for the IM webhook channels: platform crypto (Feishu / WeCom
AES-CBC), request-signature helpers, an async access-token cache, XML helpers, and
thin aiohttp POST/GET wrappers.

The HTTP wrappers (`http_post_json` / `http_get_json`) are module-level so tests can
monkeypatch them — every channel's `deliver()` goes through them, so the send APIs
are exercisable offline without hitting the real platforms. The crypto/signature
functions are pure, so they're unit-testable with round-trips and known vectors.

`cryptography` is only needed for the encrypted paths (WeChat Work always; Feishu
when an Encrypt Key is set); it's imported lazily with a clear error.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import struct
import time
import xml.etree.ElementTree as ET

log = logging.getLogger("metaagent.channels")

try:                                   # asyncio is stdlib; import at top for TokenCache
    import asyncio
except Exception:                      # pragma: no cover
    asyncio = None


# ── AES-CBC (via `cryptography`, optional dependency) ────────────────────────
def _aes(key: bytes, iv: bytes):
    try:
        from cryptography.hazmat.primitives.ciphers import (
            Cipher, algorithms, modes)
    except ImportError as e:           # pragma: no cover - surfaced to the operator
        raise RuntimeError(
            "encrypted callbacks need the 'cryptography' package: "
            "pip install cryptography") from e
    return Cipher(algorithms.AES(key), modes.CBC(iv))


def _aes_decrypt(key: bytes, iv: bytes, ct: bytes) -> bytes:
    d = _aes(key, iv).decryptor()
    return d.update(ct) + d.finalize()


def _aes_encrypt(key: bytes, iv: bytes, pt: bytes) -> bytes:
    e = _aes(key, iv).encryptor()
    return e.update(pt) + e.finalize()


def _pkcs7_unpad(data: bytes) -> bytes:
    """Strip PKCS7 padding. WeChat/WeCom pad to a 32-byte boundary (pad value =
    count, 1..32); the last byte is always the pad length, so this is generic."""
    if not data:
        return data
    n = data[-1]
    return data[:-n] if 1 <= n <= 32 else data


def _pkcs7_pad(data: bytes, block: int = 32) -> bytes:
    n = block - (len(data) % block)
    n = n or block
    return data + bytes([n]) * n


def const_eq(a: str, b: str) -> bool:
    """Constant-time string compare for signatures."""
    return hmac.compare_digest((a or ""), (b or ""))


def ci(headers: dict) -> dict:
    """Lower-cased-key copy of a header dict for case-insensitive lookup."""
    return {str(k).lower(): v for k, v in (headers or {}).items()}


def clip(text: str, limit: int) -> str:
    """Truncate a reply to a platform's max message length in CHARACTERS."""
    text = text or ""
    return text if len(text) <= limit else (text[:limit - 1] + "…")


def clip_bytes(text: str, max_bytes: int) -> str:
    """Truncate to a UTF-8 BYTE budget without splitting a codepoint. Needed for
    WeCom, whose text limit is 2048 BYTES (a Chinese char is 3 bytes), not chars."""
    text = text or ""
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text
    ell = "…".encode("utf-8")
    return raw[:max(0, max_bytes - len(ell))].decode("utf-8", "ignore") + "…"


def ok_result(resp, code_keys=("errcode", "code"), where="") -> bool:
    """True if a send-API JSON body reports success. IM APIs return HTTP 200 with
    an errcode/code field, so a non-zero code is a *silent* failure — log it."""
    if not isinstance(resp, dict):
        return True                        # non-JSON/empty 200 → assume ok
    for k in code_keys:
        if k in resp and resp[k] not in (0, None):
            log.warning("%s send failed: %s=%s msg=%s", where or "channel",
                        k, resp[k], resp.get("errmsg") or resp.get("msg") or "")
            return False
    return True


# ── Feishu / Lark: event-encrypt AES + request signature ────────────────────
def feishu_decrypt(encrypt_b64: str, encrypt_key: str) -> str:
    """Decrypt Feishu's `encrypt` envelope. key = sha256(encrypt_key); the 16-byte
    IV is the first block of the base64-decoded ciphertext; the plaintext is the
    event JSON string (no random prefix)."""
    key = hashlib.sha256((encrypt_key or "").encode("utf-8")).digest()
    blob = base64.b64decode(encrypt_b64)
    iv, ct = blob[:16], blob[16:]
    return _pkcs7_unpad(_aes_decrypt(key, iv, ct)).decode("utf-8")


def feishu_signature(timestamp: str, nonce: str, encrypt_key: str, body: bytes) -> str:
    """X-Lark-Signature = sha256(timestamp + nonce + encrypt_key + raw_body)."""
    h = hashlib.sha256()
    h.update((timestamp or "").encode("utf-8"))
    h.update((nonce or "").encode("utf-8"))
    h.update((encrypt_key or "").encode("utf-8"))
    h.update(body or b"")
    return h.hexdigest()


# ── WeChat Work (WeCom): AES-256-CBC message crypto + msg_signature ─────────
def wecom_aes_key(encoding_aes_key: str) -> bytes:
    """The 32-byte AES key from the 43-char EncodingAESKey (base64 without pad)."""
    return base64.b64decode((encoding_aes_key or "") + "=")


def wecom_signature(token: str, timestamp: str, nonce: str, encrypt: str) -> str:
    """msg_signature = sha1(sorted([token, timestamp, nonce, encrypt]) joined)."""
    parts = sorted([token or "", timestamp or "", nonce or "", encrypt or ""])
    return hashlib.sha1("".join(parts).encode("utf-8")).hexdigest()


def wecom_decrypt(encrypt_b64: str, aes_key: bytes) -> tuple[str, str]:
    """Return (message, receive_id). Plaintext layout: 16 random bytes + 4-byte
    big-endian message length + message + receive_id (the CorpID)."""
    iv = aes_key[:16]
    plain = _pkcs7_unpad(_aes_decrypt(aes_key, iv, base64.b64decode(encrypt_b64)))
    msg_len = struct.unpack(">I", plain[16:20])[0]
    msg = plain[20:20 + msg_len].decode("utf-8")
    receive_id = plain[20 + msg_len:].decode("utf-8")
    return msg, receive_id


def wecom_encrypt(msg: str, aes_key: bytes, receive_id: str,
                  rand16: bytes | None = None) -> str:
    """Encrypt for a passive/API reply: base64(AES(16rand + len + msg + corpid))."""
    import os as _os
    rand16 = rand16 or _os.urandom(16)
    body = msg.encode("utf-8")
    plain = rand16 + struct.pack(">I", len(body)) + body + (receive_id or "").encode("utf-8")
    iv = aes_key[:16]
    return base64.b64encode(_aes_encrypt(aes_key, iv, _pkcs7_pad(plain))).decode("ascii")


# ── DingTalk: outgoing-robot callback signature ─────────────────────────────
def dingtalk_sign(app_secret: str, timestamp: str) -> str:
    """sign = base64(HMAC-SHA256(app_secret, timestamp + '\\n' + app_secret))."""
    string_to_sign = f"{timestamp}\n{app_secret}"
    digest = hmac.new((app_secret or "").encode("utf-8"),
                      string_to_sign.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


# ── XML (WeCom callback body + inner message) ───────────────────────────────
import re as _re

_ENCRYPT_RE = _re.compile(
    rb"<Encrypt>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</Encrypt>", _re.DOTALL)


def wecom_extract_encrypt(raw_body: bytes) -> str:
    """Pull the base64 <Encrypt> out of a WeCom POST body with a regex — WITHOUT
    running an XML parser on unauthenticated input (the signature is verified over
    this value; the inner, decrypted message XML is then safe to ElementTree-parse
    since only the AES-key holder could have produced it)."""
    m = _ENCRYPT_RE.search(raw_body or b"")
    return m.group(1).decode("utf-8", "ignore").strip() if m else ""


def xml_to_dict(xml_text: str) -> dict:
    """Flat {tag: text} from a single-level <xml>…</xml> (WeCom's shape)."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return {}
    return {child.tag: (child.text or "") for child in root}


# ── access-token cache (Feishu tenant / WeCom corp token, ~2h) ──────────────
class TokenCache:
    """Cache one access token with its TTL; refetch (once, under a lock) when it's
    within 60s of expiry. `fetch` is an async callable returning (token, ttl_secs)."""

    def __init__(self):
        self._token = None
        self._exp = 0.0
        self._lock = asyncio.Lock() if asyncio else None

    def invalidate(self):
        """Drop the cached token so the next get() refetches (call when the API
        reports the token expired/invalid before its nominal TTL)."""
        self._token, self._exp = None, 0.0

    async def get(self, fetch) -> str:
        if self._token and time.time() < self._exp - 60:
            return self._token
        if self._lock is None:                       # pragma: no cover
            self._token, ttl = await fetch()
            self._exp = time.time() + (ttl or 7200)
            return self._token
        async with self._lock:
            if self._token and time.time() < self._exp - 60:
                return self._token
            token, ttl = await fetch()
            self._token, self._exp = token, time.time() + (ttl or 7200)
            return self._token


# ── HTTP (aiohttp) — module-level so tests can monkeypatch them ─────────────
async def http_post_json(url, *, json=None, headers=None, params=None, data=None):
    import aiohttp
    async with aiohttp.ClientSession() as s:
        async with s.post(url, json=json, headers=headers, params=params,
                          data=data) as r:
            return await r.json(content_type=None)


async def http_get_json(url, *, params=None, headers=None):
    import aiohttp
    async with aiohttp.ClientSession() as s:
        async with s.get(url, params=params, headers=headers) as r:
            return await r.json(content_type=None)
