"""WeChat Official-Account publishing toolset (the "baoyu post-to-wechat" step).

Step 6 of the pipeline (把文章放进后台草稿箱): upload images to WeChat's servers and
create a DRAFT article via the official Draft API. Nothing is auto-published to
followers — it lands in the 草稿箱 for a final human glance, which is the safe default
for an automated pipeline.

Credentials come from the environment (NEVER hard-code / never put in the graph):
  * ``WECHAT_APPID``  — the official account AppID
  * ``WECHAT_SECRET`` — the AppSecret
The server's IP must be on the account's IP allow-list (PPTX 第六步: 固定 IP 白名单).

Official API refs used:
  * token:            GET  /cgi-bin/token
  * inline image:     POST /cgi-bin/media/uploadimg      -> a URL usable in content
  * permanent image:  POST /cgi-bin/material/add_material?type=image  -> media_id (cover/thumb)
  * create draft:     POST /cgi-bin/draft/add

Conventions: ``from tool_registry import tool``; every top-level ``def`` is a tool,
so all helpers are LAMBDAS. Tools return strings and never raise. Requires ``requests``.
"""
from tool_registry import tool

import os
import json
import requests

_API = "https://api.weixin.qq.com/cgi-bin"
_creds = lambda: (os.environ.get("WECHAT_APPID", "").strip(),
                  os.environ.get("WECHAT_SECRET", "").strip())
_token_json = lambda: requests.get(
    _API + "/token",
    params={"grant_type": "client_credential",
            "appid": _creds()[0], "secret": _creds()[1]},
    timeout=30).json()
# Return (access_token, human-readable error). Surfaces WeChat's real errcode/errmsg
# (e.g. 40164 = IP not on allow-list, 40013 = bad AppID, 40125 = bad AppSecret) instead
# of a generic message, and distinguishes "env var not set in this process".
_auth = lambda: (
    ("", "WECHAT_APPID / WECHAT_SECRET 在当前运行进程里读不到"
         "（Debug Run 需在【启动画布之前】于同一终端设好环境变量）。")
    if not all(_creds())
    else (lambda j: (j.get("access_token", ""),
                     "" if j.get("access_token")
                     else f"微信返回 errcode={j.get('errcode')}, errmsg={j.get('errmsg')}"
                          "（40164=IP不在白名单/48001=接口未授权/40013=AppID错/40125=AppSecret错）")
          )(_token_json())).get("access_token", "")


@tool
def upload_content_image(image_path: str) -> str:
    """Upload a LOCAL image file for use INSIDE article content and return a WeChat
    image URL (from /media/uploadimg). Put the returned URL in the article's <img>
    tags. This upload does NOT count against the media library quota.

    Args:
        image_path: local path to a .jpg/.png file (e.g. from download_image).
    """
    if not os.path.isfile(image_path):
        return f"[ERROR] Not a file: {image_path}"
    try:
        tok, err = _auth()
    except Exception as e:  # noqa: BLE001
        return f"[ERROR] 请求微信 access_token 失败: {e}"
    if not tok:
        return f"[ERROR] 拿不到 access_token：{err}"
    try:
        with open(image_path, "rb") as f:
            r = requests.post(_API + "/media/uploadimg",
                              params={"access_token": tok},
                              files={"media": f}, timeout=60).json()
        if r.get("url"):
            return f"[url] {r['url']}"
        return f"[ERROR] WeChat rejected the image: {json.dumps(r, ensure_ascii=False)}"
    except Exception as e:  # noqa: BLE001
        return f"[ERROR] upload_content_image failed: {e}"


@tool
def upload_cover_image(image_path: str) -> str:
    """Upload a LOCAL image as a PERMANENT material and return its media_id, to be
    used as the article COVER/thumbnail (thumb_media_id in create_draft). Every draft
    needs exactly one cover.

    Args:
        image_path: local path to the cover image file.
    """
    if not os.path.isfile(image_path):
        return f"[ERROR] Not a file: {image_path}"
    try:
        tok, err = _auth()
    except Exception as e:  # noqa: BLE001
        return f"[ERROR] 请求微信 access_token 失败: {e}"
    if not tok:
        return f"[ERROR] 拿不到 access_token：{err}"
    try:
        with open(image_path, "rb") as f:
            r = requests.post(_API + "/material/add_material",
                              params={"access_token": tok, "type": "image"},
                              files={"media": f}, timeout=60).json()
        if r.get("media_id"):
            return f"[media_id] {r['media_id']}"
        return f"[ERROR] WeChat rejected the cover: {json.dumps(r, ensure_ascii=False)}"
    except Exception as e:  # noqa: BLE001
        return f"[ERROR] upload_cover_image failed: {e}"


@tool(risk="high")
def create_wechat_draft(title: str, content_html: str, thumb_media_id: str,
                        digest: str = "", author: str = "") -> str:
    """Create a DRAFT article in the WeChat 草稿箱 (does NOT publish to followers).
    Call this LAST, once the article is written, reviewed, images are uploaded, and
    you have a cover thumb_media_id.

    Args:
        title: article title (WeChat caps ~64 chars).
        content_html: full HTML body; <img> src must be WeChat URLs from
            upload_content_image (external image hosts are stripped by WeChat).
        thumb_media_id: cover media_id from upload_cover_image.
        digest: optional summary/摘要 shown in the list (auto if blank).
        author: optional author name.
    """
    if not (title and content_html and thumb_media_id):
        return "[ERROR] title, content_html and thumb_media_id are all required."
    try:
        tok, err = _auth()
    except Exception as e:  # noqa: BLE001
        return f"[ERROR] 请求微信 access_token 失败: {e}"
    if not tok:
        return f"[ERROR] 拿不到 access_token：{err}"
    article = {
        "title": title[:64], "author": author, "digest": digest[:120],
        "content": content_html, "content_source_url": "",
        "thumb_media_id": thumb_media_id,
        "need_open_comment": 0, "only_fans_can_comment": 0,
    }
    try:
        payload = json.dumps({"articles": [article]}, ensure_ascii=False).encode("utf-8")
        r = requests.post(_API + "/draft/add",
                          params={"access_token": tok},
                          data=payload,
                          headers={"Content-Type": "application/json"},
                          timeout=60).json()
        if r.get("media_id"):
            return f"[draft-created] media_id={r['media_id']} — check the 草稿箱."
        return f"[ERROR] draft/add failed: {json.dumps(r, ensure_ascii=False)}"
    except Exception as e:  # noqa: BLE001
        return f"[ERROR] create_wechat_draft failed: {e}"
