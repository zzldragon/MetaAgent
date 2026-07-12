"""Competitor / news feed toolset for the AI WeChat content factory.

Step 1-3 of the pipeline (选赛道 → 找竞品 → 抓竞品文案): pull the latest headlines
from competitor WeChat accounts and news sites so the agent has fresh, real material
to pick a topic from and to learn the competitor's title/structure style.

The recommended way to expose a WeChat public account as a feed is the self-hosted
**we-mp-rss** service (https://github.com/rachelos/we-mp-rss): it turns subscribed
公众号 into standard RSS. Any RSS/Atom URL works here (we-mp-rss, smzdm RSS, a news
site's feed, …).

Conventions (MetaAgent tool contract):
  * ``from tool_registry import tool``; EVERY top-level ``def`` becomes a tool, so all
    helpers are LAMBDAS or nested functions.
  * Tools never raise — they return a human-readable string; errors come back as
    ``"[ERROR] ..."`` so the model can recover.
"""
from tool_registry import tool

import os
import re
import xml.etree.ElementTree as ET
import urllib.request

_UA = "Mozilla/5.0 (compatible; MetaAgentContentBot/1.0)"
_MAX = 12000
_clip = lambda s: s if len(s) <= _MAX else s[:_MAX] + "\n... [truncated] ..."
_strip_html = lambda s: re.sub(r"<[^>]+>", "", s or "").strip()
_fetch = lambda url: urllib.request.urlopen(
    urllib.request.Request(url, headers={"User-Agent": _UA}), timeout=30
).read().decode("utf-8", "replace")
# we-mp-rss default RSS route for one feed id: {base}/feed/{id}
_wemp_url = lambda base, fid: base.rstrip("/") + "/feed/" + str(fid).strip()


@tool
def fetch_feed_articles(feed_url: str, limit: int = 10) -> str:
    """Fetch the latest articles from ONE RSS/Atom feed (e.g. a we-mp-rss feed of a
    competitor 公众号, or a news-site feed). Returns a numbered list of
    title / link / short summary — use it to see what competitors are publishing and
    to harvest candidate topics.

    Args:
        feed_url: the full RSS or Atom URL.
        limit: max number of recent items to return (default 10).
    """
    try:
        xml = _fetch(feed_url)
    except Exception as e:  # noqa: BLE001
        return f"[ERROR] Could not fetch feed {feed_url}: {e}"
    try:
        root = ET.fromstring(xml)
    except Exception as e:  # noqa: BLE001
        return f"[ERROR] Feed is not valid XML ({feed_url}): {e}"

    def _text(el, *tags):
        for t in tags:
            found = el.find(t)
            if found is not None and (found.text or "").strip():
                return found.text.strip()
            # Atom link is an attribute
            if t.endswith("link"):
                for lk in el.findall(t):
                    href = lk.get("href")
                    if href:
                        return href.strip()
        return ""

    # RSS <item> or Atom <entry> (namespace-agnostic: match by local tag name)
    items = [el for el in root.iter() if el.tag.split("}")[-1] in ("item", "entry")]
    if not items:
        return f"[ERROR] No <item>/<entry> found in feed {feed_url}."
    out = []
    for i, it in enumerate(items[: max(1, int(limit))], 1):
        title = _text(it, "title", "{http://www.w3.org/2005/Atom}title")
        link = _text(it, "link", "{http://www.w3.org/2005/Atom}link")
        summary = _strip_html(_text(
            it, "description", "summary",
            "{http://www.w3.org/2005/Atom}summary"))
        out.append(f"{i}. {title}\n   link: {link}\n   summary: {summary[:200]}")
    return _clip(f"[{len(out)} articles from {feed_url}]\n" + "\n".join(out))


@tool
def fetch_many_feeds(feed_urls: str, limit_per_feed: int = 6) -> str:
    """Fetch latest headlines from SEVERAL feeds at once and merge them into one
    candidate-topic list. Use this at the start of a run to gather fresh material
    across all the competitor accounts / news sources you track.

    Args:
        feed_urls: comma- or newline-separated RSS/Atom URLs.
        limit_per_feed: max items to pull from each feed (default 6).
    """
    urls = [u.strip() for u in re.split(r"[,\n]", feed_urls or "") if u.strip()]
    if not urls:
        return "[ERROR] No feed URLs given. Pass comma-separated RSS/Atom URLs."
    blocks = [fetch_feed_articles(u, limit_per_feed) for u in urls]
    return _clip("\n\n".join(blocks))


@tool
def fetch_wemp_feed(wemp_base_url: str, feed_id: str, limit: int = 10) -> str:
    """Fetch one competitor 公众号's latest articles from a self-hosted we-mp-rss
    instance (https://github.com/rachelos/we-mp-rss). Convenience wrapper that builds
    the ``{base}/feed/{id}`` RSS URL for you.

    Args:
        wemp_base_url: your we-mp-rss base URL, e.g. http://127.0.0.1:8001
        feed_id: the feed/account id shown in the we-mp-rss dashboard.
        limit: max recent items (default 10).
    """
    base = (wemp_base_url or os.environ.get("WEMP_RSS_BASE", "")).strip()
    if not base:
        return "[ERROR] No we-mp-rss base URL (arg or env WEMP_RSS_BASE)."
    return fetch_feed_articles(_wemp_url(base, feed_id), limit)
