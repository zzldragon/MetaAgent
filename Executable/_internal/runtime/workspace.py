# ── workspace ───────────────────────────────────────────────────────────────
WORKSPACE_PATH = os.path.join(BASE_DIR, "workspace.json")


def _ws_path(session_id=None) -> str:
    """The workspace file for a session: the shared workspace.json for the default
    (GUI/CLI) session, else a per-session workspace-<sid>.json so concurrent web /
    gateway users don't share (or overwrite) each other's folder selection. When
    session_id is None it resolves from the current run state (_rs())."""
    sid = session_id
    if sid is None:
        try:
            sid = _rs().session_id or ""
        except Exception:
            sid = ""
    if sid:
        return os.path.join(BASE_DIR, "workspace-" + _fs_safe(sid) + ".json")
    return WORKSPACE_PATH


def _read_ws(path) -> list:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f).get("folders", []) or []
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def get_workspace(session_id=None) -> list:
    """Folders the agent works on. A session that set its OWN workspace uses it;
    otherwise it inherits the shared default workspace.json — so the common
    single-workspace deployment (and HTTP file serving) keeps working, while a
    concurrent user can override with its own folders."""
    p = _ws_path(session_id)
    folders = _read_ws(p)
    if not folders and p != WORKSPACE_PATH:
        folders = _read_ws(WORKSPACE_PATH)          # inherit the shared default
    return [d for d in folders if os.path.isdir(d)]


def set_workspace(folders: list, session_id=None) -> None:
    with open(_ws_path(session_id), "w", encoding="utf-8") as f:
        json.dump({"folders": folders}, f, indent=2)


def add_workspace_folder(folder: str, session_id=None) -> list:
    folders = get_workspace(session_id)
    if folder not in folders:
        folders.append(folder)
    set_workspace(folders, session_id)
    return folders


def workspace_context() -> str:
    """System-prompt section describing the workspace folders and their files."""
    folders = get_workspace()
    if not folders:
        return ""
    lines = ["", "", "# Workspace",
             "You work on the following folders. Use absolute paths from this "
             "listing when calling tools."]
    for folder in folders:
        lines.append(f"- {folder}")
        try:
            entries = sorted(os.listdir(folder))[:40]
        except OSError as e:
            lines.append(f"    (cannot list: {e})")
            continue
        for entry in entries:
            lines.append(f"    {os.path.join(folder, entry)}")
    return "\n".join(lines)


def resolve_workspace_path(value):
    """Resolve a relative path in a tool argument against workspace folders."""
    if not isinstance(value, str) or os.path.isabs(value) or os.path.exists(value):
        return value
    for folder in get_workspace():
        candidate = os.path.join(folder, value)
        if os.path.exists(candidate):
            return candidate
    return value
