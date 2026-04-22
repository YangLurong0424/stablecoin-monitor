#!/usr/bin/env python3
"""Generate a static public dashboard from free public discussion sources."""

from __future__ import annotations

import argparse
import datetime as dt
import email.utils
import html
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "sources.json"
USER_AGENT = "StablecoinOverseasMonitor/1.0 (+https://github.com/)"
DEFAULT_TIMEOUT = 20


@dataclass
class SourceStatus:
    name: str
    ok: bool
    fetched: int = 0
    kept: int = 0
    message: str = ""


@dataclass
class Item:
    title: str
    url: str
    source: str
    platform: str
    published: dt.datetime
    snippet: str = ""
    author: str = ""
    query: str = ""
    engagement: int = 0
    source_weight: float = 1.0
    entities: list[str] = field(default_factory=list)
    sentiment: str = "neutral"
    score: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "url": self.url,
            "source": self.source,
            "platform": self.platform,
            "published": self.published.isoformat(),
            "snippet": self.snippet,
            "author": self.author,
            "query": self.query,
            "engagement": self.engagement,
            "entities": self.entities,
            "sentiment": self.sentiment,
            "score": round(self.score, 2),
        }


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def load_config() -> dict[str, Any]:
    with CONFIG_PATH.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def request_text(
    url: str,
    *,
    accept: str = "*/*",
    headers: dict[str, str] | None = None,
    data: bytes | None = None,
    method: str | None = None,
) -> str:
    request_headers = {
        "User-Agent": USER_AGENT,
        "Accept": accept,
    }
    if headers:
        request_headers.update(headers)
    req = urllib.request.Request(
        url,
        data=data,
        headers=request_headers,
        method=method,
    )
    with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT) as resp:
        data = resp.read()
        charset = resp.headers.get_content_charset() or "utf-8"
        return data.decode(charset, errors="replace")


def request_json(url: str, *, headers: dict[str, str] | None = None, payload: dict[str, Any] | None = None) -> Any:
    data = None
    request_headers = headers or {}
    method = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        request_headers = {"Content-Type": "application/json", **request_headers}
        method = "POST"
    return json.loads(request_text(url, accept="application/json", headers=request_headers, data=data, method=method))


def clean_text(value: str | None, *, limit: int = 500) -> str:
    if not value:
        return ""
    text = re.sub(r"<[^>]+>", " ", value)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        return text[: limit - 1].rstrip() + "..."
    return text


def parse_date(value: str | None) -> dt.datetime:
    if not value:
        return utcnow()
    value = value.strip()
    try:
        parsed = email.utils.parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.UTC)
        return parsed.astimezone(dt.UTC)
    except (TypeError, ValueError, IndexError, OverflowError):
        pass
    iso_value = value.replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(iso_value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.UTC)
        return parsed.astimezone(dt.UTC)
    except ValueError:
        return utcnow()


def xml_text(node: ET.Element, names: list[str]) -> str:
    for name in names:
        found = node.find(name)
        if found is not None and found.text:
            return found.text
    return ""


def xml_attr_link(node: ET.Element) -> str:
    link = xml_text(node, ["link"])
    if link:
        return link
    for found in node.findall("{http://www.w3.org/2005/Atom}link"):
        href = found.attrib.get("href")
        if href:
            return href
    return ""


def parse_rss_items(xml_data: str, source_name: str, platform: str, weight: float, query: str = "") -> list[Item]:
    root = ET.fromstring(xml_data)
    nodes = root.findall(".//item")
    if not nodes:
        nodes = root.findall(".//{http://www.w3.org/2005/Atom}entry")

    items: list[Item] = []
    for node in nodes:
        title = clean_text(xml_text(node, ["title", "{http://www.w3.org/2005/Atom}title"]), limit=220)
        url = xml_attr_link(node)
        published = parse_date(
            xml_text(
                node,
                [
                    "pubDate",
                    "published",
                    "updated",
                    "{http://www.w3.org/2005/Atom}published",
                    "{http://www.w3.org/2005/Atom}updated",
                ],
            )
        )
        snippet = clean_text(
            xml_text(
                node,
                [
                    "description",
                    "summary",
                    "content",
                    "{http://www.w3.org/2005/Atom}summary",
                    "{http://www.w3.org/2005/Atom}content",
                ],
            )
        )
        author = clean_text(
            xml_text(
                node,
                [
                    "author",
                    "dc:creator",
                    "{http://purl.org/dc/elements/1.1/}creator",
                    "{http://www.w3.org/2005/Atom}author/{http://www.w3.org/2005/Atom}name",
                ],
            ),
            limit=120,
        )
        if title and url:
            items.append(
                Item(
                    title=title,
                    url=url,
                    source=source_name,
                    platform=platform,
                    published=published,
                    snippet=snippet,
                    author=author,
                    query=query,
                    source_weight=weight,
                )
            )
    return items


def google_news_url(query: str) -> str:
    params = urllib.parse.urlencode({"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"})
    return f"https://news.google.com/rss/search?{params}"


def reddit_rss_url(query: str) -> str:
    params = urllib.parse.urlencode({"q": query, "sort": "new", "t": "day"})
    return f"https://www.reddit.com/search.rss?{params}"


