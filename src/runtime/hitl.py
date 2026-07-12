# ── human-in-the-loop confirmation for high-risk tools (Pillar 9) ───────────
HIGH_RISK_MARKERS = ("write", "save", "delete", "remove", "send", "post",
                     "exec", "drop", "create", "update", "upload", "shell")


def _console_confirm(tool_name: str, args: dict) -> dict:
    """Default (CLI) high-risk confirmation: show the FULL args (never trimmed) and
    ask allow/deny. Returns the rich outcome dict the new handler contract uses."""
    print("\n--- CONFIRM HIGH-RISK TOOL --------------------------------")
    print("Tool: " + str(tool_name))
    try:
        print(json.dumps(args, ensure_ascii=False, indent=2))
    except Exception:
        print(str(args))
    print("-----------------------------------------------------------")
    try:
        ans = input("[a]llow / [d]eny (default deny): ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        ans = ""
    return {"decision": "allow" if ans.startswith(("a", "y")) else "deny"}


# Confirm/review handlers, the "don't ask again" set, AND the serialization lock
# all live on the per-run state (_rs().confirm / .review / .allow / .lock) so
# concurrent sessions don't share a handler OR block each other on a global lock
# while one waits for a human. The lock still serializes a SINGLE run's pool-worker
# prompts. A run with no handler installed falls back to the console default below.


def _confirm_prompt(tool_name, args) -> str:
    return ("Allow high-risk tool '" + str(tool_name) + "' with args "
            + json.dumps(args, ensure_ascii=False) + "?")


def _adapt_confirm(fn):
    """Normalize any confirm handler to the fn(tool_name, args) form. A NEW handler
    takes two required positional params and is used as-is; a LEGACY handler (the
    documented fn(prompt)->bool: one required positional, or a variadic/defaulted
    shape written against that contract) is WRAPPED so it still receives a full,
    untrimmed prompt string. Detection is by required-positional ARITY, never by
    catching TypeError — so an unusual legacy signature can't silently receive
    (tool_name, args) and misclassify, and a genuine TypeError inside a valid
    handler is never mistaken for an arity error (it propagates as the real bug)."""
    try:
        params = inspect.signature(fn).parameters.values()
        required = sum(1 for p in params
                       if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
                       and p.default is p.empty)
    except (TypeError, ValueError):
        required = 2                       # unknown (e.g. a builtin) -> assume new
    if required >= 2:
        return fn
    return lambda tool_name, args: fn(_confirm_prompt(tool_name, args))


def set_confirm_handler(fn) -> None:
    """Replace the confirmation UI. The handler is called `fn(tool_name, args)` and
    may return either a rich dict — {"decision": "allow"|"deny", "args": <edited
    args dict>, "remember": bool} — or a plain bool (True=allow, False=deny). An
    edited `args` replaces the call's arguments; `remember` suppresses further
    prompts for that tool this run. A legacy one-arg `fn(prompt)->bool` handler is
    detected by its signature and wrapped to still receive a full prompt string.
    The GUI installs a dialog here; eval/headless runners install an auto-allow.
    Installed on the CURRENT run's state, so concurrent sessions each keep their own
    handler (the server installs a browser bridge per connection)."""
    _rs().confirm["fn"] = _adapt_confirm(fn)


def reset_confirm_session() -> None:
    """Clear the per-run 'don't ask again for this tool' allowances. Called at the
    start of each run so a remembered allowance never leaks across tasks."""
    _rs().allow.clear()


def is_high_risk(tool_name: str) -> bool:
    """Whether a tool needs HITL confirmation. An EXPLICIT per-tool classification
    is authoritative over the name heuristic, both ways:
      - in `high_risk_tools` → always confirm (even if innocuously named, e.g. a
        `refresh_cache` that secretly wipes data);
      - in `safe_tools` → never confirm (even if its name matches a marker, e.g. a
        read-only `update_dashboard`).
    These lists are populated from each tool's own `@tool(risk="high"|"safe")`
    declaration at generation time (and editable in config.json). Only when a tool
    is in neither list do we fall back to the name-substring heuristic."""
    if tool_name in CONFIG.get("high_risk_tools", []):
        return True
    if tool_name in CONFIG.get("safe_tools", []):
        return False
    low = tool_name.lower()
    return any(m in low for m in HIGH_RISK_MARKERS)


def confirm_tool(tool_name: str, args: dict) -> bool:
    """True if the call may proceed. Only high-risk tools ask (when enabled).
    The handler sees the FULL args (never trimmed) and may EDIT them (an edited
    args dict is applied in place, so the tool runs with the reviewed version) or
    ask to not be prompted again for this tool this run. The lock serializes
    prompts so parallel pool workers can't pop overlapping dialogs."""
    if not CONFIG.get("hitl_confirm", True) or not is_high_risk(tool_name):
        return True
    _st = _rs()                            # this run's confirm handler + remember-set
    with _st.lock:                         # serialize THIS run's prompts + remember-set
        if tool_name in _st.allow:         # re-checked here so parallel pool workers
            return True                    # can't both prompt for the same tool
        _fn = _st.confirm["fn"] or _console_confirm    # console fallback if none set
        res = _fn(tool_name, args)                      # always the 2-arg form (adapted)
        if isinstance(res, bool):
            res = {"decision": "allow" if res else "deny"}
        elif not isinstance(res, dict):
            res = {"decision": "deny"}
        if res.get("remember"):
            _st.allow.add(tool_name)
    allowed = res.get("decision", "deny") != "deny"
    if allowed:
        edited = res.get("args")
        if isinstance(edited, dict) and edited is not args:
            args.clear()                 # apply reviewed/edited args in place, so
            args.update(edited)          # the react loop runs the reviewed version
    trace_event("hitl", tool=tool_name, allowed=allowed)
    return allowed


class HumanRejected(Exception):
    """A HITL checkpoint was rejected with on_reject='stop' — ends the run."""


def _console_review(prompt: str, content: str, choices=None) -> dict:
    print("\n--- HUMAN REVIEW -------------------------------------------")
    print(prompt + "\n")
    print(content)
    print("-----------------------------------------------------------")
    if choices:                            # ROUTE mode: pick which branch runs next
        for i, c in enumerate(choices, 1):
            print("  %d. %s" % (i, c))
        try:
            ans = input("Choose next step 1-%d (blank = default): " % len(choices)).strip()
        except (EOFError, KeyboardInterrupt):
            ans = ""
        # Abstain (blank / EOF / unrecognized) -> return "" so the CALLER applies the
        # node's default_route. Returning choices[0] here would silently pick the first
        # branch and mask the operator's configured default on unattended runs.
        pick = ""
        if ans.isdigit() and 1 <= int(ans) <= len(choices):
            pick = choices[int(ans) - 1]
        elif ans in choices:
            pick = ans
        return {"decision": pick, "content": content, "feedback": ""}
    try:
        ans = input("[a]pprove / [e]dit / [r]eject (default approve): ")
    except (EOFError, KeyboardInterrupt):
        ans = ""
    ans = ans.strip().lower()
    if ans.startswith("r"):
        try:
            fb = input("Rejection feedback: ").strip()
        except (EOFError, KeyboardInterrupt):
            fb = ""
        return {"decision": "reject", "content": content, "feedback": fb}
    if ans.startswith("e"):
        try:
            edited = input("Edited content (blank keeps original): ")
        except (EOFError, KeyboardInterrupt):
            edited = ""
        return {"decision": "edit", "content": edited or content,
                "feedback": ""}
    return {"decision": "approve", "content": content, "feedback": ""}


def set_review_handler(fn) -> None:
    """Replace the HITL review UI. fn(prompt, content) -> a dict with keys
    decision ('approve'|'edit'|'reject'), content (text to forward on), and
    feedback. The GUI installs a dialog; eval/headless runners install
    auto-approve. Installed on the current run's state (per-session)."""
    _rs().review["fn"] = fn


def _invoke_review(fn, prompt, content, choices):
    """Call a review handler, passing `choices` ONLY when it can accept a third
    argument (route mode). A legacy fn(prompt, content) handler is called with two
    args as before, so gate-mode review stays byte-identical; when route mode meets
    such a handler it simply can't express a branch, and _human_route falls back to
    the default branch. Arity is detected the same way as _adapt_confirm."""
    if choices:
        try:
            params = list(inspect.signature(fn).parameters.values())
            npos = sum(1 for p in params
                       if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD))
            var_kw = any(p.kind == p.VAR_KEYWORD for p in params)
            # a param literally named 'choices' (whether pos-or-kw or KEYWORD-ONLY)
            named = any(p.name == "choices" and p.kind in
                        (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY) for p in params)
        except (TypeError, ValueError):
            npos, var_kw, named = 3, False, True   # unknown (e.g. a builtin) -> try new form
        if named or var_kw:
            # bind by KEYWORD so a keyword-only `*, choices=None` handler works too
            # (a positional call would TypeError and abort the whole run).
            return fn(prompt, content, choices=choices)
        if npos >= 3:
            return fn(prompt, content, choices)   # a 3rd positional param not named 'choices'
    return fn(prompt, content)


