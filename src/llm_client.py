"""Thin DeepSeek chat client (OpenAI-compatible API)."""

from __future__ import annotations

# NOTE: the OpenAI SDK is imported lazily inside LLMClient.__init__, not here.
# `from openai import OpenAI` costs ~3s and was the single biggest reason opening
# the Tool Generator felt slow (it sits on the coding_agent import chain). Keeping
# it off module import makes `import coding_agent` instant; the app also pre-warms
# the SDK in a background thread at launch (see canvas_qt.welcome.run).

# Sentinel returned by chat() when a streamed response was aborted mid-flight
# via should_cancel (so the caller can tell "stopped" apart from an empty reply).
CANCELLED = object()


class _Function:
    __slots__ = ("name", "arguments")

    def __init__(self, name: str, arguments: str):
        self.name = name
        self.arguments = arguments


class _ToolCall:
    """Minimal stand-in for an OpenAI tool_call, assembled from stream deltas."""
    __slots__ = ("id", "type", "function")

    def __init__(self, id: str, name: str, arguments: str):
        self.id = id
        self.type = "function"
        self.function = _Function(name, arguments)


class _Message:
    """Minimal stand-in for an OpenAI assistant message (.content, .tool_calls)
    so streaming and non-streaming returns look identical to the caller."""
    __slots__ = ("content", "tool_calls")

    def __init__(self, content: str, tool_calls: list):
        self.content = content
        self.tool_calls = tool_calls


class LLMClient:
    def __init__(self, api_key: str, base_url: str, model: str,
                 request_timeout_s: float | None = 120, proxy: str | None = None):
        self.model = model
        # Lazy import (see module note): the first client construction pays the
        # SDK import cost, which the app pre-warms in the background at launch.
        from openai import OpenAI
        import httpx
        # A hard per-request timeout so a stalled endpoint can't hang the app
        # forever; None falls back to ~10 min. The SHORT connect timeout is the
        # important part: when the API host is only reachable through a corporate
        # proxy and the connection is blocked/dropped (e.g. the GUI process didn't
        # inherit HTTP(S)_PROXY), the call FAILS FAST with a clear error instead
        # of hanging on "Thinking…" forever. The longer read timeout covers a slow
        # (reasoning) model once connected.
        read = float(request_timeout_s) if request_timeout_s else 600.0
        timeout = httpx.Timeout(read, connect=min(15.0, read))
        # Build the http client explicitly so an EXPLICIT proxy (from config) is
        # honored even when the process has no proxy env vars. trust_env=True keeps
        # the env-var proxy (HTTP(S)_PROXY / NO_PROXY) working when none is set in
        # config — so behaviour is unchanged for users who launch with the env set.
        http_client = httpx.Client(proxy=proxy or None, timeout=timeout,
                                   trust_env=True)
        self._client = OpenAI(api_key=api_key, base_url=base_url,
                              timeout=timeout, http_client=http_client)
        self.last_usage = None    # token usage of the most recent chat() call
        self._active_stream = None  # in-flight stream, so cancel() can close it

    def chat(self, messages: list[dict], max_tokens: int = 4096,
             tools: list | None = None, should_cancel=None):
        """One chat completion; returns the assistant message object
        (.content, .tool_calls). Token usage is stored on self.last_usage.

        Pass `tools` (OpenAI function-calling schemas) to enable native tool
        calls. Pass `should_cancel` (a no-arg callable returning bool) to make
        the call STREAM so it can be aborted mid-response: the stream is checked
        before each chunk and closed when should_cancel() is true, in which case
        chat() returns the CANCELLED sentinel. Without should_cancel the call is
        a single blocking request (cannot be interrupted until it returns)."""
        kwargs = {}
        if tools:
            kwargs["tools"] = tools

        if should_cancel is None:
            rsp = self._client.chat.completions.create(
                model=self.model, messages=messages, max_tokens=max_tokens,
                **kwargs)
            self.last_usage = getattr(rsp, "usage", None)
            return rsp.choices[0].message

        # Streaming path: the Stop button can abort mid-response (a non-streaming
        # call is opaque and the user would otherwise wait until it returns).
        self.last_usage = None
        stream = self._client.chat.completions.create(
            model=self.model, messages=messages, max_tokens=max_tokens,
            stream=True, stream_options={"include_usage": True}, **kwargs)
        self._active_stream = stream     # let cancel() force-close it
        text_parts: list[str] = []
        acc: dict[int, dict] = {}        # tool_call deltas accumulated by index
        cancelled = False
        try:
            for chunk in stream:
                if should_cancel():
                    cancelled = True
                    break
                if getattr(chunk, "usage", None):
                    self.last_usage = chunk.usage
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                if getattr(delta, "content", None):
                    text_parts.append(delta.content)
                for tcd in (getattr(delta, "tool_calls", None) or []):
                    slot = acc.setdefault(tcd.index,
                                          {"id": None, "name": None, "args": ""})
                    if tcd.id:
                        slot["id"] = tcd.id
                    if tcd.function and tcd.function.name:
                        slot["name"] = tcd.function.name
                    if tcd.function and tcd.function.arguments:
                        slot["args"] += tcd.function.arguments
        except Exception:
            if should_cancel():          # cancel() force-closed the stream
                cancelled = True
            else:
                raise
        finally:
            self._active_stream = None
            try:
                stream.close()
            except Exception:
                pass

        if cancelled:
            return CANCELLED
        tool_calls = [
            _ToolCall(slot["id"] or f"call_{i}", slot["name"],
                      slot["args"] or "{}")
            for i, slot in sorted(acc.items()) if slot["name"]
        ]
        return _Message("".join(text_parts), tool_calls)

    def cancel(self) -> None:
        """Force-close the in-flight stream (if any) so a blocked read — e.g.
        waiting for the first token — unblocks at once. Safe from another thread;
        chat() turns the resulting error into the CANCELLED sentinel (because
        should_cancel() is true by then)."""
        s = self._active_stream
        if s is not None:
            try:
                s.close()
            except Exception:
                pass
