#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import datetime as dt
import email.utils
import html
import json
import mimetypes
import os
import re
import shutil
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import x_substack_bridge as bridge


ROOT = Path(__file__).resolve().parent
UI_DIR = ROOT / "ui"
DATA_DIR = ROOT / "data"
CONTENT_DIR = ROOT / "content" / "articles"
DIST_DIR = ROOT / "dist"
CURRENT_JSON = DATA_DIR / "current_draft.json"
PUBLISH_PAYLOAD_JSON = DATA_DIR / "publish_payload.json"
PUBLISH_RESULT_JSON = DATA_DIR / "publish_result.json"
X_EXTRACT_RESULT_JSON = DATA_DIR / "x_extract_result.json"
DRAFT_PIPELINE_JSON = DATA_DIR / "draft_pipeline.json"
SYNC_STATE_JSON = DATA_DIR / "sync_state.json"
ENV_FILE = ROOT / ".env"
DRAFT_LOCK = threading.RLock()
SYNC_LOCK = threading.Lock()


UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123 Safari/537.36"
)


try:
    import certifi  # type: ignore
except ModuleNotFoundError:  # pragma: no cover
    certifi = None  # type: ignore[assignment]


def load_env_file() -> None:
    if not ENV_FILE.exists():
        return
    for raw_line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_env_file()


def ssl_context() -> ssl.SSLContext:
    if certifi is not None:
        return ssl.create_default_context(cafile=certifi.where())
    return ssl.create_default_context()


class MetaParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.meta: Dict[str, str] = {}
        self.title_parts: List[str] = []
        self.in_title = False

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        attr = {k.lower(): v or "" for k, v in attrs}
        if tag.lower() == "title":
            self.in_title = True
        if tag.lower() == "meta":
            key = attr.get("property") or attr.get("name")
            value = attr.get("content")
            if key and value:
                self.meta[key.lower()] = value.strip()

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self.in_title = False

    def handle_data(self, data: str) -> None:
        if self.in_title and data.strip():
            self.title_parts.append(data.strip())

    @property
    def title(self) -> str:
        return " ".join(self.title_parts).strip()


class TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: List[str] = []

    def handle_data(self, data: str) -> None:
        cleaned = data.strip()
        if cleaned:
            self.parts.append(cleaned)

    @property
    def text(self) -> str:
        return " ".join(self.parts).strip()


def json_bytes(payload: Any, status: int = 200) -> Tuple[int, bytes, str]:
    return status, json.dumps(payload, indent=2).encode("utf-8"), "application/json"


def read_json_body(handler: BaseHTTPRequestHandler) -> Dict[str, Any]:
    length = int(handler.headers.get("content-length", "0") or "0")
    if length <= 0:
        return {}
    return json.loads(handler.rfile.read(length).decode("utf-8"))


