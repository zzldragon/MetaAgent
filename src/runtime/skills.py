# ── runtime-managed skills (Cursor/Claude-style progressive disclosure) ──────
# Only each skill's name + description enter the system prompt (Layer 1). The
# full body (Layer 2) is loaded ON DEMAND — by the model via the load_skill
# tool when a description matches, or by the user typing /<name>. A skill flagged
# disable_model_invocation is omitted from the auto-pick list and applies only
# via /<name>. This keeps baseline context tiny no matter how many skills exist.
SKILLS_PATH = os.path.join(BASE_DIR, "skills.json")


def _load_skills() -> dict:
    try:
        with open(SKILLS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {k: list(v) for k, v in data.items()}
    except (OSError, json.JSONDecodeError):
        pass
    base = CONFIG.get("skills", {})
    return ({k: [dict(s) for s in v] for k, v in base.items()}
            if isinstance(base, dict) else {})


SKILLS = _load_skills()
# Auto-discovered skills from the workspace (<ws>/.*/skills/<name>/SKILL.md,
# Cursor/Claude-style) now live on the per-run state (_rs().ws_skills), refreshed
# each run from THIS session's workspace so they never leak across concurrent runs.


def save_skills() -> None:
    try:
        with open(SKILLS_PATH, "w", encoding="utf-8") as f:
            json.dump(SKILLS, f, indent=2, ensure_ascii=False)
    except OSError:
        pass


def skills_for(agent_name: str) -> list:
    return SKILLS.get(agent_name, [])


def _all_skills() -> list:
    out = []
    for items in SKILLS.values():
        out.extend(items)
    return out


def _skill_desc(s: dict) -> str:
    """Layer-1 routing line: the explicit description, else the body's first
    non-empty line (so legacy skills without a description still route)."""
    d = (s.get("description") or "").strip()
    if d:
        return d
    for line in (s.get("text") or "").splitlines():
        t = line.strip().lstrip("#").strip()
        if t:
            return t[:200]
    return (s.get("name") or "skill").strip()


def _render_skills_block(items: list) -> str:
    """Render the '## Available skills' block (progressive disclosure: names +
    descriptions only) from a list of skill dicts. Pure + globals-free: this is
    the SINGLE definition of the rendering, shared by skills_block (runtime) and
    gen-time graph_codegen._compose_system_prompt (which loads it from THIS
    fragment's source), so the two can't drift. Pinned by test_prompt_parity.py."""
    auto, manual = [], []
    for s in items or []:
        name = (s.get("name") or "skill").strip()
        if not name:
            continue
        if s.get("disable_model_invocation"):
            manual.append(name)
        else:
            auto.append("- " + name + ": " + _skill_desc(s))
    if not auto and not manual:
        return ""
    out = ["\n\n## Available skills"]
    if auto:
        out.append(
            "\nIf the request matches one of these skills, call the load_skill "
            "tool with its name FIRST to load the full instructions, then follow "
            "them:\n" + "\n".join(auto))
    if manual:
        out.append("\nUser-only skills (apply only when the user types /<name>): "
                   + ", ".join(sorted(manual)))
    return "".join(out)


def skills_block(agent_name: str) -> str:
    """The agent's skills rendered for its system prompt — names + descriptions
    only (progressive disclosure); empty when the agent has no skills. The entry
    agent also gets workspace-discovered skills; rendering goes through the shared
    _render_skills_block (matched at gen time by _compose_system_prompt)."""
    items = list(skills_for(agent_name))
    if "ENTRY" in globals() and agent_name == ENTRY:
        _ws = _rs().ws_skills             # per-run workspace skills (this session)
        if _ws:
            items += _ws                  # workspace skills augment the entry agent
    return _render_skills_block(items)


def list_skill_agents() -> list:
    """Agents that have (or had at generation) skills — drives the GUI menu."""
    base = CONFIG.get("skills", {})
    names = set(base if isinstance(base, dict) else {}) | set(SKILLS)
    return sorted(names)


def find_skill(name: str):
    """Resolve a skill by name across all agents + workspace (names = global IDs)."""
    key = (name or "").strip().lower()
    for s in _all_skills() + _rs().ws_skills:
        if (s.get("name") or "").strip().lower() == key:
            return s
    return None


def _skill_body_text(s: dict, name: str) -> str:
    body = (s.get("text") or "").strip()
    nm = (s.get("name") or name).strip()
    return f"# Skill: {nm}\n\n" + (body or "(this skill has no body)")


def load_skill(name: str) -> str:
    """Load the full step-by-step instructions for one of the Available skills,
    then follow them. Call this when the user's request matches a skill listed
    under '## Available skills'.

    Args:
        name: the skill's name, exactly as shown in the Available skills list.
    """
    s = find_skill(name)
    if s is None:
        names = ", ".join(sorted((x.get("name") or "")
                                 for x in _all_skills())) or "(none)"
        return f"[skill not found: {name}] Available skills: {names}"
    if s.get("disable_model_invocation"):
        return (f"[skill '{name}' is user-only] Ask the user to type /{name} "
                "to apply it.")
    return _skill_body_text(s, name)


def skill_command(task: str) -> str:
    """If `task` begins with /<known-skill>, rewrite it to apply that skill's
    full body (user-invoked — works even for disable_model_invocation skills).
    Unknown /commands and plain text pass through unchanged."""
    m = re.match(r"\s*/([A-Za-z0-9_\-]+)\s*(.*)", task or "", re.DOTALL)
    if not m:
        return task
    s = find_skill(m.group(1))
    if s is None:
        return task
    rest = m.group(2).strip()
    return ("Apply the following skill, then address the user's request.\n\n"
            + _skill_body_text(s, m.group(1))
            + "\n\n---\nUser request: " + (rest or "(follow the skill)"))


def add_skill(agent_name: str, name: str, text: str, description: str = "",
              disable_model_invocation: bool = False) -> None:
    SKILLS.setdefault(agent_name, []).append(
        {"name": name, "description": description, "text": text,
         "disable_model_invocation": bool(disable_model_invocation)})
    save_skills()


def update_skill(agent_name: str, index: int, name: str, text: str,
                 description: str = "",
                 disable_model_invocation: bool = False) -> None:
    items = SKILLS.setdefault(agent_name, [])
    if 0 <= index < len(items):
        items[index] = {"name": name, "description": description, "text": text,
                        "disable_model_invocation": bool(disable_model_invocation)}
        save_skills()


def remove_skill(agent_name: str, index: int) -> None:
    items = SKILLS.setdefault(agent_name, [])
    if 0 <= index < len(items):
        items.pop(index)
        save_skills()


# ── SKILL.md (Cursor/Claude format) parsing + workspace auto-discovery ───────
def _parse_frontmatter(fm: str) -> dict:
    """Tiny YAML-subset parser for SKILL.md frontmatter (no PyYAML dependency):
    `key: value`, quoted values, and folded/literal block scalars (>, >-, |, |-)
    whose continuation lines are indented."""
    fields, lines, i = {}, fm.split("\n"), 0
    while i < len(lines):
        line = lines[i]; i += 1
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = re.match(r"([A-Za-z0-9_-]+)\s*:\s*(.*)$", line)
        if not m:
            continue
        key, val = m.group(1).strip(), m.group(2).strip()
        if val in (">", ">-", ">+", "|", "|-", "|+"):     # block scalar
            block = []
            while i < len(lines) and (not lines[i].strip()
                                      or lines[i][:1] in (" ", "\t")):
                block.append(lines[i].strip()); i += 1
            joiner = "\n" if val.startswith("|") else " "
            fields[key] = joiner.join(block).strip()
        else:
            if len(val) >= 2 and val[0] in "\"'" and val[-1] == val[0]:
                val = val[1:-1]
            fields[key] = val
    return fields


def parse_skill_md(text: str) -> dict:
    """Parse a Cursor/Claude SKILL.md (YAML frontmatter + markdown body) into
    {name, description, text, disable_model_invocation}. Missing frontmatter ->
    the whole text becomes the body."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    name = description = ""
    disable = False
    body = text
    m = re.match(r"\s*---\n(.*?)\n---\n?(.*)", text, re.DOTALL)
    if m:
        body = m.group(2)
        f = _parse_frontmatter(m.group(1))
        name = (f.get("name") or "").strip()
        description = (f.get("description") or "").strip()
        dv = f.get("disable-model-invocation", f.get("disable_model_invocation"))
        disable = str(dv).strip().lower() in ("true", "yes", "1", "on")
    return {"name": name, "description": description, "text": body.strip(),
            "disable_model_invocation": disable}


def _scan_workspace_skills() -> list:
    """Discover <workspace>/.*/skills/<name>/SKILL.md (Cursor/Claude layout):
    a hidden dir, a skills/ folder, a per-skill dir, a SKILL.md. Returns parsed
    skill dicts; the dir name is the fallback skill name."""
    folders = get_workspace() if "get_workspace" in globals() else []
    out, seen = [], set()
    for ws in folders:
        try:
            hidden = [d for d in os.listdir(ws) if d.startswith(".")]
        except OSError:
            continue
        for hd in hidden:
            sroot = os.path.join(ws, hd, "skills")
            if not os.path.isdir(sroot):
                continue
            try:
                names = sorted(os.listdir(sroot))
            except OSError:
                continue
            for nm in names:
                skdir = os.path.join(sroot, nm)
                if not os.path.isdir(skdir):
                    continue
                path = next((os.path.join(skdir, fn) for fn in os.listdir(skdir)
                             if fn.lower() == "skill.md"), None)
                if not path:
                    continue
                try:
                    with open(path, encoding="utf-8", errors="ignore") as f:
                        s = parse_skill_md(f.read())
                except OSError:
                    continue
                s["name"] = (s.get("name") or nm).strip() or nm
                key = s["name"].lower()
                if key in seen:
                    continue
                seen.add(key)
                s["_source"] = path
                out.append(s)
    return out


def refresh_workspace_skills() -> list:
    """Re-scan the workspace for SKILL.md skills (called each run, so they pick
    up file edits). When any are found, make sure the entry agent carries the
    load_skill tool so the model can auto-load them."""
    _ws = _rs().ws_skills                  # per-run: this session's workspace only
    _ws[:] = _scan_workspace_skills()
    if _ws and "AGENTS" in globals() and "ENTRY" in globals():
        spec = AGENTS.get(ENTRY)
        if spec is not None and "load_skill" not in spec.setdefault("tools", []):
            spec["tools"].append("load_skill")
    return _ws
