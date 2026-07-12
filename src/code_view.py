"""'Check Code' — per-node view of the generated code.

Given a graph and a node, generate the real agent.py (+ config.json) and locate the
CHARACTER SPANS that node contributed, so the canvas can show the full generated
code with that node's regions highlighted (the VB "code-behind" idea, adapted).

Non-invasive: it runs the normal codegen and finds each node's regions by anchor
search in the output (no template changes → generation stays byte-identical). The
generated app's node-specific content is a name-keyed topology block (PERSONAS,
AGENTS, PIPELINE, SUCCESSORS, STAGE_KINDS, CONDITIONS, SETSTATE, GUARDRAIL_NODES)
plus `# --- from tools/<file> ---` blocks; LLM config lives in config.json. The
shared runtime below belongs to no node (nothing highlights there — that's correct).
"""

from __future__ import annotations

import os
import shutil


def _matching(text: str, i: int) -> int:
    """Index just past the bracket matching text[i] (one of ([{), quote-aware
    (skips brackets inside '…' / \"…\" string literals)."""
    pairs = {"(": ")", "[": "]", "{": "}"}
    if i < 0 or i >= len(text) or text[i] not in pairs:
        return i + 1
    depth, q, j = 0, None, i
    while j < len(text):
        c = text[j]
        if q:
            if c == "\\":
                j += 2
                continue
            if c == q:
                q = None
        elif c in "'\"":
            q = c
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    return len(text)


def _first_bracket(text: str, i: int) -> int:
    while i < len(text) and text[i] in " \t":
        i += 1
    return i if i < len(text) and text[i] in "([{" else -1


def _key_block(text: str, container: str, key: str):
    """Span (start,end) of `key <bracket>…<bracket>` inside `container` (a dict/
    section marker). `key` includes quotes+colon, e.g. "'gate':". None if absent."""
    c = text.find(container)
    if c < 0:
        return None
    k = text.find(key, c)
    if k < 0:
        return None
    b = _first_bracket(text, k + len(key))
    return (k, _matching(text, b)) if b >= 0 else None


def _entry_span(text: str, container: str, key: str):
    """Span of a `key value` entry inside a dict container, where value may be a
    bracketed literal or a bare scalar (up to the next comma/close)."""
    c = text.find(container)
    if c < 0:
        return None
    cend = _matching(text, _first_bracket(text, c + len(container)))
    k = text.find(key, c, cend)
    if k < 0:
        return None
    b = _first_bracket(text, k + len(key))
    if b >= 0:
        return (k, _matching(text, b))
    e = k + len(key)
    while e < len(text) and text[e] not in ",}\n":
        e += 1
    return (k, e)


def _persona_block(text: str, name: str):
    """Span of `'name': \"\"\"…\"\"\"` (or ''' / '…') inside the PERSONAS block."""
    p = text.find("PERSONAS = {")
    if p < 0:
        return None
    key = "%r:" % name          # -> 'name':
    k = text.find(key, p)
    if k < 0:
        return None
    j = k + len(key)
    while j < len(text) and text[j] == " ":
        j += 1
    for q in ('"""', "'''", '"', "'"):
        if text.startswith(q, j):
            end = text.find(q, j + len(q))
            return (k, end + len(q)) if end >= 0 else None
    return None


def _tool_block(text: str, fname: str):
    """Span of the `# --- from tools/<fname> ---` inlined block. Located exactly by
    reconstructing the chunk the codegen inlined (`_inline_tool_files([fname])`),
    which is a verbatim substring of agent.py — robust regardless of where the
    tools section sits or how many blank lines the source has."""
    chunk = None
    try:
        from graph_codegen import _inline_tool_files
        chunk = _inline_tool_files([fname]).strip("\n")
    except Exception:
        chunk = None
    hdr = "# --- from tools/%s ---" % fname
    k = text.find(hdr)
    if k < 0:
        return None
    if chunk:
        i = text.find(chunk)
        if i >= 0:
            return (i, i + len(chunk))
        return (k, min(k + len(chunk), len(text)))   # header found, body drifted: use chunk length
    nxt = text.find("# --- from tools/", k + len(hdr))
    return (k, nxt if nxt >= 0 else len(text))