def fetch_google_news(config: dict[str, Any], source: dict[str, Any]) -> tuple[list[Item], SourceStatus]:
    items: list[Item] = []
    status = SourceStatus(source["name"], True)
    for query in config["queries"]:
        try:
            fetched = parse_rss_items(
                request_text(google_news_url(query), accept="application/rss+xml"),
                source["name"],
                "News RSS",
                float(source.get("weight", 1.0)),
                query,
            )
            items.extend(fetched)
            status.fetched += len(fetched)
            time.sleep(0.2)
        except Exception as exc:  # noqa: BLE001 - one source should not break the report.
            status.ok = False
            status.message = f"{query}: {exc}"
    return items, status


def fetch_reddit_rss(config: dict[str, Any], source: dict[str, Any]) -> tuple[list[Item], SourceStatus]:
    items: list[Item] = []
    status = SourceStatus(source["name"], True)
    for query in config["queries"]:
        try:
            fetched = parse_rss_items(
                request_text(reddit_rss_url(query), accept="application/rss+xml"),
                source["name"],
                "Reddit",
                float(source.get("weight", 1.0)),
                query,
            )
            items.extend(fetched)
            status.fetched += len(fetched)
            time.sleep(0.4)
        except Exception as exc:  # noqa: BLE001
            status.ok = False
            status.message = f"{query}: {exc}"
    return items, status


def fetch_bluesky(config: dict[str, Any], source: dict[str, Any]) -> tuple[list[Item], SourceStatus]:
    items: list[Item] = []
    status = SourceStatus(source["name"], True)
    headers: dict[str, str] = {}
    host = "https://public.api.bsky.app"
    identifier = os.environ.get("BSKY_IDENTIFIER", "")
    app_password = os.environ.get("BSKY_APP_PASSWORD", "")
    if identifier and app_password:
        try:
            session = request_json(
                "https://bsky.social/xrpc/com.atproto.server.createSession",
                payload={"identifier": identifier, "password": app_password},
            )
            headers["Authorization"] = f"Bearer {session['accessJwt']}"
            host = "https://bsky.social"
        except Exception as exc:  # noqa: BLE001
            status.ok = False
            status.message = f"Bluesky auth failed: {exc}"

    for query in config["queries"]:
        params = urllib.parse.urlencode({"q": query, "limit": 25, "sort": "latest"})
        url = f"{host}/xrpc/app.bsky.feed.searchPosts?{params}"
        try:
            data = request_json(url, headers=headers)
            posts = data.get("posts", [])
            status.fetched += len(posts)
            for post in posts:
                record = post.get("record") or {}
                author_data = post.get("author") or {}
                handle = author_data.get("handle", "")
                uri = post.get("uri", "")
                rkey = uri.rsplit("/", 1)[-1] if "/" in uri else ""
                link = f"https://bsky.app/profile/{handle}/post/{rkey}" if handle and rkey else ""
                text = clean_text(record.get("text", ""), limit=500)
                title = text.split("\n", 1)[0][:220] or f"Bluesky post by {handle}"
                engagement = int(post.get("likeCount", 0) or 0) + int(post.get("repostCount", 0) or 0) + int(
                    post.get("replyCount", 0) or 0
                )
                if link and text:
                    items.append(
                        Item(
                            title=title,
                            url=link,
                            source=source["name"],
                            platform="Bluesky",
                            published=parse_date(post.get("indexedAt") or record.get("createdAt")),
                            snippet=text,
                            author=handle,
                            query=query,
                            engagement=engagement,
                            source_weight=float(source.get("weight", 1.0)),
                        )
                    )
            time.sleep(0.25)
        except Exception as exc:  # noqa: BLE001
            status.ok = False
            status.message = f"{query}: {exc}"
    if not status.ok and "403" in status.message and not headers:
        status.message += "; set BSKY_IDENTIFIER and BSKY_APP_PASSWORD secrets for free authenticated search"
    return items, status


def fetch_hackernews(config: dict[str, Any], source: dict[str, Any]) -> tuple[list[Item], SourceStatus]:
    items: list[Item] = []
    status = SourceStatus(source["name"], True)
    for query in config["queries"]:
        params = urllib.parse.urlencode({"query": query, "tags": "story,comment", "hitsPerPage": 25})
        url = f"https://hn.algolia.com/api/v1/search_by_date?{params}"
        try:
            data = request_json(url)
            hits = data.get("hits", [])
            status.fetched += len(hits)
            for hit in hits:
                title = clean_text(hit.get("title") or hit.get("story_title") or hit.get("comment_text"), limit=220)
                url_value = hit.get("url") or hit.get("story_url") or ""
                object_id = hit.get("objectID")
                if not url_value and object_id:
                    url_value = f"https://news.ycombinator.com/item?id={object_id}"
                snippet = clean_text(hit.get("comment_text") or hit.get("story_text") or "", limit=500)
                if title and url_value:
                    items.append(
                        Item(
                            title=title,
                            url=url_value,
                            source=source["name"],
                            platform="Hacker News",
                            published=parse_date(hit.get("created_at")),
                            snippet=snippet,
                            author=hit.get("author", ""),
                            query=query,
                            engagement=int(hit.get("points") or 0),
                            source_weight=float(source.get("weight", 1.0)),
                        )
                    )
            time.sleep(0.25)
        except Exception as exc:  # noqa: BLE001
            status.ok = False
            status.message = f"{query}: {exc}"
    return items, status


