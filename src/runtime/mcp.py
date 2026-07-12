# ── MCP clients: connect to MCP servers, expose their tools (Pillar) ────────
MCP_SERVERS = CONFIG.get("mcp_servers", [])
_MCP = {"loop": None, "started": False, "schemas": {}, "sessions": {},
        "by_server": {}}
_MCP_START_LOCK = threading.Lock()   # guard the once-only start (concurrent 1st runs)


def _mcp_label(sid: str) -> str:
    """Human-friendly server label (the canvas node name) for log/error lines;
    the raw id is still used internally to attach tools to agents."""
    for s in MCP_SERVERS:
        if s.get("id") == sid:
            return s.get("name") or sid
    return sid


def _mcp_srv(sid: str) -> dict:
    """The config entry for a server id (for per-server Extra Settings), or {}."""
    for s in MCP_SERVERS:
        if s.get("id") == sid:
            return s
    return {}


def _mcp_tool_wrapper(server_id: str, tool_name: str):
    """Sync wrapper that dispatches an MCP tool call onto the bg event loop."""
    def call(**kwargs):
        import asyncio
        session = _MCP["sessions"].get(server_id)
        if session is None or _MCP["loop"] is None:
            return f"[ERROR] MCP server '{_mcp_label(server_id)}' is not connected."
        try:
            fut = asyncio.run_coroutine_threadsafe(
                session.call_tool(tool_name, kwargs), _MCP["loop"])
            res = fut.result(timeout=_mcp_srv(server_id).get("call_timeout") or 60)
        except Exception as e:
            return f"[ERROR] MCP tool '{tool_name}' failed: {e}"
        parts = []
        for block in (getattr(res, "content", None) or []):
            text = getattr(block, "text", None)
            if text is not None:
                parts.append(text)
        out = "\n".join(parts) if parts else str(res)
        return ("[ERROR] " + out) if getattr(res, "isError", False) else out
    call.__name__ = tool_name
    return call


def _mcp_http_factory(verify_tls: bool, url: str = ""):
    """httpx client factory for MCP HTTP transports. verify_tls=False accepts
    self-signed certs (common for local https servers). For a LOCAL server the
    env/system proxy is ignored (trust_env=False) — otherwise a corporate/VPN
    proxy may try to tunnel to localhost and answer 'Tunnel connection failed:
    403 Forbidden'."""
    import urllib.parse
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    local = host in ("localhost", "127.0.0.1", "::1") or host.startswith("127.")

    def factory(headers=None, timeout=None, auth=None):
        import httpx
        kwargs = {"follow_redirects": True, "verify": verify_tls}
        if local:
            kwargs["trust_env"] = False          # ignore HTTP(S)_PROXY for localhost
        if headers:
            kwargs["headers"] = headers
        kwargs["timeout"] = timeout if timeout is not None else httpx.Timeout(30.0)
        if auth is not None:
            kwargs["auth"] = auth
        return httpx.AsyncClient(**kwargs)
    return factory


async def _mcp_session(sid, read, write, emit, rev, stop) -> None:
    """Open a ClientSession, register its tools, then hold it open (in THIS
    task) until shutdown is signalled."""
    from mcp import ClientSession
    async with ClientSession(read, write) as session:
        await session.initialize()
        resp = await session.list_tools()
        srv = _mcp_srv(sid)
        allow = set(srv.get("allow_tools") or [])   # opt-in: expose only these
        deny = set(srv.get("deny_tools") or [])      # opt-in: hide these (deny wins)
        names = []
        for t in resp.tools:
            if (allow and t.name not in allow) or (t.name in deny):
                continue                              # filtered before it can attach
            if t.name in TOOLS:
                emit(f"[mcp] {_mcp_label(sid)}: skipping duplicate tool '{t.name}'")
                continue
            TOOLS[t.name] = _mcp_tool_wrapper(sid, t.name)
            _MCP["schemas"][t.name] = {
                "description": t.description or t.name,
                "parameters": t.inputSchema or {"type": "object",
                                                "properties": {}},
            }
            names.append(t.name)
        _MCP["sessions"][sid] = session
        _MCP["by_server"][sid] = names
        emit(f"[mcp] connected '{_mcp_label(sid)}' ({len(names)} tool(s))")
        rev.set()
        await stop.wait()              # keep the session + transport alive