def code_for_node(graph, node_id: str) -> dict:
    """Generate the app and return what `node_id` contributed:
    {files:{'agent.py','config.json'}, spans:{file:[(start,end)…]}, node, note}
    or {error} if the graph can't generate."""
    import graph_codegen as gc
    from graph_model import AGENT_KINDS, tool_files

    node = graph.nodes.get(node_id)
    if node is None:
        return {"error": "Node not found."}
    try:
        out = gc.generate_from_graph(graph, "_codeview_tmp", gui=False)
    except Exception as e:  # noqa: BLE001 — analyze errors etc.
        return {"error": f"Can't generate code for this graph:\n{e}"}
    try:
        agent_src = open(os.path.join(out, "agent.py"), encoding="utf-8").read()
        cfg_path = os.path.join(out, "config.json")
        cfg_src = open(cfg_path, encoding="utf-8").read() if os.path.exists(cfg_path) else ""
    finally:
        shutil.rmtree(out, ignore_errors=True)

    a, c, notes = [], [], []
    kind, name = node.kind, node.name

    def linked_agents():
        return [graph.nodes[e.dst].name for e in graph.edges
                if e.src == node_id and e.dst in graph.nodes
                and graph.nodes[e.dst].kind in AGENT_KINDS]

    if kind in AGENT_KINDS:
        for sp in (_key_block(agent_src, "AGENTS = {", "%r:" % name),
                   _persona_block(agent_src, name),
                   _entry_span(agent_src, "SUCCESSORS = {", "%r:" % name),
                   _entry_span(agent_src, "STAGE_KINDS = {", "%r:" % name)):
            if sp:
                a.append(sp)
        cc = _key_block(cfg_src, '"llms"', '"%s":' % name)
        if cc:
            c.append(cc)
    elif kind in ("condition", "while"):
        for sp in (_key_block(agent_src, "CONDITIONS = {", "%r:" % name),
                   _entry_span(agent_src, "SUCCESSORS = {", "%r:" % name),
                   _entry_span(agent_src, "STAGE_KINDS = {", "%r:" % name)):
            if sp:
                a.append(sp)
    elif kind == "setstate":
        for sp in (_key_block(agent_src, "SETSTATE = {", "%r:" % name),
                   _entry_span(agent_src, "SUCCESSORS = {", "%r:" % name)):
            if sp:
                a.append(sp)
    elif kind == "guardrail":
        sp = _key_block(agent_src, "GUARDRAIL_NODES = {", "%r:" % name)
        if sp:
            a.append(sp)
    elif kind == "end":
        for sp in (_entry_span(agent_src, "SUCCESSORS = {", "%r:" % name),
                   _entry_span(agent_src, "STAGE_KINDS = {", "%r:" % name)):
            if sp:
                a.append(sp)
    elif kind == "prompt":
        for ag in linked_agents():
            sp = _persona_block(agent_src, ag)
            if sp:
                a.append(sp)
        if not a:
            notes.append("No persona region found (the agent may use its role default).")
    elif kind == "llm":
        for ag in linked_agents():
            cc = _key_block(cfg_src, '"llms"', '"%s":' % ag)
            if cc:
                c.append(cc)
        notes.append("An LLM node configures config.json → llms[<agent>].")
    elif kind == "tool":
        for f in tool_files(node):
            sp = _tool_block(agent_src, f)
            if sp:
                a.append(sp)
        if not a:
            notes.append("No inlined tool block found (no files selected?).")
    else:
        notes.append(f"A '{kind}' node contributes to config.json and/or the shared "
                     "runtime; it has no isolated agent.py region to highlight.")

    return {
        "files": {"agent.py": agent_src, "config.json": cfg_src},
        "spans": {"agent.py": sorted(a), "config.json": sorted(c)},
        "node": f"{name}  ({kind})",
        "note": "  ".join(notes),
    }