def fetch_mastodon(config: dict[str, Any], source: dict[str, Any]) -> tuple[list[Item], SourceStatus]:
    items: list[Item] = []
    status = SourceStatus(source["name"], True)
    for instance in source.get("instances", []):
        for query in config["queries"]:
            params = urllib.parse.urlencode({"q": query, "type": "statuses", "limit": 20})
            url = f"https://{instance}/api/v2/search?{params}"
            try:
                data = request_json(url)
                statuses = data.get("statuses", [])
                status.fetched += len(statuses)
                for post in statuses:
                    account = post.get("account") or {}
                    text = clean_text(post.get("content", ""), limit=500)
                    link = post.get("url") or ""
                    engagement = int(post.get("reblogs_count", 0) or 0) + int(post.get("favourites_count", 0) or 0)
                    if text and link:
                        items.append(
                            Item(
                                title=text[:220],
                                url=link,
                                source=f"{source['name']}:{instance}",
                                platform="Mastodon",
                                published=parse_date(post.get("created_at")),
                                snippet=text,
                                author=account.get("acct", ""),
                                query=query,
                                engagement=engagement,
                                source_weight=float(source.get("weight", 1.0)),
                            )
                        )
                time.sleep(0.3)
            except Exception as exc:  # noqa: BLE001
                status.ok = False
                status.message = f"{instance} {query}: {exc}"
    return items, status


def fetch_youtube(config: dict[str, Any], source: dict[str, Any]) -> tuple[list[Item], SourceStatus]:
    api_key = os.environ.get(source.get("requires_env", "YOUTUBE_API_KEY"), "")
    status = SourceStatus(source["name"], bool(api_key), message="YOUTUBE_API_KEY not configured")
    if not api_key:
        return [], status
    items: list[Item] = []
    for query in config["queries"]:
        params = urllib.parse.urlencode(
            {
                "part": "snippet",
                "q": query,
                "type": "video",
                "order": "date",
                "maxResults": 15,
                "key": api_key,
            }
        )
        url = f"https://www.googleapis.com/youtube/v3/search?{params}"
        try:
            data = request_json(url)
            videos = data.get("items", [])
            status.fetched += len(videos)
            for video in videos:
                snippet = video.get("snippet") or {}
                video_id = (video.get("id") or {}).get("videoId", "")
                if video_id:
                    items.append(
                        Item(
                            title=clean_text(snippet.get("title"), limit=220),
                            url=f"https://www.youtube.com/watch?v={video_id}",
                            source=source["name"],
                            platform="YouTube",
                            published=parse_date(snippet.get("publishedAt")),
                            snippet=clean_text(snippet.get("description"), limit=500),
                            author=snippet.get("channelTitle", ""),
                            query=query,
                            source_weight=float(source.get("weight", 1.0)),
                        )
                    )
            time.sleep(0.25)
        except Exception as exc:  # noqa: BLE001
            status.ok = False
            status.message = f"{query}: {exc}"
    return items, status


def fetch_configured_feeds(config: dict[str, Any]) -> tuple[list[Item], list[SourceStatus]]:
    items: list[Item] = []
    statuses: list[SourceStatus] = []
    for feed in config.get("feeds", []):
        status = SourceStatus(feed["name"], True)
        if not feed.get("enabled", True):
            statuses.append(SourceStatus(feed["name"], False, message=feed.get("note", "disabled")))
            continue
        try:
            fetched = parse_rss_items(
                request_text(feed["url"], accept="application/rss+xml"),
                feed["name"],
                "RSS Feed",
                float(feed.get("weight", 1.0)),
            )
            items.extend(fetched)
            status.fetched = len(fetched)
        except Exception as exc:  # noqa: BLE001
            status.ok = False
            status.message = str(exc)
        statuses.append(status)
        time.sleep(0.2)
    return items, statuses


FETCHERS = {
    "google_news_rss": fetch_google_news,
    "reddit_rss": fetch_reddit_rss,
    "bluesky_search": fetch_bluesky,
    "hackernews_algolia": fetch_hackernews,
    "mastodon_search": fetch_mastodon,
    "youtube_search": fetch_youtube,
}


def compile_entity_patterns(config: dict[str, Any]) -> dict[str, re.Pattern[str]]:
    patterns: dict[str, re.Pattern[str]] = {}
    for entity, aliases in config["entities"].items():
        escaped = [re.escape(alias) for alias in aliases]
        patterns[entity] = re.compile(r"(?<![A-Za-z0-9])(" + "|".join(escaped) + r")(?![A-Za-z0-9])", re.I)
    return patterns


