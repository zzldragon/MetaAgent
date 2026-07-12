# MetaAgent Client

Talk to a MetaAgent **generated agent** that has a **Web Server** node enabled
(its `server.py` listens on `ws://host:port`). Built so the same core powers a
desktop GUI today and WeChat / DingTalk / Feishu bots later.

## Architecture (one transport, many channels)

```
                         ┌─────────────────────────────┐
   PySide6 GUI  ───────▶ │                             │
   (qt_app.py)           │   AgentClient (protocol)    │ ──ws──▶ generated
   Feishu  ┐             │   - hello/auth handshake    │         agent
   DingTalk├─ webhook ─▶ │   - task / token / trace    │        server.py
   WeChat  ┘  (gateway)  │   - hitl_confirm / review   │
                         │   - result / error / cancel │
                         └─────────────────────────────┘
```

- **`agent_client.AgentClient`** — the only thing that knows the wire protocol.
  Persistent, multi-turn, streaming, HITL-aware. Reused by everything.
- **`dispatcher.Dispatcher` (+ `SessionManager`)** — the unified coordinator. One
  `AgentClient` per conversation, plus a **global run-gate** that serializes runs
  across ALL clients. This is not cosmetic: the agent enforces a process-global
  run lock and a 2nd concurrent run blocks *silently*, so only one coordinator can
  serialize visibly (and show a "queued" notice instead of a silent stall).
- **`channel.Channel` + `Inbound` + `ReplyContext`** — the adapter seam. Two
  capability flags (`streaming`, `interactive_hitl`) let the same pipeline drive
  interactive WS channels and unattended webhook channels without `isinstance`.
- **`ws_frontdoor.WsChannel`** — a WS server that **re-emits the agent protocol**,
  so the PySide6 client and the browser web UI connect to the dispatcher *unchanged*
  (just a different URL). HITL is id-correlated with a timeout; auth is gated.
- **`qt_app`** — the interactive desktop client (fully working).
- **`channels/` + `gateway.py`** — webhook bots for the IM platforms
  (scaffolded: orchestration is real; per-platform verify/parse/send are TODO).

## Unified dispatcher (single front door)

Put one dispatcher in front of the agent; point every client at it:

```bash
python -m client.ws_frontdoor --agent ws://127.0.0.1:8765 \
    [--token AGENT_TOKEN] [--auth-token DISPATCH_TOKEN] --host 0.0.0.0 --port 9000
# then the desktop client / web UI just use the dispatcher's URL:
python -m client --url ws://127.0.0.1:9000 --connect
```

Every WS client is one conversation; all runs serialize through the dispatcher's
global gate (a second client sees a `[queued…]` trace instead of a silent stall),
HITL round-trips through the proxy with a safety timeout, and a client disconnect
cancels its run so the agent is freed. The agent still accepts direct connections,
so the dispatcher is **recommended, not mandatory**.

**Scope note (honest):** the WS front-door unifies the WS-native clients (PySide6 +
web UI) through one coordinator and is verified end-to-end offline. The IM webhook
channels run via `gateway.py` (aiohttp); to get the *same* cross-client serialization
guarantee across IM **and** WS, they must share one process — folding the IM webhook
routes into the front-door (or running both on one server) is the next step. IM
`deliver()`/`verify()` and HITL approval-cards still need platform creds (deferred).

## Run the desktop client

```bash
pip install -r client/requirements.txt
# from the repo root (the folder containing client/):
python -m client --url ws://127.0.0.1:8765 --token "" --connect
```

**If you get `No module named client`:** your Python runs in "safe path" mode
(portable/embeddable installs — `python -c "import sys;print(sys.flags.safe_path)"`
prints `True`), so it doesn't add the current dir to the import path and even
`PYTHONPATH` is ignored. Use the launcher instead — it puts the repo on the path
itself:

```bash
python run_client.py --url ws://127.0.0.1:8765 --connect
```

(Find host/port/token in the generated app's `config.json → server`.) The GUI
streams tokens live, shows a **Trace** pane, answers **HITL** tool-confirm /
review prompts via dialogs, and keeps one connection for the whole chat.

## Use AgentClient directly

```python
import asyncio
from client import AgentClient

async def main():
    async with AgentClient("ws://127.0.0.1:8765", token="") as ac:
        res = await ac.run("encode 54545 with base64",
                           on_token=lambda d: print(d, end=""),
                           on_hitl=lambda kind, prompt, content: True)  # auto-approve
        print("\n=>", res.text)

asyncio.run(main())
```

## IM bots (WeChat Work / DingTalk / Feishu)

These are **unattended webhook** channels (the platform calls you). Run the
gateway and point each platform's callback URL at it:

```bash
pip install -r client/requirements.txt      # aiohttp + cryptography (for WeCom/Feishu-encrypted)
python -m client.gateway --url ws://AGENT_HOST:8765 --token SECRET \
    --feishu --dingtalk --wechat --config channels.json --port 9000
# Feishu event request URL -> http://<public-host>:9000/feishu   (dingtalk/wechat likewise)
```

`channels.json` holds each platform's credentials:

```json
{
  "feishu":   {"app_id": "cli_x", "app_secret": "…", "encrypt_key": "…", "verification_token": "…"},
  "dingtalk": {"app_secret": "…"},
  "wechat":   {"corp_id": "ww…", "token": "…", "encoding_aes_key": "<43 chars>", "agent_id": 1000002, "secret": "…"}
}
```

**Status: implemented.** `verify` / `parse` / `deliver` (and WeCom's AES/XML
crypto + URL verification) are done for all three, keyed **per end-user**, and
covered by an offline test (`tests/_verify_gateway_channels.py` — crypto
round-trips, signature verify/tamper, and `deliver` against the real send-API
shapes with HTTP mocked). What each does:

- `verify()` — Feishu `X-Lark-Signature` (encrypted mode) / token; DingTalk
  `timestamp`+`sign` HMAC (+ replay window); WeCom `msg_signature`. A channel with
  **no secret configured accepts callbacks (DEV ONLY)** — always set the secret
  before exposing a public URL.
- `parse()` — text messages → a per-user `Inbound`; WeCom decrypts the AES/XML
  callback first (in its own `handle_webhook`).
- `deliver()` — Feishu `im/v1/messages` (tenant token), DingTalk `sessionWebhook`,
  WeCom `message/send` (corp access token). Access tokens are cached (~2h).
- `ask_hitl()` — still **deny/reject** for unattended bots (an interactive
  approval card is a future enhancement).

## Add a new channel

Subclass `WebhookChannel` (or `Channel`), implement `verify`/`parse`/`deliver`,
register it in `gateway._load_channel`, and you inherit the agent-driving Hub
for free. The protocol layer never changes.
