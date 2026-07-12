"""MetaAgent client: talk to a generated agent's web server.

Layers:
  agent_client.AgentClient   transport core (the WS protocol) — shared by all
  channel.Channel / Hub      the multi-channel seam (GUI now; IM webhooks later)
  qt_app                     PySide6 desktop client (interactive console channel)
  channels/                  WeChat / DingTalk / Feishu webhook adapters (stubs)
"""
from client.agent_client import AgentClient, AgentError, RunResult

__all__ = ["AgentClient", "AgentError", "RunResult"]