def classify_item(item: Item, config: dict[str, Any], patterns: dict[str, re.Pattern[str]]) -> None:
    text = f"{item.title} {item.snippet}".lower()
    item.entities = [entity for entity, pattern in patterns.items() if pattern.search(text)]

    risk_hits = sum(1 for term in config.get("risk_terms", []) if term.lower() in text)
    positive_hits = sum(1 for term in config.get("positive_terms", []) if term.lower() in text)
    if risk_hits:
        item.sentiment = "risk"
    elif positive_hits:
        item.sentiment = "positive"
    else:
        item.sentiment = "neutral"

    age_hours = max(0.0, (utcnow() - item.published).total_seconds() / 3600)
    recency_score = max(0.0, 24.0 - min(age_hours, 48.0) / 2)
    entity_score = len(item.entities) * 6.0
    risk_score = risk_hits * 4.0
    positive_score = positive_hits * 1.5
    engagement_score = math.log1p(max(0, item.engagement)) * 3.0
    item.score = (recency_score + entity_score + risk_score + positive_score + engagement_score) * item.source_weight


def relevant(item: Item, lookback_start: dt.datetime) -> bool:
    if item.published < lookback_start:
        return False
    return bool(item.entities)


def dedupe(items: list[Item]) -> list[Item]:
    seen: dict[str, Item] = {}
    for item in items:
        key = canonical_key(item)
        existing = seen.get(key)
        if existing is None or item.score > existing.score:
            seen[key] = item
    return list(seen.values())


def canonical_key(item: Item) -> str:
    parsed = urllib.parse.urlparse(item.url)
    title_based = normalized_title_key(item.title)
    if title_based and (parsed.netloc.lower() == "news.google.com" or len(title_based) > 32):
        return f"title:{title_based}"
    if parsed.netloc:
        path = parsed.path.rstrip("/")
        return f"{parsed.netloc.lower()}{path}".lower()
    return re.sub(r"\W+", "", item.title.lower())[:120]


def normalized_title_key(title: str) -> str:
    value = html.unescape(title).lower()
    value = re.sub(r"\s+[-|]\s+[a-z0-9 .,&]+$", "", value)
    value = re.sub(r"[^a-z0-9]+", "", value)
    return value[:180]


def build_summary(items: list[Item], config: dict[str, Any]) -> dict[str, Any]:
    entity_counts: dict[str, int] = {entity: 0 for entity in config["entities"]}
    platform_counts: dict[str, int] = {}
    risk_items: list[Item] = []
    for item in items:
        platform_counts[item.platform] = platform_counts.get(item.platform, 0) + 1
        for entity in item.entities:
            entity_counts[entity] += 1
        if item.sentiment == "risk":
            risk_items.append(item)

    narratives = []
    for entity, count in sorted(entity_counts.items(), key=lambda kv: kv[1], reverse=True):
        if count == 0:
            continue
        leading = next((item for item in items if entity in item.entities), None)
        narratives.append(
            {
                "entity": entity,
                "count": count,
                "headline": leading.title if leading else "",
                "url": leading.url if leading else "",
            }
        )

    return {
        "entity_counts": entity_counts,
        "platform_counts": dict(sorted(platform_counts.items(), key=lambda kv: kv[1], reverse=True)),
        "risk_count": len(risk_items),
        "top_risks": [item.as_dict() for item in sorted(risk_items, key=lambda x: x.score, reverse=True)[:12]],
        "narratives": narratives[:8],
    }


def collect(config: dict[str, Any]) -> tuple[list[Item], list[SourceStatus]]:
    all_items: list[Item] = []
    statuses: list[SourceStatus] = []

    for source in config.get("sources", []):
        if not source.get("enabled", True):
            statuses.append(SourceStatus(source["name"], False, message="disabled"))
            continue
        requires_env = source.get("requires_env")
        if requires_env and not os.environ.get(requires_env):
            statuses.append(SourceStatus(source["name"], False, message=f"{requires_env} not configured"))
            continue
        fetcher = FETCHERS.get(source.get("type", ""))
        if not fetcher:
            statuses.append(SourceStatus(source["name"], False, message=f"unknown type {source.get('type')}"))
            continue
        items, status = fetcher(config, source)
        all_items.extend(items)
        statuses.append(status)

    feed_items, feed_statuses = fetch_configured_feeds(config)
    all_items.extend(feed_items)
    statuses.extend(feed_statuses)
    return all_items, statuses


def html_escape(value: Any) -> str:
    return html.escape(str(value), quote=True)


def render_badges(values: list[str], kind: str = "entity") -> str:
    return "".join(f'<span class="badge {kind}">{html_escape(value)}</span>' for value in values)


def format_time(value: dt.datetime, tz: ZoneInfo) -> str:
    return value.astimezone(tz).strftime("%Y-%m-%d %H:%M")