def request_json(url: str, headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    req = urllib.request.Request(url, headers=headers or {"user-agent": UA})
    with urllib.request.urlopen(req, timeout=25, context=ssl_context()) as response:
        return json.loads(response.read().decode("utf-8"))


def request_text(url: str) -> str:
    req = urllib.request.Request(url, headers={"user-agent": UA})
    with urllib.request.urlopen(req, timeout=20, context=ssl_context()) as response:
        return response.read().decode("utf-8", errors="replace")


def normalize_x_url(url: str) -> str:
    url = url.strip()
    if not url:
        raise ValueError("Paste an X article URL first.")
    parsed = urllib.parse.urlparse(url)
    if not parsed.scheme:
        url = "https://" + url
        parsed = urllib.parse.urlparse(url)
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    if host not in {"x.com", "twitter.com", "mobile.twitter.com"}:
        raise ValueError("That does not look like an X/Twitter URL.")
    return url.split("#", 1)[0]


def extract_x_id(url: str) -> Optional[str]:
    for pattern in (r"/status(?:es)?/(\d+)", r"/i/article/(\d+)", r"/articles?/(\d+)"):
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    numbers = re.findall(r"\d{12,}", url)
    return numbers[-1] if numbers else None


def clean_text(text: str) -> str:
    text = html.unescape(text or "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"https://t\.co/\S+", "", text)
    text = re.sub(r"\s+pic\.twitter\.com/\S+", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def infer_title(text: str, fallback: str = "Untitled X article") -> str:
    for line in text.splitlines():
        line = line.strip(" -:\t")
        if len(line) >= 8:
            return line[:120]
    return fallback


def word_count(text: str) -> int:
    return len(re.findall(r"\b[\w'-]+\b", text or ""))


def is_byline_only(text: str) -> bool:
    cleaned = clean_text(text)
    return bool(
        re.fullmatch(
            r"[\u2013\u2014-]?\s*[\w .'-]+\s+\(@[\w_]+\)\s+[A-Za-z]+\s+\d{1,2},\s+\d{4}",
            cleaned,
        )
    )


def is_login_wall(text: str) -> bool:
    lowered = clean_text(text).lower()
    markers = [
        "continue with phone",
        "email or username",
        "by continuing, you agree",
        "sign in to x",
        "log in to x",
    ]
    return any(marker in lowered for marker in markers)


def is_draft_usable(draft: Dict[str, Any]) -> bool:
    body = clean_text(str(draft.get("body") or ""))
    media = list(draft.get("media") or [])
    if is_login_wall(body):
        return False
    if is_byline_only(body):
        return False
    if word_count(body) >= 35:
        return True
    return bool(media) and word_count(body) >= 12


def low_value_reason(source: str) -> str:
    if source == "public_x_embed":
        return "Public X only returned the embed/byline, not the article body."
    if source == "html_meta":
        return "X only exposed page metadata, not the article body."
    if source == "x_logged_in_browser":
        return "X showed a login wall instead of the article body."
    return "Extractor did not return enough article body to build a Substack draft."


def first_str(obj: Dict[str, Any], keys: List[str]) -> str:
    for key in keys:
        value = obj.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def walk(obj: Any) -> List[Any]:
    out: List[Any] = []
    stack = [obj]
    while stack:
        cur = stack.pop()
        out.append(cur)
        if isinstance(cur, dict):
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    return out


def extract_media(payload: Dict[str, Any]) -> List[Dict[str, str]]:
    media: List[Dict[str, str]] = []
    seen: set[str] = set()
    includes = payload.get("includes") if isinstance(payload.get("includes"), dict) else {}
    for item in includes.get("media", []) if isinstance(includes, dict) else []:
        if not isinstance(item, dict):
            continue
        url = first_str(item, ["url", "preview_image_url"])
        if not url or url in seen:
            continue
        seen.add(url)
        media.append(
            {
                "type": str(item.get("type") or "image"),
                "url": url,
                "alt": first_str(item, ["alt_text"]) or "X media",
                "width": str(item.get("width") or ""),
                "height": str(item.get("height") or ""),
            }
        )

    for node in walk(payload.get("data", {})):
        if not isinstance(node, dict):
            continue
        for key in ("url", "preview_image_url"):
            url = node.get(key)
            if isinstance(url, str) and re.search(r"\.(png|jpe?g|webp)(\?|$)", url, re.I) and url not in seen:
                seen.add(url)
                media.append({"type": "image", "url": url, "alt": "X media", "width": "", "height": ""})
    return media


def draft_from_x_api(url: str) -> Optional[Dict[str, Any]]:
    token = os.environ.get("X_BEARER_TOKEN")
    x_id = extract_x_id(url)
    if not token or not x_id:
        return None
    params = {
        "tweet.fields": "article,attachments,created_at,entities,note_tweet,text",
        "expansions": "article.cover_media,article.media_entities,attachments.media_keys,author_id",
        "media.fields": "alt_text,preview_image_url,type,url,width,height",
    }
    api_url = f"https://api.x.com/2/tweets/{urllib.parse.quote(x_id)}?{urllib.parse.urlencode(params)}"
    payload = request_json(api_url, {"authorization": f"Bearer {token}", "user-agent": UA})
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    article = data.get("article") if isinstance(data.get("article"), dict) else {}
    note = data.get("note_tweet") if isinstance(data.get("note_tweet"), dict) else {}

    body = first_str(article, ["text", "body", "content", "preview_text"])
    if not body:
        body = first_str(note, ["text"])
    if not body:
        body = first_str(data, ["text"])
    body = clean_text(body)

    title = first_str(article, ["title", "name"]) or infer_title(body)
    subtitle = first_str(article, ["subtitle", "dek", "description", "preview_text"])
    created = first_str(data, ["created_at"])
    date = created[:10] if re.match(r"\d{4}-\d{2}-\d{2}", created) else dt.date.today().isoformat()
    return {
        "url": url,
        "title": title,
        "subtitle": subtitle,
        "date": date,
        "body": body,
        "media": extract_media(payload),
        "source": "x_api",
        "warnings": [],
    }


def draft_from_oembed(url: str) -> Optional[Dict[str, Any]]:
    endpoint = "https://publish.twitter.com/oembed?" + urllib.parse.urlencode(
        {"url": url, "omit_script": "1", "dnt": "1"}
    )
    try:
        payload = request_json(endpoint)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return None
    parser = TextParser()
    parser.feed(str(payload.get("html") or ""))
    body = clean_text(parser.text)
    if not body:
        return None
    draft = {
        "url": url,
        "title": infer_title(body),
        "subtitle": "",
        "date": dt.date.today().isoformat(),
        "body": body,
        "media": [],
        "source": "public_x_embed",
        "warnings": [
            "Public X embed returned text only. Add X_BEARER_TOKEN to preserve long-form article fields and media."
        ],
    }
    if not is_draft_usable(draft):
        raise ValueError(low_value_reason("public_x_embed"))
    return draft


def draft_from_meta(url: str) -> Optional[Dict[str, Any]]:
    try:
        raw = request_text(url)
    except (urllib.error.URLError, TimeoutError):
        return None
    parser = MetaParser()
    parser.feed(raw)
    meta = parser.meta
    title = meta.get("og:title") or meta.get("twitter:title") or parser.title
    description = meta.get("og:description") or meta.get("twitter:description") or ""
    image = meta.get("og:image") or meta.get("twitter:image") or ""
    if not title and not description:
        return None
    media = [{"type": "image", "url": image, "alt": title or "X media", "width": "", "height": ""}] if image else []
    draft = {
        "url": url,
        "title": clean_text(title) or "Untitled X article",
        "subtitle": "",
        "date": dt.date.today().isoformat(),
        "body": clean_text(description),
        "media": media,
        "source": "html_meta",
        "warnings": [
            "X did not expose full article text publicly. Add X_BEARER_TOKEN or use a logged-in browser flow for exact full article capture."
        ],
    }
    if not is_draft_usable(draft):
        raise ValueError(low_value_reason("html_meta"))
    return draft


def run_node_extractor(script_name: str, url: str, timeout: int = 180) -> Dict[str, Any]:
    script = ROOT / script_name
    if not script.exists():
        return {}
    env = os.environ.copy()
    try:
        proc = subprocess.run(
            ["node", str(script), url],
            cwd=str(ROOT),
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise ValueError(f"Node.js is required for X extraction: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ValueError("X extraction timed out. Check the X/Chrome window and try again.") from exc

    raw = proc.stdout.strip()
    try:
        result = json.loads(raw.splitlines()[-1]) if raw else {}
    except json.JSONDecodeError as exc:
        raise ValueError(f"X extraction returned unreadable output: {proc.stderr.strip()}") from exc
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    X_EXTRACT_RESULT_JSON.write_text(json.dumps(result, indent=2), encoding="utf-8")
    if proc.returncode != 0 or not result.get("ok"):
        message = str(result.get("message") or proc.stderr.strip() or "X extraction failed.")
        raise ValueError(message)
    return result


def draft_from_existing_chrome(url: str) -> Optional[Dict[str, Any]]:
    if sys.platform != "darwin" or os.environ.get("X_SKIP_EXISTING_CHROME") == "1":
        return None
    result = run_node_extractor("extract_x_chrome_tab.mjs", url, timeout=75)
    draft = result.get("draft")
    if not isinstance(draft, dict):
        return None
    if not is_draft_usable(draft):
        raise ValueError(low_value_reason(str(draft.get("source") or "x_browser")))
    return draft


def draft_from_logged_in_browser(url: str) -> Optional[Dict[str, Any]]:
    if os.environ.get("X_ALLOW_ISOLATED_X_PROFILE") != "1" or os.environ.get("X_SKIP_BROWSER_EXTRACT") == "1":
        return None
    result = run_node_extractor("extract_x_article.mjs", url, timeout=180)
    draft = result.get("draft")
    if not isinstance(draft, dict):
        return None
    if not is_draft_usable(draft):
        raise ValueError(low_value_reason(str(draft.get("source") or "x_browser")))
    return draft


def build_draft(url: str) -> Dict[str, Any]:
    normalized = normalize_x_url(url)
    errors: List[str] = []
    for builder in (draft_from_x_api, draft_from_existing_chrome, draft_from_logged_in_browser, draft_from_oembed, draft_from_meta):
        try:
            draft = builder(normalized)
        except Exception as exc:
            errors.append(f"{builder.__name__}: {exc}")
            draft = None
        if draft and (draft.get("body") or draft.get("media")) and is_draft_usable(draft):
            draft["warnings"] = list(draft.get("warnings") or []) + errors
            return draft
    detail = " ".join(errors[-3:])
    raise ValueError(
        "Could not capture a real article body from that X URL. Bring your logged-in Chrome profile window to the front, "
        "then paste the link again; or set X_BEARER_TOKEN before starting the server."
        + (f" Details: {detail}" if detail else "")
    )


def media_html(media: List[Dict[str, str]]) -> str:
    chunks: List[str] = []
    for item in media:
        url = html.escape(item.get("url", ""), quote=True)
        alt = html.escape(item.get("alt", "") or "X media")
        if not url:
            continue
        if item.get("type") == "video":
            chunks.append(f'<figure><a href="{url}"><img src="{url}" alt="{alt}"></a><figcaption>Video media from X</figcaption></figure>')
        else:
            chunks.append(f'<figure><img src="{url}" alt="{alt}"></figure>')
    return "\n".join(chunks)


def render_article_html(draft: Dict[str, Any]) -> str:
    body_html = bridge.markdown_to_html(str(draft.get("body") or ""))
    title = html.escape(str(draft.get("title") or "Untitled X article"))
    subtitle = html.escape(str(draft.get("subtitle") or ""))
    source = html.escape(str(draft.get("url") or ""), quote=True)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <link rel="stylesheet" href="/style.css">
</head>
<body>
  <main class="article-page">
    <article class="article">
      <p class="source">Imported from <a href="{source}" target="_blank" rel="noopener">X</a></p>
      <h1>{title}</h1>
      {f'<p class="subtitle">{subtitle}</p>' if subtitle else ''}
      <div class="body">{body_html}</div>
      <div class="media">{media_html(list(draft.get("media") or []))}</div>
    </article>
  </main>
</body>
</html>
"""


def render_feed(draft: Dict[str, Any], base_url: str) -> str:
    ET.register_namespace("content", "http://purl.org/rss/1.0/modules/content/")
    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = "Manvinder Arora"
    ET.SubElement(channel, "link").text = base_url.rstrip("/") + "/draft.html"
    ET.SubElement(channel, "description").text = "Single X article converted into a Substack-ready draft."
    ET.SubElement(channel, "lastBuildDate").text = email.utils.format_datetime(dt.datetime.now(dt.timezone.utc), usegmt=True)

    item = ET.SubElement(channel, "item")
    url = base_url.rstrip("/") + "/draft.html"
    date = bridge.parse_date(str(draft.get("date") or dt.date.today().isoformat()))
    ET.SubElement(item, "title").text = str(draft.get("title") or "Untitled X article")
    ET.SubElement(item, "link").text = url
    ET.SubElement(item, "guid", {"isPermaLink": "true"}).text = url
    ET.SubElement(item, "pubDate").text = email.utils.format_datetime(date, usegmt=True)
    ET.SubElement(item, "description").text = str(draft.get("subtitle") or "")
    content = ET.SubElement(item, "{http://purl.org/rss/1.0/modules/content/}encoded")
    content.text = bridge.markdown_to_html(str(draft.get("body") or "")) + media_html(list(draft.get("media") or []))
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(rss, encoding="unicode")


def write_style() -> None:
    (DIST_DIR / "style.css").write_text(
        """
:root { color-scheme: light; --ink: #17191f; --muted: #626a76; --paper: #f7f7f2; --panel: #fff; --line: #d9dee5; --accent: #0e5d63; }
* { box-sizing: border-box; }
body { margin: 0; font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color: var(--ink); background: var(--paper); line-height: 1.62; }
a { color: var(--accent); }
.article-page { width: min(820px, calc(100% - 28px)); margin: 0 auto; padding: 42px 0 70px; }
.article { border: 1px solid var(--line); border-radius: 8px; background: var(--panel); padding: 30px; }
.source, .subtitle { color: var(--muted); }
h1 { margin: 0 0 14px; font-size: clamp(2.2rem, 8vw, 4.7rem); line-height: .94; letter-spacing: 0; }
.subtitle { margin: 0 0 28px; font-size: 1.1rem; }
.body { font-size: 1.06rem; }
figure { margin: 28px 0 0; }
img { max-width: 100%; border-radius: 8px; border: 1px solid var(--line); display: block; }
figcaption { color: var(--muted); font-size: .9rem; margin-top: 7px; }
@media (max-width: 640px) { .article { padding: 20px; } h1 { font-size: clamp(2rem, 13vw, 3.6rem); } }
""".strip()
        + "\n",
        encoding="utf-8",
    )


def persist_draft(draft: Dict[str, Any], base_url: str) -> Dict[str, Any]:
    if not is_draft_usable(draft):
        raise ValueError("Refusing to save an empty/byline-only draft. Capture the full X article first.")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CONTENT_DIR.mkdir(parents=True, exist_ok=True)
    DIST_DIR.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(DIST_DIR / "posts", ignore_errors=True)

    slug = bridge.slugify(str(draft.get("title") or "x-article"), "x-article")
    date = str(draft.get("date") or dt.date.today().isoformat())
    md_path = CONTENT_DIR / f"{date}-{slug}.md"
    tags = "x, substack"
    markdown = str(draft.get("body") or "").strip()
    md_path.write_text(
        "\n".join(
            [
                "---",
                f"title: {draft.get('title', '')}",
                f"subtitle: {draft.get('subtitle', '')}",
                f"date: {date}",
                "status: draft",
                f"tags: {tags}",
                f"original_url: {draft.get('url', '')}",
                "---",
                "",
                markdown,
                "",
            ]
        ),
        encoding="utf-8",
    )

    draft["markdown_path"] = str(md_path)
    draft["draft_url"] = base_url.rstrip("/") + "/draft.html"
    draft["feed_url"] = base_url.rstrip("/") + "/feed.xml"
    CURRENT_JSON.write_text(json.dumps(draft, indent=2), encoding="utf-8")

    write_style()
    (DIST_DIR / "draft.html").write_text(render_article_html(draft), encoding="utf-8")
    (DIST_DIR / "feed.xml").write_text(render_feed(draft, base_url), encoding="utf-8")
    with (DIST_DIR / "substack-import.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["title", "subtitle", "date", "original_url", "html_file"])
        writer.writeheader()
        writer.writerow(
            {
                "title": draft.get("title", ""),
                "subtitle": draft.get("subtitle", ""),
                "date": date,
                "original_url": draft.get("url", ""),
                "html_file": draft["draft_url"],
            }
        )
    return draft


def current_draft() -> Optional[Dict[str, Any]]:
    if not CURRENT_JSON.exists():
        return None
    draft = json.loads(CURRENT_JSON.read_text(encoding="utf-8"))
    return draft if is_draft_usable(draft) else None


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def pipeline_draft_id(draft: Dict[str, Any]) -> str:
    x_id = extract_x_id(str(draft.get("url") or ""))
    if x_id:
        return x_id
    return bridge.slugify(
        f"{draft.get('date') or dt.date.today().isoformat()}-{draft.get('title') or 'draft'}",
        "draft",
    )


def empty_pipeline() -> Dict[str, Any]:
    return {"selected_id": None, "items": []}


def read_pipeline() -> Dict[str, Any]:
    with DRAFT_LOCK:
        if not DRAFT_PIPELINE_JSON.exists():
            return empty_pipeline()
        try:
            pipeline = json.loads(DRAFT_PIPELINE_JSON.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return empty_pipeline()
        if not isinstance(pipeline.get("items"), list):
            pipeline["items"] = []
        return pipeline


def write_pipeline(pipeline: Dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DRAFT_PIPELINE_JSON.write_text(json.dumps(pipeline, indent=2), encoding="utf-8")


def ensure_pipeline() -> Dict[str, Any]:
    with DRAFT_LOCK:
        pipeline = read_pipeline()
        if pipeline["items"]:
            return pipeline
        draft = current_draft()
        if not draft:
            return pipeline
        timestamp = now_iso()
        item = dict(draft)
        item.update(
            {
                "id": pipeline_draft_id(draft),
                "status": "draft",
                "discovered_at": timestamp,
                "updated_at": timestamp,
                "published_at": None,
            }
        )
        pipeline = {"selected_id": item["id"], "items": [item]}
        write_pipeline(pipeline)
        return pipeline


def upsert_pipeline_draft(
    draft: Dict[str, Any],
    base_url: str,
    *,
    select: bool,
) -> Dict[str, Any]:
    with DRAFT_LOCK:
        pipeline = ensure_pipeline()
        draft = dict(draft)
        draft_id = pipeline_draft_id(draft)
        existing = next((item for item in pipeline["items"] if str(item.get("id")) == draft_id), None)
        timestamp = now_iso()
        draft.update(
            {
                "id": draft_id,
                "status": str((existing or {}).get("status") or "draft"),
                "discovered_at": str((existing or {}).get("discovered_at") or timestamp),
                "updated_at": timestamp,
                "published_at": (existing or {}).get("published_at"),
            }
        )
        if select:
            draft = persist_draft(draft, base_url)
        if existing is None:
            pipeline["items"].append(draft)
        else:
            pipeline["items"][pipeline["items"].index(existing)] = draft
        if select:
            pipeline["selected_id"] = draft_id
        write_pipeline(pipeline)
        return draft


def select_pipeline_draft(draft_id: str, base_url: str) -> Dict[str, Any]:
    with DRAFT_LOCK:
        pipeline = ensure_pipeline()
        draft = next((item for item in pipeline["items"] if str(item.get("id")) == draft_id), None)
        if not isinstance(draft, dict):
            raise ValueError("Draft was not found in the pipeline.")
        draft = persist_draft(dict(draft), base_url)
        pipeline["items"] = [draft if str(item.get("id")) == draft_id else item for item in pipeline["items"]]
        pipeline["selected_id"] = draft_id
        write_pipeline(pipeline)
        return draft


def mark_selected_published() -> None:
    with DRAFT_LOCK:
        pipeline = ensure_pipeline()
        selected_id = str(pipeline.get("selected_id") or "")
        timestamp = now_iso()
        for item in pipeline["items"]:
            if str(item.get("id")) == selected_id:
                item["status"] = "published"
                item["published_at"] = timestamp
                item["updated_at"] = timestamp
                break
        write_pipeline(pipeline)


def sync_state() -> Dict[str, Any]:
    if not SYNC_STATE_JSON.exists():
        return {"status": "idle", "last_sync": None, "last_error": None}
    try:
        return json.loads(SYNC_STATE_JSON.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"status": "idle", "last_sync": None, "last_error": None}


def write_sync_state(state: Dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SYNC_STATE_JSON.write_text(json.dumps(state, indent=2), encoding="utf-8")


def normalized_title(value: Any) -> str:
    title = html.unescape(str(value or "")).lower()
    title = title.translate(str.maketrans({"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u2013": "-", "\u2014": "-"}))
    return re.sub(r"[^a-z0-9]+", " ", title).strip()


def substack_feed_url() -> str:
    configured = os.environ.get("SUBSTACK_FEED_URL") or ""
    if configured:
        return configured
    editor_url = os.environ.get("SUBSTACK_EDITOR_URL") or ""
    parsed = urllib.parse.urlparse(editor_url)
    return f"{parsed.scheme}://{parsed.netloc}/feed" if parsed.scheme and parsed.netloc else ""


def fetch_substack_publications() -> List[Dict[str, Any]]:
    feed_url = substack_feed_url()
    if not feed_url:
        raise ValueError("SUBSTACK_FEED_URL is not configured.")
    root = ET.fromstring(request_text(feed_url))
    publications: List[Dict[str, Any]] = []
    for item in root.findall("./channel/item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        published = (item.findtext("pubDate") or "").strip()
        if not title or not link:
            continue
        published_at: Optional[str] = None
        if published:
            try:
                published_at = email.utils.parsedate_to_datetime(published).isoformat()
            except (TypeError, ValueError):
                published_at = published
        publications.append({"title": title, "url": link, "published_at": published_at})
    return publications


def reconcile_substack_publications() -> Dict[str, Any]:
    publications = fetch_substack_publications()
    by_title = {normalized_title(post["title"]): post for post in publications if normalized_title(post["title"])}
    matched: List[str] = []
    changed = False
    with DRAFT_LOCK:
        pipeline = ensure_pipeline()
        for item in pipeline["items"]:
            post = by_title.get(normalized_title(item.get("title")))
            if not post:
                continue
            draft_id = str(item.get("id") or "")
            matched.append(draft_id)
            next_published_at = post.get("published_at") or item.get("published_at") or now_iso()
            if (
                item.get("status") != "published"
                or item.get("substack_url") != post["url"]
                or item.get("published_at") != next_published_at
            ):
                item["status"] = "published"
                item["substack_url"] = post["url"]
                item["published_at"] = next_published_at
                item["updated_at"] = now_iso()
                changed = True
        if changed:
            write_pipeline(pipeline)
    return {"feed_url": substack_feed_url(), "publication_count": len(publications), "matched": matched, "changed": changed}


def pipeline_payload() -> Dict[str, Any]:
    pipeline = ensure_pipeline()
    items = sorted(
        pipeline["items"],
        key=lambda item: (
            str(item.get("status") or "draft") == "published",
            -(int(str(item.get("id") or "0")) if str(item.get("id") or "").isdigit() else 0),
        ),
    )
    return {
        "selected_id": pipeline.get("selected_id"),
        "items": [
            {
                "id": item.get("id"),
                "title": item.get("title") or "Untitled X article",
                "subtitle": item.get("subtitle") or "",
                "url": item.get("url") or "",
                "date": item.get("date") or "",
                "status": item.get("status") or "draft",
                "word_count": word_count(str(item.get("body") or "")),
                "media_count": len(list(item.get("media") or [])),
                "discovered_at": item.get("discovered_at"),
                "updated_at": item.get("updated_at"),
                "published_at": item.get("published_at"),
                "substack_url": item.get("substack_url"),
            }
            for item in items
        ],
        "sync": sync_state(),
        "account": os.environ.get("X_ACCOUNT_HANDLE") or "@0xgoodie",
    }


def sync_x_articles(base_url: str) -> Dict[str, Any]:
    if not SYNC_LOCK.acquire(blocking=False):
        return {"ok": True, "status": "already_syncing", "pipeline": pipeline_payload()}
    started = now_iso()
    write_sync_state({"status": "syncing", "started_at": started, "last_sync": sync_state().get("last_sync"), "last_error": None})
    try:
        discovered: List[Dict[str, Any]] = []
        added: List[str] = []
        errors: List[str] = []
        x_ok = False
        substack_ok = False
        reconciliation: Dict[str, Any] = {}
        try:
            discovery = run_node_extractor("discover_x_articles.mjs", "", timeout=75)
            discovered = list(discovery.get("items") or [])
            x_ok = True
        except Exception as exc:
            errors.append(f"X sync: {exc}")
        existing_ids = {str(item.get("id")) for item in ensure_pipeline()["items"]}
        for found in discovered:
            draft_id = str(found.get("id") or "")
            url = str(found.get("url") or "")
            if not draft_id or not url or draft_id in existing_ids:
                continue
            try:
                draft = build_draft(url)
                if found.get("title") and len(str(found["title"]).strip()) >= 8:
                    draft["title"] = str(found["title"]).strip()[:240]
                upsert_pipeline_draft(draft, base_url, select=False)
                existing_ids.add(draft_id)
                added.append(draft_id)
            except Exception as exc:
                errors.append(f"{url}: {exc}")
        try:
            reconciliation = reconcile_substack_publications()
            substack_ok = True
        except Exception as exc:
            errors.append(f"Substack sync: {exc}")
        finished = now_iso()
        state = {
            "status": "ready" if not errors else ("partial" if x_ok or substack_ok else "error"),
            "started_at": started,
            "last_sync": finished,
            "last_error": " ".join(errors[-2:]) if errors else None,
            "discovered_count": len(discovered),
            "added_count": len(added),
            "substack_publication_count": reconciliation.get("publication_count", 0),
            "published_match_count": len(reconciliation.get("matched", [])),
            "substack_feed_url": reconciliation.get("feed_url") or substack_feed_url(),
        }
        write_sync_state(state)
        return {
            "ok": state["status"] != "error",
            "status": state["status"],
            "added": added,
            "published_matches": reconciliation.get("matched", []),
            "pipeline": pipeline_payload(),
        }
    except Exception as exc:
        state = {
            "status": "error",
            "started_at": started,
            "last_sync": now_iso(),
            "last_error": str(exc),
        }
        write_sync_state(state)
        return {"ok": False, "status": "error", "message": str(exc), "pipeline": pipeline_payload()}
    finally:
        SYNC_LOCK.release()


def auto_sync_loop(base_url: str) -> None:
    interval = max(300, int(os.environ.get("X_SYNC_INTERVAL_SECONDS") or "900"))
    time.sleep(20)
    while True:
        sync_x_articles(base_url)
        time.sleep(interval)


def publish_config() -> Dict[str, Any]:
    editor_url = os.environ.get("SUBSTACK_EDITOR_URL") or os.environ.get("SUBSTACK_PUBLISH_URL") or ""
    return {
        "editor_url": editor_url,
        "profile_dir": os.environ.get("SUBSTACK_PROFILE_DIR") or str(ROOT / ".substack-profile"),
        "browser": os.environ.get("SUBSTACK_BROWSER_APP") or "Google Chrome",
        "existing_chrome": os.environ.get("SUBSTACK_USE_PLAYWRIGHT") != "1",
        "direct_publish": os.environ.get("SUBSTACK_AUTOPUBLISH") == "1",
        "configured": bool(editor_url),
    }


def publish_to_substack(confirm_publish: bool, allow_publish: bool = False) -> Dict[str, Any]:
    draft = current_draft()
    if not draft:
        raise ValueError("Create a draft from an X article first.")
    config = publish_config()
    if not config["configured"]:
        return {
            "ok": False,
            "status": "setup_required",
            "message": "Set SUBSTACK_EDITOR_URL to your Substack new-post/editor URL, then restart this local server.",
            "config": config,
        }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PUBLISH_PAYLOAD_JSON.write_text(json.dumps(draft, indent=2), encoding="utf-8")
    env = os.environ.copy()
    env["SUBSTACK_CONFIRM_PUBLISH"] = "1" if confirm_publish else "0"
    env["SUBSTACK_AUTOPUBLISH"] = "1" if allow_publish else "0"
    script = ROOT / (
        "publish_to_substack.mjs"
        if os.environ.get("SUBSTACK_USE_PLAYWRIGHT") == "1"
        else "publish_to_substack_chrome.mjs"
    )
    try:
        proc = subprocess.run(
            ["node", str(script), str(PUBLISH_PAYLOAD_JSON)],
            cwd=str(ROOT),
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=180,
        )
    except FileNotFoundError as exc:
        return {
            "ok": False,
            "status": "setup_required",
            "message": "Node.js is required for the Substack publisher connector.",
            "detail": str(exc),
        }
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "status": "timeout",
            "message": "Substack publish automation timed out. Check login state and try again.",
        }
    raw = proc.stdout.strip()
    result: Dict[str, Any]
    try:
        result = json.loads(raw.splitlines()[-1]) if raw else {}
    except json.JSONDecodeError:
        result = {}
    if proc.returncode != 0:
        result = {
            "ok": False,
            "status": result.get("status") or "publish_failed",
            "message": result.get("message") or "Substack publisher connector failed.",
            "stdout": raw,
            "stderr": proc.stderr.strip(),
        }
    PUBLISH_RESULT_JSON.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


class Handler(BaseHTTPRequestHandler):
    server_version = "XSubstackOneShot/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} - {fmt % args}")

    @property
    def base_url(self) -> str:
        host = self.headers.get("host") or f"127.0.0.1:{self.server.server_port}"
        scheme = "https" if self.headers.get("x-forwarded-proto") == "https" else "http"
        return f"{scheme}://{host}"

    def send_blob(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(body)))
        self.send_header("cache-control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, payload: Any, status: int = 200) -> None:
        code, body, content_type = json_bytes(payload, status)
        self.send_blob(code, body, content_type)

    def serve_file(self, path: Path, content_type: Optional[str] = None) -> None:
        if not path.exists() or not path.is_file():
            self.send_json({"error": "Not found"}, 404)
            return
        self.send_blob(200, path.read_bytes(), content_type or mimetypes.guess_type(str(path))[0] or "application/octet-stream")

    def route_get(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        if path in {"/", "/index.html"}:
            self.serve_file(UI_DIR / "index.html", "text/html; charset=utf-8")
        elif path == "/app.js":
            self.serve_file(UI_DIR / "app.js", "text/javascript; charset=utf-8")
        elif path == "/styles.css":
            self.serve_file(UI_DIR / "styles.css", "text/css; charset=utf-8")
        elif path == "/api/current":
            last_publish = json.loads(PUBLISH_RESULT_JSON.read_text(encoding="utf-8")) if PUBLISH_RESULT_JSON.exists() else None
            self.send_json({"draft": current_draft(), "publish": publish_config(), "last_publish": last_publish})
        elif path == "/api/drafts":
            self.send_json(pipeline_payload())
        elif path == "/draft.html":
            self.serve_file(DIST_DIR / "draft.html", "text/html; charset=utf-8")
        elif path == "/feed.xml":
            self.serve_file(DIST_DIR / "feed.xml", "application/xml; charset=utf-8")
        elif path == "/substack-import.csv":
            self.serve_file(DIST_DIR / "substack-import.csv", "text/csv; charset=utf-8")
        elif path == "/style.css":
            self.serve_file(DIST_DIR / "style.css", "text/css; charset=utf-8")
        else:
            self.send_json({"error": "Not found"}, 404)

    def do_HEAD(self) -> None:
        self.route_get()

    def do_GET(self) -> None:
        self.route_get()

    def do_POST(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        try:
            payload = read_json_body(self)
            if path == "/api/ingest":
                draft = build_draft(str(payload.get("url") or ""))
                self.send_json({"ok": True, "draft": upsert_pipeline_draft(draft, self.base_url, select=True), "pipeline": pipeline_payload()})
            elif path == "/api/draft":
                draft = current_draft()
                if not draft:
                    raise ValueError("Create a draft from an X article first.")
                title = str(payload.get("title") or "").strip()
                body = clean_text(str(payload.get("body") or ""))
                if not title:
                    raise ValueError("Draft title cannot be empty.")
                if word_count(body) < 12:
                    raise ValueError("Draft body is too short to save.")
                draft.update(
                    {
                        "title": title[:240],
                        "subtitle": str(payload.get("subtitle") or "").strip()[:500],
                        "body": body,
                    }
                )
                self.send_json({"ok": True, "draft": upsert_pipeline_draft(draft, self.base_url, select=True), "pipeline": pipeline_payload()})
            elif path == "/api/drafts/select":
                draft = select_pipeline_draft(str(payload.get("id") or ""), self.base_url)
                self.send_json({"ok": True, "draft": draft, "pipeline": pipeline_payload()})
            elif path == "/api/drafts/sync":
                result = sync_x_articles(self.base_url)
                self.send_json(result, 200 if result.get("ok") else 502)
            elif path == "/api/publish":
                mode = str(payload.get("mode") or "review")
                if mode not in {"review", "publish"}:
                    raise ValueError("Unknown Substack action.")
                confirmed = bool(payload.get("confirm_publish"))
                if mode == "publish" and not confirmed:
                    raise ValueError("Live publishing requires explicit confirmation.")
                result = publish_to_substack(
                    confirm_publish=confirmed,
                    allow_publish=mode == "publish" and confirmed,
                )
                if result.get("ok") and result.get("status") == "published":
                    mark_selected_published()
                result["pipeline"] = pipeline_payload()
                self.send_json(result)
            else:
                self.send_json({"error": "Not found"}, 404)
        except ValueError as exc:
            self.send_json({"error": str(exc)}, 400)
        except Exception as exc:
            self.send_json({"error": str(exc)}, 500)


def ensure_empty_dist() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DIST_DIR.mkdir(parents=True, exist_ok=True)
    write_style()
    if not (DIST_DIR / "feed.xml").exists():
        (DIST_DIR / "feed.xml").write_text(
            '<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>Manvinder Arora</title><description>No draft yet</description></channel></rss>',
            encoding="utf-8",
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="One-shot X article to Substack draft tool")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8788)
    args = parser.parse_args()
    ensure_empty_dist()
    ensure_pipeline()
    watcher_host = "127.0.0.1" if args.host in {"0.0.0.0", "::"} else args.host
    watcher_base_url = f"http://{watcher_host}:{args.port}"
    if os.environ.get("X_AUTO_SYNC", "1") != "0":
        threading.Thread(target=auto_sync_loop, args=(watcher_base_url,), daemon=True).start()
    with ThreadingHTTPServer((args.host, args.port), Handler) as httpd:
        print(f"X article to Substack draft tool running on http://{args.host}:{args.port}/")
        httpd.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