def _call_with_timeout(fn, timeout, default):
    """Run fn() in a daemon thread; return its result, or `default` if it doesn't
    finish within `timeout` seconds. A late-finishing orphan thread is discarded (the
    reviewer window may linger; the run has already moved on with the auto-decision)."""
    box = {}

    def _work():
        try:
            box["res"] = fn()
        except Exception:
            box["res"] = default
    t = threading.Thread(target=_work, daemon=True)
    t.start()
    t.join(timeout)
    return box["res"] if "res" in box else default


def human_review(prompt: str, content: str, timeout=0, on_timeout="approve",
                 choices=None) -> dict:
    """Run one human-review round (serialized with the tool-confirm lock so parallel
    workers never pop overlapping dialogs). Always returns a dict with decision/
    content/feedback. When timeout>0 and the reviewer doesn't answer in time, auto-
    decides `on_timeout` (for unattended runs); timeout<=0 keeps the exact blocking
    behaviour (byte-identical). `choices` (route mode) offers the reviewer a set of
    next-step branches — the returned `decision` is the chosen branch name; on
    timeout it is `on_timeout` (the caller passes the default branch there)."""
    _st = _rs()
    _fn = _st.review["fn"] or _console_review  # console fallback if none installed;
                                           # resolve in THIS thread (the timeout path
                                           # runs it in a daemon thread w/o our context)
    with _st.lock:                         # this run's lock (not a cross-session one)
        if timeout and timeout > 0:
            res = _call_with_timeout(
                lambda: _invoke_review(_fn, prompt, content or "", choices),
                timeout, None)
            if res is None:
                trace_event("hitl_timeout", decision=on_timeout)
                res = {"decision": on_timeout, "content": content,
                       "feedback": "auto: timeout"}
        else:
            res = _invoke_review(_fn, prompt, content or "", choices)
    if not isinstance(res, dict):
        res = {}
    res.setdefault("decision", "approve")
    res.setdefault("content", content)
    res.setdefault("feedback", "")
    return res