def render_html(payload: dict[str, Any], config: dict[str, Any]) -> str:
    report = config["report"]
    generated = parse_date(payload["generated_at"])
    tz = ZoneInfo(report.get("timezone", "Asia/Shanghai"))
    items = payload["items"]
    statuses = payload["sources"]
    summary = payload["summary"]
    archive_path = f"data/latest.json"

    narrative_rows = "\n".join(
        f"""
        <tr>
          <td>{html_escape(row["entity"])}</td>
          <td>{row["count"]}</td>
          <td><a href="{html_escape(row["url"])}" target="_blank" rel="noopener">{html_escape(row["headline"])}</a></td>
        </tr>
        """
        for row in summary["narratives"]
    ) or '<tr><td colspan="3">暂无有效公开源结果</td></tr>'

    item_cards = "\n".join(render_item_card(item, tz) for item in items[: int(report.get("max_items", 120))])
    if not item_cards:
        item_cards = '<article class="item empty">暂无匹配结果。请稍后重跑 workflow 或增加免费公开源。</article>'

    source_rows = "\n".join(
        f"""
        <tr>
          <td>{html_escape(source["name"])}</td>
          <td><span class="status {'ok' if source["ok"] else 'warn'}">{'正常' if source["ok"] else '跳过/受限'}</span></td>
          <td>{source["fetched"]}</td>
          <td>{source["kept"]}</td>
          <td>{html_escape(source["message"])}</td>
        </tr>
        """
        for source in statuses
    )

    platform_counts = summary.get("platform_counts", {})
    platform_chips = "".join(
        f'<span class="metric-chip">{html_escape(name)} <b>{count}</b></span>' for name, count in platform_counts.items()
    )

    coverage_notes = "".join(f"<li>{html_escape(note)}</li>" for note in config.get("coverage_notes", []))

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html_escape(report["title"])}</title>
  <style>
    :root {{
      --bg: #f7f8fb;
      --surface: #ffffff;
      --text: #18202a;
      --muted: #5e6a78;
      --border: #dfe5ee;
      --blue: #1f5f99;
      --blue-soft: #e8f2fb;
      --green: #2f7d5c;
      --green-soft: #e8f6ef;
      --orange: #b95f24;
      --orange-soft: #fff0e5;
      --purple: #6750a4;
      --purple-soft: #f0edfb;
      --rose: #a94461;
      --rose-soft: #fdebf1;
      --gold: #95720a;
      --gold-soft: #fff7cf;
      --shadow: 0 1px 2px rgba(24, 32, 42, 0.08);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif;
      line-height: 1.55;
    }}
    a {{ color: var(--blue); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    header {{
      background: var(--surface);
      border-bottom: 1px solid var(--border);
      padding: 28px 20px 20px;
      text-align: center;
    }}
    .wrap {{
      width: min(1180px, calc(100% - 32px));
      margin: 0 auto;
    }}
    h1 {{
      margin: 0 0 10px;
      font-size: 30px;
      font-weight: 760;
      letter-spacing: 0;
    }}
    .subline {{
      color: var(--muted);
      margin: 0;
      font-size: 14px;
    }}
    .actions {{
      display: flex;
      justify-content: center;
      gap: 10px;
      flex-wrap: wrap;
      margin-top: 16px;
    }}
    .button {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 36px;
      padding: 7px 12px;
      border: 1px solid var(--border);
      border-radius: 6px;
      background: var(--surface);
      color: var(--text);
      font-size: 14px;
      font-family: inherit;
      box-shadow: var(--shadow);
      cursor: pointer;
    }}
    .button.primary {{
      border-color: #b7cee5;
      background: var(--blue-soft);
      color: var(--blue);
    }}
    main {{ padding: 24px 0 40px; }}
    section {{
      margin: 0 0 26px;
      border-top: 1px solid var(--border);
      padding-top: 22px;
    }}
    section:first-child {{
      border-top: 0;
      padding-top: 0;
    }}
    h2 {{
      margin: 0 0 14px;
      font-size: 19px;
      text-align: center;
      letter-spacing: 0;
    }}
    .metrics {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
    }}
    .metric {{
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 14px 16px;
      background: var(--surface);
      min-height: 96px;
      text-align: center;
    }}
    .metric:nth-child(1) {{ background: var(--blue-soft); }}
    .metric:nth-child(2) {{ background: var(--green-soft); }}
    .metric:nth-child(3) {{ background: var(--orange-soft); }}
    .metric:nth-child(4) {{ background: var(--purple-soft); }}
    .metric .value {{
      display: block;
      font-size: 30px;
      font-weight: 780;
      font-variant-numeric: tabular-nums;
    }}
    .metric:nth-child(1) .value {{ color: var(--blue); }}
    .metric:nth-child(2) .value {{ color: var(--green); }}
    .metric:nth-child(3) .value {{ color: var(--orange); }}
    .metric:nth-child(4) .value {{ color: var(--purple); }}
    .metric .label {{
      color: var(--muted);
      font-size: 13px;
    }}
    .chips {{
      display: flex;
      justify-content: center;
      gap: 8px;
      flex-wrap: wrap;
      margin-top: 12px;
    }}
    .metric-chip {{
      border: 1px solid var(--border);
      background: var(--surface);
      border-radius: 999px;
      padding: 4px 9px;
      font-size: 13px;
      color: var(--muted);
    }}
    .metric-chip b {{
      color: var(--text);
      font-variant-numeric: tabular-nums;
    }}
    .grid-2 {{
      display: grid;
      grid-template-columns: minmax(0, 1.2fr) minmax(280px, 0.8fr);
      gap: 18px;
      align-items: start;
    }}
    .panel {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 8px;
      box-shadow: var(--shadow);
      overflow: hidden;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      table-layout: fixed;
      font-size: 14px;
    }}
    th, td {{
      border-bottom: 1px solid var(--border);
      padding: 10px 12px;
      vertical-align: middle;
      text-align: center;
      overflow-wrap: anywhere;
    }}
    th {{
      background: var(--blue-soft);
      color: #173e63;
      font-weight: 700;
      position: sticky;
      top: 0;
      z-index: 1;
    }}
    tbody tr:nth-child(even) {{ background: #fbfcfe; }}
    tbody tr:hover {{ background: var(--gold-soft); }}
    .notes {{
      margin: 0;
      padding: 14px 18px 14px 34px;
      color: var(--muted);
      font-size: 14px;
    }}
    .notes li + li {{ margin-top: 8px; }}
    .filters {{
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 10px;
      flex-wrap: wrap;
      margin-bottom: 14px;
    }}
    select {{
      min-width: 150px;
      min-height: 36px;
      border: 1px solid var(--border);
      border-radius: 6px;
      background: var(--surface);
      color: var(--text);
      padding: 6px 9px;
      font-size: 14px;
    }}
    .items {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
    }}
    .item {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-left: 5px solid var(--blue);
      border-radius: 8px;
      padding: 14px;
      box-shadow: var(--shadow);
      min-height: 170px;
      display: flex;
      flex-direction: column;
      gap: 9px;
    }}
    .item.risk {{ border-left-color: var(--rose); }}
    .item.positive {{ border-left-color: var(--green); }}
    .item.neutral {{ border-left-color: var(--blue); }}
    .item.empty {{
      grid-column: 1 / -1;
      min-height: 80px;
      text-align: center;
      justify-content: center;
      color: var(--muted);
    }}
    .item h3 {{
      margin: 0;
      font-size: 16px;
      line-height: 1.42;
      letter-spacing: 0;
    }}
    .meta {{
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
      color: var(--muted);
      font-size: 12px;
    }}
    .snippet {{
      margin: 0;
      color: #334150;
      font-size: 14px;
      flex: 1;
    }}
    .zh-summary {{
      margin: 0;
      padding: 9px 10px;
      border-radius: 6px;
      background: #fbfcfe;
      border: 1px solid var(--border);
      color: #253243;
      font-size: 14px;
    }}
    .zh-summary strong {{
      color: var(--purple);
    }}
    .badge {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      border-radius: 999px;
      padding: 3px 8px;
      font-size: 12px;
      line-height: 1.3;
      border: 1px solid transparent;
    }}
    .badge.entity {{
      background: var(--purple-soft);
      color: var(--purple);
      border-color: #d9d0f2;
    }}
    .badge.sentiment-risk {{
      background: var(--rose-soft);
      color: var(--rose);
      border-color: #efc8d2;
    }}
    .badge.sentiment-positive {{
      background: var(--green-soft);
      color: var(--green);
      border-color: #c7ead8;
    }}
    .badge.sentiment-neutral {{
      background: var(--blue-soft);
      color: var(--blue);
      border-color: #c8dff3;
    }}
    .status {{
      display: inline-block;
      min-width: 72px;
      border-radius: 999px;
      padding: 3px 9px;
      font-size: 12px;
    }}
    .status.ok {{
      background: var(--green-soft);
      color: var(--green);
    }}
    .status.warn {{
      background: var(--orange-soft);
      color: var(--orange);
    }}
    footer {{
      color: var(--muted);
      font-size: 12px;
      text-align: center;
      padding: 0 0 32px;
    }}
    @media (max-width: 860px) {{
      .metrics, .items, .grid-2 {{
        grid-template-columns: 1fr;
      }}
      h1 {{ font-size: 24px; }}
      th, td {{ padding: 9px 8px; font-size: 13px; }}
    }}
  </style>
