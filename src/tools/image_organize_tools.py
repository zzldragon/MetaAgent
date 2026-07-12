"""Sort classified furniture images into per-category folders.

Step 3 of the furniture-ad collector: move each downloaded image out of the inbox into
``<FURNITURE_DIR>/<category>/`` so everything is filed neatly by type. Also exposes a
summary tool for the report and the GUI.

Collect dir: env ``FURNITURE_DIR`` (default ``./collected_images``).

Conventions: ``from tool_registry import tool``; every top-level ``def`` is a tool,
so all helpers are LAMBDAS. Tools return strings and never raise. Uses stdlib only.
"""
from tool_registry import tool

import os
import re
import json
import shutil
import time

_CATS = ["sofa", "bed", "dining", "table", "storage", "lighting",
         "decor", "rug", "outdoor", "office", "promo", "other"]
_dir = lambda: os.environ.get("FURNITURE_DIR", os.path.join(os.getcwd(), "collected_images"))
_cat = lambda c: (c or "").strip().lower() if (c or "").strip().lower() in _CATS else "other"
_uniq = lambda p: p if not os.path.exists(p) else os.path.join(
    os.path.dirname(p),
    f"{os.path.splitext(os.path.basename(p))[0]}_{int(time.time()*1000)%100000}{os.path.splitext(p)[1]}")


@tool(risk="high")
def organize_image(image_path: str, category: str, caption: str = "") -> str:
    """Move one classified image into its category subfolder and log its caption.
    Call after classify_image, once per image. Returns the new local path.

    Args:
        image_path: current local path of the image (usually in the _inbox folder).
        category: the slug from classify_image (sofa/bed/dining/table/storage/lighting/
                  decor/rug/outdoor/office/promo/other); anything else -> "other".
        caption: optional short description to record alongside the image.
    """
    if not os.path.isfile(image_path):
        return f"[ERROR] Not a file: {image_path}"
    cat = _cat(category)
    dest_dir = os.path.join(_dir(), cat)
    try:
        os.makedirs(dest_dir, exist_ok=True)
        dest = _uniq(os.path.join(dest_dir, os.path.basename(image_path)))
        shutil.move(image_path, dest)
    except Exception as e:  # noqa: BLE001
        return f"[ERROR] Could not move {image_path}: {e}"
    if (caption or "").strip():
        try:
            log = os.path.join(_dir(), "captions.json")
            db = {}
            if os.path.isfile(log):
                db = json.load(open(log, "r", encoding="utf-8"))
            db[os.path.relpath(dest, _dir()).replace("\\", "/")] = caption.strip()
            json.dump(db, open(log, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        except Exception:  # noqa: BLE001
            pass
    return f"[filed] {dest}"


@tool
def list_collected(category: str = "") -> str:
    """Summarize what has been collected so far: image counts per category folder (and,
    if a category slug is given, the file names inside it). Use it for the final report.

    Args:
        category: optional slug to list files for; empty = show the per-category totals.
    """
    root = _dir()
    if not os.path.isdir(root):
        return "[empty] No images collected yet."
    if (category or "").strip():
        cat = _cat(category)
        d = os.path.join(root, cat)
        if not os.path.isdir(d):
            return f"[empty] No images in category '{cat}'."
        files = [f for f in os.listdir(d) if not f.startswith(".")]
        return f"{cat}: {len(files)} images\n" + "\n".join(sorted(files))
    lines, total = [], 0
    for cat in _CATS:
        d = os.path.join(root, cat)
        if os.path.isdir(d):
            n = len([f for f in os.listdir(d) if not f.startswith(".")])
            if n:
                lines.append(f"  {cat:<9} {n}")
                total += n
    inbox = os.path.join(root, "_inbox")
    pending = len([f for f in os.listdir(inbox)]) if os.path.isdir(inbox) else 0
    head = f"已归类 {total} 张，共 {len([l for l in lines])} 个类目" + (
        f"（inbox 还有 {pending} 张待处理）" if pending else "")
    return head + ("\n" + "\n".join(lines) if lines else "")
