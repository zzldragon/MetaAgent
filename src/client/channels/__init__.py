"""Webhook channel adapters for IM platforms (WeChat Work / DingTalk / Feishu).

Each is a Channel that receives the platform's callback, normalizes it to an
Inbound, runs the agent via the Hub, and replies through the platform API.
These are SCAFFOLDS: the protocol/orchestration is real and reused, but the
platform-specific signature verification, payload parsing, and send-API calls
are marked TODO — they need your app credentials + a public callback URL.
"""