</head>
<body>
  <header>
    <div class="wrap">
      <h1>{html_escape(report["title"])}</h1>
      <p class="subline">生成时间：{html_escape(format_time(generated, tz))}（北京时间） · 免费公开源 · 每日 08:00 自动更新</p>
      <div class="actions">
        <a class="button" href="{archive_path}" target="_blank" rel="noopener">latest.json</a>
        <a class="button" href="archive/{html_escape(payload["date"])}.json" target="_blank" rel="noopener">archive.json</a>
      </div>
    </div>
  </header>

  <main class="wrap">
    <section>
      <div class="metrics">
        <div class="metric"><span class="value">{len(items)}</span><span class="label">匹配条目</span></div>
        <div class="metric"><span class="value">{sum(1 for v in summary["entity_counts"].values() if v)}</span><span class="label">活跃主题</span></div>
        <div class="metric"><span class="value">{summary["risk_count"]}</span><span class="label">风险/监管信号</span></div>
        <div class="metric"><span class="value">{sum(1 for s in statuses if s["ok"])}</span><span class="label">可用来源</span></div>
      </div>
      <div class="chips">{platform_chips}</div>
    </section>

    <section class="grid-2">
      <div>
        <h2>核心叙事</h2>
        <div class="panel">
          <table>
            <thead><tr><th style="width: 20%">主题</th><th style="width: 16%">数量</th><th>代表链接</th></tr></thead>
            <tbody>{narrative_rows}</tbody>
          </table>
        </div>
      </div>
      <div>
        <h2>覆盖说明</h2>
        <div class="panel"><ul class="notes">{coverage_notes}</ul></div>
      </div>
    </section>

    <section>
      <h2>重点发言与文章</h2>
      <div class="filters">
        <select id="entityFilter" aria-label="主题筛选"><option value="">全部主题</option>{entity_options(config)}</select>
        <select id="sentimentFilter" aria-label="信号筛选">
          <option value="">全部信号</option>
          <option value="risk">风险/监管</option>
          <option value="positive">积极进展</option>
          <option value="neutral">中性</option>
        </select>
        <select id="platformFilter" aria-label="来源筛选"><option value="">全部来源</option>{platform_options(items)}</select>
      </div>
      <div class="items" id="items">{item_cards}</div>
    </section>

    <section>
      <h2>来源状态</h2>
      <div class="panel">
        <table>
          <thead><tr><th style="width: 20%">来源</th><th style="width: 14%">状态</th><th style="width: 12%">抓取</th><th style="width: 12%">保留</th><th>备注</th></tr></thead>
          <tbody>{source_rows}</tbody>
        </table>
      </div>
    </section>
  </main>

  <footer>Generated by free public sources. Direct X/Twitter API monitoring is intentionally excluded unless free official access is configured.</footer>

  <script>
    const entityFilter = document.getElementById('entityFilter');
    const sentimentFilter = document.getElementById('sentimentFilter');
    const platformFilter = document.getElementById('platformFilter');
    const cards = Array.from(document.querySelectorAll('.item[data-entities]'));
    function applyFilters() {{
      const entity = entityFilter.value;
      const sentiment = sentimentFilter.value;
      const platform = platformFilter.value;
      cards.forEach(card => {{
        const hasEntity = !entity || card.dataset.entities.split('|').includes(entity);
        const hasSentiment = !sentiment || card.dataset.sentiment === sentiment;
        const hasPlatform = !platform || card.dataset.platform === platform;
        card.style.display = hasEntity && hasSentiment && hasPlatform ? '' : 'none';
      }});
    }}
    [entityFilter, sentimentFilter, platformFilter].forEach(el => el.addEventListener('change', applyFilters));
  </script>