async def _mcp_hold(srv, rev, stop, emit) -> None:
    """Connect one server and hold it. CRITICAL: every async context for the
    transport is entered AND exited in THIS one task (plain nested `async
    with`). Entering them in a child task (asyncio.wait_for) or an AsyncExitStack
    closed from another task is what raised 'CancelledError: Cancelled via
    cancel scope' — anyio cancel scopes must live in a single task."""
    import asyncio
    sid = srv["id"]
    transport = srv.get("transport", "stdio")
    try:
        if transport == "stdio":
            from mcp import StdioServerParameters
            from mcp.client.stdio import stdio_client
            _env = srv.get("env")            # MERGE with os.environ (a bare dict would
            params = StdioServerParameters(  # wipe PATH etc. and the child would fail)
                command=srv["command"], args=srv.get("args", []),
                env=({**os.environ, **_env} if _env else None))
            async with stdio_client(params) as (read, write):
                await _mcp_session(sid, read, write, emit, rev, stop)
        elif transport in ("streamable_http", "http"):
            from mcp.client.streamable_http import streamablehttp_client
            factory = _mcp_http_factory(srv.get("verify_tls", True), srv["url"])
            async with streamablehttp_client(
                    srv["url"], headers=srv.get("headers") or None,
                    httpx_client_factory=factory) as (read, write, _sid):
                await _mcp_session(sid, read, write, emit, rev, stop)
        else:  # sse
            from mcp.client.sse import sse_client
            async with sse_client(
                    srv["url"],
                    headers=srv.get("headers") or None) as (read, write):
                await _mcp_session(sid, read, write, emit, rev, stop)
    except asyncio.CancelledError:       # timed out / shutting down
        rev.set()
        raise
    except BaseException as e:           # isolate: one bad server doesn't poison
        emit(f"[mcp] failed to connect '{_mcp_label(sid)}': {type(e).__name__}: {e}")
        rev.set()


async def _mcp_serve(ready, emit) -> None:
    """One hold-task per server (each owns its transport's cancel scope). Wait
    for each to connect or fail (bounded), signal overall ready, then idle so
    the sessions stay open for the life of the process."""
    import asyncio
    stop = asyncio.Event()
    shutdown = asyncio.Event()           # set by mcp_reconnect() to tear down
    _MCP["stop"], _MCP["shutdown"] = stop, shutdown
    holds = []
    for srv in MCP_SERVERS:
        _MCP["by_server"].setdefault(srv["id"], [])
        rev = asyncio.Event()
        holds.append((srv, rev, asyncio.ensure_future(
            _mcp_hold(srv, rev, stop, emit))))
    for srv, rev, task in holds:
        _ct = srv.get("connect_timeout") or 30
        try:
            await asyncio.wait_for(rev.wait(), timeout=_ct)
        except asyncio.TimeoutError:
            emit(f"[mcp] failed to connect '{_mcp_label(srv['id'])}': timed out after {_ct}s")
            task.cancel()
    ready.set()
    try:
        await shutdown.wait()          # idle until reconnect/shutdown is signalled
    finally:
        stop.set()
        for _srv, _rev, task in holds:
            task.cancel()
        await asyncio.gather(*[t for _s, _r, t in holds],
                             return_exceptions=True)


def _mcp_ensure_started(emit=print) -> None:
    """Idempotent: connect all MCP servers once, attach their tools to the
    agents that linked them. Never raises — a dead/misconfigured server just
    logs a warning and yields 0 tools; the agent keeps running.
    """
    # Double-checked start: with the global run lock gone, two sessions' first
    # turns can call this at once — without the lock both would spawn the loop.
    if _MCP["started"]:
        return
    with _MCP_START_LOCK:
        if _MCP["started"]:
            return
        _MCP["started"] = True
    if not MCP_SERVERS:
        return
    import asyncio
    ready = threading.Event()

    def run_loop():
        loop = asyncio.new_event_loop()
        _MCP["loop"] = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_mcp_serve(ready, emit))
        except BaseException:
            ready.set()

    _t = threading.Thread(target=run_loop, daemon=True)
    _MCP["thread"] = _t
    _t.start()
    ready.wait(timeout=45)
    # attach discovered tools to the agents that linked each server
    for spec in AGENTS.values():
        for sid in spec.get("mcp", []):
            for tname in _MCP["by_server"].get(sid, []):
                if tname not in spec["tools"]:
                    spec["tools"].append(tname)


def mcp_reconnect(emit=print) -> None:
    """Tear down the current MCP connections and reconnect — picks up a restarted
    server or edited config.json mcp_servers. Driven by the GUI's Reload menu;
    call when the agent is idle. Re-reads MCP_SERVERS from the (possibly reloaded)
    CONFIG."""
    global MCP_SERVERS
    loop, shutdown, thread = (_MCP.get("loop"), _MCP.get("shutdown"),
                              _MCP.get("thread"))
    if loop is not None and shutdown is not None:
        try:
            loop.call_soon_threadsafe(shutdown.set)   # clean same-task teardown
        except Exception:
            pass
    if thread is not None:
        thread.join(timeout=15)
    # drop the MCP-registered tools so stale ones don't linger, and strip them
    # from the agents' tool lists; _mcp_ensure_started re-attaches the fresh ones.
    for _sid, names in list(_MCP.get("by_server", {}).items()):
        for tname in names:
            TOOLS.pop(tname, None)
    for spec in AGENTS.values():
        spec["tools"] = [t for t in spec["tools"] if t in TOOLS]
    _MCP.update({"loop": None, "started": False, "sessions": {},
                 "by_server": {}, "schemas": {}, "shutdown": None,
                 "stop": None, "thread": None})
    MCP_SERVERS = CONFIG.get("mcp_servers", [])      # config may have changed
    emit("[mcp] reconnecting...")
    _mcp_ensure_started(emit)
