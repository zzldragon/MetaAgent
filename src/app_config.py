"""Configuration for MetaAgent itself (not for generated agents).

The API key lives in config.json next to this file, per Spec.md.
A missing file is created with defaults so the app always starts.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time

# ── path resolution: source run vs frozen PyInstaller build ──────────────────
# Two roots, distinct only when frozen:
#   BASE_DIR  = the READ-ONLY bundle root — where --add-data lands (templates,
#               graphs, assets). Frozen: sys._MEIPASS (a temp dir for --onefile,
#               _internal\ for --onedir). Source run: this file's directory.
#   DATA_DIR  = the WRITABLE root for user data (config.json, tools the coding
#               agent writes, generated_agents, history, recents). Frozen: the
#               folder holding the .exe — so data PERSISTS next to the exe and
#               is never lost to --onefile's per-run temp extraction. Source
#               run: same as BASE_DIR (byte-identical behaviour, tests unaffected).
_FROZEN = getattr(sys, "frozen", False)
if _FROZEN:
    BASE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(sys.executable)))
    DATA_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    DATA_DIR = BASE_DIR

# read-only bundle data
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
# writable user data (next to the exe when frozen)
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
TOOLS_DIR = os.path.join(DATA_DIR, "tools")
GENERATED_DIR = os.path.join(DATA_DIR, "generated_agents")
HISTORY_PATH = os.path.join(DATA_DIR, "chat_history.json")
# Recently opened / saved projects, shown on the welcome launcher. Kept in its
# own file (not config.json) so it never tangles with the self-healing settings
# merge, and so wiping it is just deleting one file.
RECENTS_PATH = os.path.join(DATA_DIR, "recent_projects.json")
MAX_RECENTS = 8


def _seed_data_dir() -> None:
    """First-run bootstrap for a frozen build: copy the bundled example tools
    (BASE_DIR/tools, read-only) out to the writable DATA_DIR/tools so the
    designer sees the shipped tools AND can add/save their own there. No-op for a
    source run (BASE_DIR == DATA_DIR) and once the writable copy already exists."""
    if not _FROZEN or BASE_DIR == DATA_DIR:
        return
    try:
        bundled = os.path.join(BASE_DIR, "tools")
        if os.path.isdir(bundled) and not os.path.exists(TOOLS_DIR):
            shutil.copytree(bundled, TOOLS_DIR)
        os.makedirs(GENERATED_DIR, exist_ok=True)
    except OSError:
        pass  # fail-soft: a read-only/locked target must not block startup


_seed_data_dir()

# Provider-neutral: the Tool Generator + Estimation talk to ANY OpenAI-compatible
# endpoint — point "api_key" / "base_url" / "model" at your provider (OpenAI,
# DeepSeek, SiliconFlow, a local server, …); the Anthropic API is also supported.
# The defaults below are just DeepSeek-V4-Flash via SiliconFlow.
# Note: base_url must NOT include /chat/completions — the SDK appends it.
DEFAULTS = {
    "api_key": "",                     # API key for your LLM provider
    "model": "deepseek-ai/DeepSeek-V4-Flash",
    "base_url": "https://api.siliconflow.cn/v1",
    "hitl_confirm": True,              # confirm before the agent writes tools
    # Hard per-request timeout (seconds) for the coding agent's LLM calls so a
    # stalled endpoint can't hang the app. Mirrors the generated agents'
    # request_timeout_s. Set to null in config.json to fall back to the SDK
    # default (~600s).
    "request_timeout_s": 120,
    # Optional HTTP/HTTPS proxy for the coding agent's LLM calls, e.g.
    # "http://10.144.1.10:8080". Leave "" to use the process's HTTP(S)_PROXY
    # environment variables (or a direct connection if none are set). Set this
    # when the app is launched WITHOUT inheriting a corporate proxy (e.g. from a
    # desktop shortcut) and the API host is only reachable through that proxy —
    # otherwise the call hangs on a blocked/black-holed connection.
    "proxy": "",
    # Context capacity (tokens) = the coding agent model's context window. When set
    # (>0), older turns are COMPACTED (LLM-summarized, keeping the system prompt and
    # the most recent turns) as the input nears it — instead of stopping. 0 = no
    # context control (rely on the provider's own limit). Set this to your model's
    # window, e.g. 128000.
    "context_capacity": 0,
    # UI theme for the app: "dark" (default) or "light" (white background,
    # black text). Set via the designer's Configure → Theme menu.
    "theme": "dark",
    # UI language: "en" (default) or "zh" (Simplified Chinese). Set via the
    # welcome window's Settings → Language menu.
    "language": "en",
}

_THEMES = ("dark", "light")
_LANGUAGES = ("en", "zh")


def get_language() -> str:
    """The saved UI language ("en" | "zh"), defaulting to "en"."""
    lang = load_config().get("language", "en")
    return lang if lang in _LANGUAGES else "en"


def set_language(lang: str) -> None:
    """Persist the UI language choice to config.json."""
    cfg = load_config()
    cfg["language"] = lang if lang in _LANGUAGES else "en"
    save_config(cfg)


def get_theme() -> str:
    """The saved UI theme ("dark" | "light"), defaulting to "dark"."""
    t = load_config().get("theme", "dark")
    return t if t in _THEMES else "dark"


def set_theme(name: str) -> None:
    """Persist the UI theme choice to config.json."""
    cfg = load_config()
    cfg["theme"] = name if name in _THEMES else "dark"
    save_config(cfg)


def _migrate(cfg: dict) -> dict:
    """Back-compat: the API key was historically stored under the SiliconFlow-
    specific 'deepseek_api_key'. It is now the provider-neutral 'api_key'. Carry an
    existing legacy value over (unless 'api_key' is already set) and drop the old
    field, so a config written before the rename keeps working."""
    if "deepseek_api_key" in cfg:
        legacy = cfg.pop("deepseek_api_key")
        if not str(cfg.get("api_key") or "").strip():
            cfg["api_key"] = legacy
    return cfg


def load_config() -> dict:
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        cfg = {}
    cfg = _migrate(cfg)
    merged = {**DEFAULTS, **cfg}
    if merged != cfg:                       # persists the migration (drops the old key)
        save_config(merged)
    return merged


def save_config(cfg: dict) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


# ── recent projects (welcome launcher) ──────────────────────────────────────
def load_recent_projects() -> list[dict]:
    """Most-recent-first list of {"path": str, "opened_at": epoch_seconds}.
    Tolerates a missing/corrupt file by returning []."""
    try:
        with open(RECENTS_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []
    if not isinstance(data, list):
        return []
    out = []
    for item in data:
        if isinstance(item, dict) and item.get("path"):
            out.append({"path": str(item["path"]),
                        "opened_at": float(item.get("opened_at", 0) or 0)})
    return out


def save_recent_projects(items: list[dict]) -> None:
    try:
        with open(RECENTS_PATH, "w", encoding="utf-8") as f:
            json.dump(items[:MAX_RECENTS], f, indent=2, ensure_ascii=False)
    except OSError:
        pass


def add_recent_project(path: str) -> None:
    """Record (or bump) a project path at the top of the recents list."""
    if not path:
        return
    path = os.path.abspath(path)
    key = os.path.normcase(path)
    items = [it for it in load_recent_projects()
             if os.path.normcase(it["path"]) != key]
    items.insert(0, {"path": path, "opened_at": time.time()})
    save_recent_projects(items)


def remove_recent_project(path: str) -> None:
    key = os.path.normcase(os.path.abspath(path))
    items = [it for it in load_recent_projects()
             if os.path.normcase(os.path.abspath(it["path"])) != key]
    save_recent_projects(items)