</body>
</html>
"""


def entity_options(config: dict[str, Any]) -> str:
    return "".join(f'<option value="{html_escape(entity)}">{html_escape(entity)}</option>' for entity in config["entities"])


def platform_options(items: list[dict[str, Any]]) -> str:
    names = sorted({item["platform"] for item in items})
    return "".join(f'<option value="{html_escape(name)}">{html_escape(name)}</option>' for name in names)


TRANSLATION_GLOSSARY = [
    (r"\bstablecoins?\b", "稳定币"),
    (r"\bstable coin(s)?\b", "稳定币"),
    (r"\bCircle\b", "Circle"),
    (r"\bUSDC\b", "USDC"),
    (r"\bTether\b", "Tether"),
    (r"\bUSDT\b", "USDT"),
    (r"\bRobinhood\b", "Robinhood"),
    (r"\bcrypto\b", "加密货币"),
    (r"\bbitcoin\b", "比特币"),
    (r"\bethereum\b", "以太坊"),
    (r"\bmarket cap\b", "市值"),
    (r"\bdominance\b", "主导地位"),
    (r"\breserves?\b", "储备"),
    (r"\breserve attestation\b", "储备证明"),
    (r"\baudit(s|ed|ing)?\b", "审计"),
    (r"\battestation(s)?\b", "证明报告"),
    (r"\bregulation(s)?\b", "监管"),
    (r"\bregulated\b", "受监管"),
    (r"\blegislation\b", "立法"),
    (r"\bbill\b", "法案"),
    (r"\bSEC\b", "美国 SEC"),
    (r"\bCFTC\b", "美国 CFTC"),
    (r"\bDOJ\b", "美国 DOJ"),
    (r"\bMiCA\b", "欧盟 MiCA"),
    (r"\bsanction(s|ed)?\b", "制裁"),
    (r"\blawsuit(s)?\b", "诉讼"),
    (r"\binvestigation(s)?\b", "调查"),
    (r"\bdepeg(ging)?\b", "脱锚"),
    (r"\bredeem(s|ed|ing)?\b", "赎回"),
    (r"\bredemption(s)?\b", "赎回"),
    (r"\bliquidity\b", "流动性"),
    (r"\bfreeze(s|d|ing)?\b", "冻结"),
    (r"\bblacklist(s|ed|ing)?\b", "黑名单"),
    (r"\bhack(s|ed|ing)?\b", "黑客攻击"),
    (r"\bfraud\b", "欺诈"),
    (r"\bbankruptcy\b", "破产"),
    (r"\blaunch(es|ed|ing)?\b", "推出"),
    (r"\bpartnership(s)?\b", "合作"),
    (r"\bintegrat(e|es|ed|ion|ions)\b", "集成"),
    (r"\badoption\b", "采用"),
    (r"\bgrowth\b", "增长"),
    (r"\brevenue\b", "收入"),
    (r"\bprofit(s)?\b", "利润"),
    (r"\bapproval\b", "批准"),
    (r"\blicensed\b", "获牌照"),
    (r"\bexchange(s)?\b", "交易所"),
    (r"\bwallet(s)?\b", "钱包"),
    (r"\bDeFi\b", "DeFi"),
    (r"\busers?\b", "用户"),
    (r"\breport(s|ed)?\b", "报道"),
    (r"\bsays?\b", "表示"),
    (r"\bamid\b", "在……背景下"),
    (r"\bover\b", "关于"),
    (r"\bafter\b", "之后"),
    (r"\bbefore\b", "之前"),
]


def inline_chinese_summary(item: dict[str, Any]) -> str:
    text = clean_text(f"{item.get('title', '')}. {item.get('snippet', '')}", limit=240)
    if not text:
        return "暂无摘要。"

    translated = text
    for pattern, replacement in TRANSLATION_GLOSSARY:
        translated = re.sub(pattern, replacement, translated, flags=re.I)
    translated = re.sub(r"\s+", " ", translated).strip()

    entities = "、".join(item.get("entities", [])) or "相关主题"
    sentiment_label = {"risk": "风险/监管信号", "positive": "积极进展", "neutral": "中性动态"}.get(
        item.get("sentiment", "neutral"), "中性动态"
    )
    return f"{sentiment_label}：涉及 {entities}。{translated}"


def render_item_card(item: dict[str, Any], tz: ZoneInfo) -> str:
    sentiment = item["sentiment"]
    sentiment_label = {"risk": "风险/监管", "positive": "积极进展", "neutral": "中性"}.get(sentiment, sentiment)
    published = format_time(parse_date(item["published"]), tz)
    entities = item.get("entities", [])
    entity_data = "|".join(entities)
    snippet = item.get("snippet") or ""
    if len(snippet) > 260:
        snippet = snippet[:259].rstrip() + "..."
    zh_summary = inline_chinese_summary(item)
    return f"""
      <article class="item {html_escape(sentiment)}" data-entities="{html_escape(entity_data)}" data-sentiment="{html_escape(sentiment)}" data-platform="{html_escape(item["platform"])}">
        <div class="meta">
          <span>{html_escape(item["platform"])}</span>
          <span>{html_escape(item["source"])}</span>
          <span>{html_escape(published)}</span>
          <span class="badge sentiment-{html_escape(sentiment)}">{html_escape(sentiment_label)}</span>
        </div>
        <h3><a href="{html_escape(item["url"])}" target="_blank" rel="noopener">{html_escape(item["title"])}</a></h3>
        <p class="zh-summary"><strong>中文要点：</strong>{html_escape(zh_summary)}</p>
        <p class="snippet">{html_escape(snippet)}</p>
        <div class="meta">
          {render_badges(entities)}
          <span>score {html_escape(item["score"])}</span>
          {f'<span>@{html_escape(item["author"])}</span>' if item.get("author") else ''}
        </div>
      </article>
    """


def write_outputs(payload: dict[str, Any], config: dict[str, Any], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    data_dir = output_dir / "data"
    archive_dir = output_dir / "archive"
    data_dir.mkdir(exist_ok=True)
    archive_dir.mkdir(exist_ok=True)

    latest_json = json.dumps(payload, ensure_ascii=False, indent=2)
    (data_dir / "latest.json").write_text(latest_json + "\n", encoding="utf-8")
    (archive_dir / f"{payload['date']}.json").write_text(latest_json + "\n", encoding="utf-8")
    (output_dir / "index.html").write_text(render_html(payload, config), encoding="utf-8")


def main() -> int:
    global CONFIG_PATH

    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="docs", help="Static output directory.")
    parser.add_argument("--config", default=str(CONFIG_PATH), help="Config JSON path.")
    parser.add_argument("--offline", action="store_true", help="Render an empty report without network fetching.")
    args = parser.parse_args()

    CONFIG_PATH = Path(args.config).resolve()
    config = load_config()
    tz = ZoneInfo(config["report"].get("timezone", "Asia/Shanghai"))
    generated = utcnow()
    lookback_start = generated - dt.timedelta(hours=int(config["report"].get("lookback_hours", 36)))

    if args.offline:
        raw_items: list[Item] = []
        statuses = [SourceStatus("offline", False, message="offline render")]
    else:
        raw_items, statuses = collect(config)

    patterns = compile_entity_patterns(config)
    for item in raw_items:
        classify_item(item, config, patterns)

    kept_items = [item for item in raw_items if relevant(item, lookback_start)]
    kept_items = dedupe(kept_items)
    kept_items.sort(key=lambda item: item.score, reverse=True)
    max_items = int(config["report"].get("max_items", 120))
    kept_items = kept_items[:max_items]

    kept_by_source: dict[str, int] = {}
    for item in kept_items:
        kept_by_source[item.source] = kept_by_source.get(item.source, 0) + 1
    for status in statuses:
        status.kept = kept_by_source.get(status.name, 0) + sum(
            count for name, count in kept_by_source.items() if name.startswith(f"{status.name}:")
        )

    payload = {
        "date": generated.astimezone(tz).date().isoformat(),
        "generated_at": generated.isoformat(),
        "timezone": config["report"].get("timezone", "Asia/Shanghai"),
        "items": [item.as_dict() for item in kept_items],
        "summary": build_summary(kept_items, config),
        "sources": [status.__dict__ for status in statuses],
        "coverage_notes": config.get("coverage_notes", []),
    }

    write_outputs(payload, config, Path(args.output))
    print(f"Generated {Path(args.output).resolve()} with {len(kept_items)} items from {len(statuses)} sources.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
