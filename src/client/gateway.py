"""HTTP gateway that hosts IM webhook channels against one agent.

All enabled channels share a single Dispatcher (one AgentClient session per
conversation), so each chat keeps its own multi-turn context. Needs aiohttp.

Run (after filling a channel's verify/parse/deliver seams + config):
    python -m client.gateway --url ws://AGENT_HOST:8765 --token SECRET \
        --feishu --dingtalk --port 9000
Then point each platform's callback URL at  http://<public-host>:9000/<name>.
"""
from __future__ import annotations

import argparse
import asyncio

from client.dispatcher import Dispatcher

CHANNELS = {}     # name -> Channel class (populated lazily to avoid hard imports)


def _load_channel(name):
    if name == "feishu":
        from client.channels.feishu import FeishuChannel
        return FeishuChannel
    if name == "dingtalk":
        from client.channels.dingtalk import DingTalkChannel
        return DingTalkChannel
    if name == "wechat":
        from client.channels.wechat import WeChatWorkChannel
        return WeChatWorkChannel
    raise ValueError(f"unknown channel: {name}")


def build_app(dispatcher: Dispatcher, channels: list):
    from aiohttp import web

    app = web.Application()

    def make_post(ch):
        async def handler(request):
            raw = await request.read()
            res = await ch.handle_webhook(
                dict(request.headers), raw, dict(request.query)) or {}
            status = res.pop("_status", 200)
            if "_raw" in res:                    # plain-text reply (WeCom / echoes)
                return web.Response(text=res["_raw"], status=status)
            return web.json_response(res, status=status)
        return handler

    def make_get(ch):
        async def handler(request):
            # URL verification: the channel decides what to echo (WeCom decrypts
            # + verifies the challenge; others just return 'ok'/echostr).
            text = await ch.handle_get(dict(request.query))
            return web.Response(text=text)
        return handler

    for ch in channels:
        app.router.add_post(ch.path, make_post(ch))
        app.router.add_get(ch.path, make_get(ch))
    return app


async def serve(url, token, names, port, host="0.0.0.0", config=None):
    from aiohttp import web

    dispatcher = Dispatcher(url, token)
    channels = [_load_channel(n)(dispatcher, (config or {}).get(n, {})) for n in names]
    for ch in channels:
        await ch.start()
        reason = ch.insecure_reason() if hasattr(ch, "insecure_reason") else None
        if reason:                              # fail-open: loud warning, don't silently ship an open relay
            print(f"  !! WARNING [{ch.name}]: {reason} — callbacks are UNVERIFIED "
                  f"(DEV ONLY). Set credentials in --config before exposing a public URL.")
    app = build_app(dispatcher, channels)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    print(f"gateway listening on http://{host}:{port}  channels: "
          + ", ".join(f"{c.name}{c.path}" for c in channels))
    await asyncio.Event().wait()        # run until cancelled


def _load_config(path):
    """Per-channel credentials: {"feishu": {...}, "dingtalk": {...},
    "wechat": {...}}. Each channel documents its own keys (app_id/secret,
    corp_id/token/encoding_aes_key, etc.)."""
    if not path:
        return {}
    import json
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def main(argv=None):
    p = argparse.ArgumentParser(description="IM webhook gateway for a MetaAgent agent.")
    p.add_argument("--url", required=True, help="agent WebSocket URL, e.g. ws://host:8765")
    p.add_argument("--token", default="", help="agent auth token, if set")
    p.add_argument("--port", type=int, default=9000)
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--config", default="", help="JSON file of per-channel creds "
                  "({feishu:{...}, dingtalk:{...}, wechat:{...}})")
    for n in ("feishu", "dingtalk", "wechat"):
        p.add_argument(f"--{n}", action="store_true", help=f"enable the {n} channel")
    args = p.parse_args(argv)
    names = [n for n in ("feishu", "dingtalk", "wechat") if getattr(args, n)]
    if not names:
        p.error("enable at least one channel, e.g. --feishu")
    config = _load_config(args.config)
    asyncio.run(serve(args.url, args.token, names, args.port, args.host, config=config))


if __name__ == "__main__":
    main()
