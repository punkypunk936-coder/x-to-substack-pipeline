#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import datetime as dt
import email.utils
import html
import json
import os
import re
import shutil
import sys
import textwrap
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
CONTENT_DIR = ROOT / "content" / "articles"
DIST_DIR = ROOT / "dist"
RAW_DIR = ROOT / "raw"


@dataclass
class Article:
    title: str
    subtitle: str
    date: dt.datetime
    status: str
    tags: List[str]
    original_url: str
    slug: str
    source_path: Path
    markdown: str

    @property
    def url_path(self) -> str:
        return f"/posts/{self.slug}.html"


def load_config() -> Dict[str, str]:
    if not CONFIG_PATH.exists():
        raise SystemExit(f"Missing config: {CONFIG_PATH}")
    data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    required = ["publication_title", "publication_description", "author_name", "site_url"]
    missing = [key for key in required if not data.get(key)]
    if missing:
        raise SystemExit(f"Missing config values: {', '.join(missing)}")
    return {str(k): str(v) for k, v in data.items()}


def slugify(text: str, fallback: str = "post") -> str:
    text = text.strip().lower()
    text = text.replace("\u2019", "")
    text = re.sub(r"['`]", "", text)
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    return text or fallback


def parse_date(value: str) -> dt.datetime:
    value = value.strip()
    if not value:
        return dt.datetime.now(dt.timezone.utc)
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        parsed = dt.datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc)
    except ValueError:
        pass
    for fmt in ("%a %b %d %H:%M:%S %z %Y", "%Y-%m-%d %H:%M:%S"):
        try:
            parsed = dt.datetime.strptime(value, fmt)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt.timezone.utc)
            return parsed.astimezone(dt.timezone.utc)
        except ValueError:
            continue
    raise ValueError(f"Unsupported date: {value}")


def split_frontmatter(raw: str, path: Path) -> Tuple[Dict[str, str], str]:
    if not raw.startswith("---\n"):
        raise ValueError(f"{path} is missing frontmatter")
    end = raw.find("\n---\n", 4)
    if end == -1:
        raise ValueError(f"{path} has unclosed frontmatter")
    front = raw[4:end]
    body = raw[end + 5 :].strip()
    meta: Dict[str, str] = {}
    for line in front.splitlines():
        if not line.strip() or line.strip().startswith("#"):
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        meta[key.strip()] = value.strip().strip('"')
    return meta, body


def load_articles(include_drafts: bool = False) -> List[Article]:
    articles: List[Article] = []
    for path in sorted(CONTENT_DIR.glob("*.md")):
        raw = path.read_text(encoding="utf-8")
        meta, body = split_frontmatter(raw, path)
        status = meta.get("status", "published").lower()
        if status == "draft" and not include_drafts:
            continue
        title = meta.get("title") or path.stem
        subtitle = meta.get("subtitle", "")
        date = parse_date(meta.get("date", ""))
        tags = [tag.strip() for tag in meta.get("tags", "").split(",") if tag.strip()]
        original_url = meta.get("original_url", "")
        date_prefix = date.strftime("%Y-%m-%d")
        slug = slugify(meta.get("slug", "") or re.sub(r"^\d{4}-\d{2}-\d{2}-", "", path.stem))
        if not slug.startswith(date_prefix):
            slug = f"{date_prefix}-{slug}"
        articles.append(
            Article(
                title=title,
                subtitle=subtitle,
                date=date,
                status=status,
                tags=tags,
                original_url=original_url,
                slug=slug,
                source_path=path,
                markdown=body,
            )
        )
    articles.sort(key=lambda item: item.date, reverse=True)
    return articles


def markdown_inline(text: str) -> str:
    escaped = html.escape(text)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"\*(.+?)\*", r"<em>\1</em>", escaped)
    escaped = re.sub(
        r"\[([^\]]+)\]\((https?://[^)]+)\)",
        lambda m: f'<a href="{html.escape(m.group(2), quote=True)}" target="_blank" rel="noopener">{m.group(1)}</a>',
        escaped,
    )
    return escaped


def markdown_to_html(markdown: str) -> str:
    lines = markdown.strip().splitlines()
    out: List[str] = []
    paragraph: List[str] = []
    list_items: List[str] = []
    quote_lines: List[str] = []

    def flush_paragraph() -> None:
        nonlocal paragraph
        if paragraph:
            out.append(f"<p>{markdown_inline(' '.join(paragraph))}</p>")
            paragraph = []

    def flush_list() -> None:
        nonlocal list_items
        if list_items:
            out.append("<ul>" + "".join(f"<li>{markdown_inline(item)}</li>" for item in list_items) + "</ul>")
            list_items = []

    def flush_quote() -> None:
        nonlocal quote_lines
        if quote_lines:
            out.append("<blockquote>" + "".join(f"<p>{markdown_inline(line)}</p>" for line in quote_lines) + "</blockquote>")
            quote_lines = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            flush_paragraph()
            flush_list()
            flush_quote()
            continue
        if stripped.startswith("### "):
            flush_paragraph()
            flush_list()
            flush_quote()
            out.append(f"<h3>{markdown_inline(stripped[4:])}</h3>")
        elif stripped.startswith("## "):
            flush_paragraph()
            flush_list()
            flush_quote()
            out.append(f"<h2>{markdown_inline(stripped[3:])}</h2>")
        elif stripped.startswith("# "):
            flush_paragraph()
            flush_list()
            flush_quote()
            out.append(f"<h1>{markdown_inline(stripped[2:])}</h1>")
        elif stripped.startswith("> "):
            flush_paragraph()
            flush_list()
            quote_lines.append(stripped[2:])
        elif stripped.startswith("- "):
            flush_paragraph()
            flush_quote()
            list_items.append(stripped[2:])
        else:
            flush_list()
            flush_quote()
            paragraph.append(stripped)

    flush_paragraph()
    flush_list()
    flush_quote()
    return "\n".join(out)


def absolute_url(base_url: str, path: str) -> str:
    return urllib.parse.urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))


def page_template(title: str, body: str, config: Dict[str, str]) -> str:
    return f"""<!doctype html>
<html lang="{html.escape(config.get('language', 'en'))}">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <link rel="stylesheet" href="/style.css">
</head>
<body>
  <main class="page">
    {body}
  </main>
</body>
</html>
"""


def write_style() -> None:
    (DIST_DIR / "style.css").write_text(
        """
:root {
  color-scheme: light;
  --ink: #16191f;
  --muted: #5f6672;
  --line: #d8dde5;
  --paper: #f7f8f5;
  --panel: #ffffff;
  --accent: #155e63;
  --accent-2: #b6462f;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  background: var(--paper);
  color: var(--ink);
  line-height: 1.65;
}
a { color: var(--accent); }
.page {
  width: min(900px, calc(100% - 32px));
  margin: 0 auto;
  padding: 56px 0 72px;
}
.masthead, .post {
  border: 1px solid var(--line);
  background: var(--panel);
  border-radius: 8px;
  padding: 28px;
}
.masthead h1, .post h1 {
  margin: 0 0 12px;
  font-size: clamp(2.2rem, 7vw, 4.6rem);
  line-height: 0.95;
  letter-spacing: 0;
}
.masthead p, .dek, .meta {
  color: var(--muted);
}
.article-list {
  display: grid;
  gap: 14px;
  margin-top: 22px;
}
.article-card {
  display: block;
  border: 1px solid var(--line);
  background: var(--panel);
  border-radius: 8px;
  padding: 20px;
  text-decoration: none;
  color: inherit;
}
.article-card h2 {
  margin: 0 0 8px;
  font-size: 1.25rem;
}
.article-card p {
  margin: 0;
  color: var(--muted);
}
.post {
  max-width: 760px;
  margin: 0 auto;
}
.post-body {
  margin-top: 28px;
  font-size: 1.08rem;
}
.post-body h2, .post-body h3 {
  line-height: 1.15;
  margin-top: 2rem;
}
blockquote {
  margin: 1.5rem 0;
  padding: 0.15rem 0 0.15rem 1rem;
  border-left: 4px solid var(--accent-2);
  color: #30343b;
}
.source {
  margin-top: 32px;
  padding-top: 18px;
  border-top: 1px solid var(--line);
}
@media (max-width: 640px) {
  .page { width: min(100% - 24px, 900px); padding-top: 28px; }
  .masthead, .post { padding: 20px; }
  .masthead h1, .post h1 { font-size: clamp(2rem, 12vw, 3.4rem); }
}
""".strip()
        + "\n",
        encoding="utf-8",
    )


def render_article(article: Article, config: Dict[str, str], base_url: str) -> str:
    body_html = markdown_to_html(article.markdown)
    tags = ", ".join(article.tags)
    source_link = ""
    if article.original_url:
        source_link = (
            f'<p class="source">Originally posted on '
            f'<a href="{html.escape(article.original_url, quote=True)}" target="_blank" rel="noopener">X</a>.</p>'
        )
    body = f"""
<article class="post">
  <p class="meta">{article.date.strftime('%B %d, %Y')}{' / ' + html.escape(tags) if tags else ''}</p>
  <h1>{html.escape(article.title)}</h1>
  {f'<p class="dek">{html.escape(article.subtitle)}</p>' if article.subtitle else ''}
  <div class="post-body">
    {body_html}
  </div>
  {source_link}
  <p><a href="/">Back to archive</a></p>
</article>
"""
    return page_template(article.title, body, config)


def render_index(articles: Sequence[Article], config: Dict[str, str], base_url: str) -> str:
    cards = []
    for article in articles:
        cards.append(
            f"""
<a class="article-card" href="{html.escape(article.url_path)}">
  <h2>{html.escape(article.title)}</h2>
  <p>{article.date.strftime('%B %d, %Y')}{' / ' + html.escape(article.subtitle) if article.subtitle else ''}</p>
</a>
""".strip()
        )
    feed_url = absolute_url(base_url, "/feed.xml")
    body = f"""
<section class="masthead">
  <p class="meta">Substack import archive</p>
  <h1>{html.escape(config['publication_title'])}</h1>
  <p>{html.escape(config['publication_description'])}</p>
  <p><a href="{html.escape(feed_url)}">RSS feed for Substack import</a></p>
</section>
<section class="article-list">
  {''.join(cards) if cards else '<p>No published posts yet. Generate with --include-drafts to preview drafts.</p>'}
</section>
"""
    return page_template(config["publication_title"], body, config)


def build_rss(articles: Sequence[Article], config: Dict[str, str], base_url: str) -> str:
    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = config["publication_title"]
    ET.SubElement(channel, "link").text = base_url.rstrip("/") + "/"
    ET.SubElement(channel, "description").text = config["publication_description"]
    ET.SubElement(channel, "language").text = config.get("language", "en")
    ET.SubElement(channel, "lastBuildDate").text = email.utils.format_datetime(
        dt.datetime.now(dt.timezone.utc), usegmt=True
    )
    for article in articles:
        item = ET.SubElement(channel, "item")
        url = absolute_url(base_url, article.url_path)
        ET.SubElement(item, "title").text = article.title
        ET.SubElement(item, "link").text = url
        ET.SubElement(item, "guid", {"isPermaLink": "true"}).text = url
        ET.SubElement(item, "pubDate").text = email.utils.format_datetime(article.date, usegmt=True)
        description = article.subtitle or re.sub(r"\s+", " ", article.markdown).strip()[:240]
        ET.SubElement(item, "description").text = description
        content = ET.SubElement(item, "{http://purl.org/rss/1.0/modules/content/}encoded")
        source_note = ""
        if article.original_url:
            source_note = f'<p><em>Originally posted on <a href="{html.escape(article.original_url, quote=True)}">X</a>.</em></p>'
        content.text = markdown_to_html(article.markdown) + source_note
        for tag in article.tags:
            ET.SubElement(item, "category").text = tag
    ET.register_namespace("content", "http://purl.org/rss/1.0/modules/content/")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(rss, encoding="unicode")


def write_csv(articles: Sequence[Article]) -> None:
    path = DIST_DIR / "substack-import.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["title", "subtitle", "date", "status", "tags", "original_url", "markdown_file", "html_file"],
        )
        writer.writeheader()
        for article in articles:
            writer.writerow(
                {
                    "title": article.title,
                    "subtitle": article.subtitle,
                    "date": article.date.isoformat(),
                    "status": article.status,
                    "tags": ", ".join(article.tags),
                    "original_url": article.original_url,
                    "markdown_file": str(article.source_path.relative_to(ROOT)),
                    "html_file": article.url_path,
                }
            )


def generate(args: argparse.Namespace) -> None:
    config = load_config()
    base_url = args.base_url or config["site_url"]
    if args.base_url:
        config["site_url"] = args.base_url
    articles = load_articles(include_drafts=args.include_drafts)
    DIST_DIR.mkdir(parents=True, exist_ok=True)
    posts_dir = DIST_DIR / "posts"
    if posts_dir.exists():
        shutil.rmtree(posts_dir)
    posts_dir.mkdir(parents=True, exist_ok=True)
    write_style()
    for article in articles:
        (posts_dir / f"{article.slug}.html").write_text(
            render_article(article, config, base_url), encoding="utf-8"
        )
    (DIST_DIR / "index.html").write_text(render_index(articles, config, base_url), encoding="utf-8")
    (DIST_DIR / "feed.xml").write_text(build_rss(articles, config, base_url), encoding="utf-8")
    write_csv(articles)
    print(f"Generated {len(articles)} post(s) in {DIST_DIR}")
    print(f"Archive: {base_url.rstrip('/')}/")
    print(f"Feed:    {base_url.rstrip('/')}/feed.xml")


def extract_x_status_id(url: str) -> Optional[str]:
    match = re.search(r"/status(?:es)?/(\d+)", url)
    if match:
        return match.group(1)
    return None


def normalize_x_url(url: str) -> str:
    url = url.strip()
    url = url.split("?", 1)[0].split("#", 1)[0]
    return url


def article_path_for_url(url: str) -> Path:
    status_id = extract_x_status_id(url) or slugify(url)[-24:]
    return CONTENT_DIR / f"{dt.date.today().isoformat()}-x-{status_id}.md"


def import_links(args: argparse.Namespace) -> None:
    source = Path(args.links_file)
    if not source.exists():
        raise SystemExit(f"Missing links file: {source}")
    CONTENT_DIR.mkdir(parents=True, exist_ok=True)
    created = 0
    for raw_line in source.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        url = normalize_x_url(line)
        path = article_path_for_url(url)
        if path.exists():
            continue
        status_id = extract_x_status_id(url) or "link"
        title = f"Draft from X {status_id}"
        path.write_text(
            f"""---
title: {title}
subtitle:
date: {dt.date.today().isoformat()}
status: draft
tags:
original_url: {url}
---

Paste or rewrite the X article/thread here.

Keep the sharp X hook, then expand the argument for Substack:

- Context
- Core claim
- Why it matters
- What changes next
""",
            encoding="utf-8",
        )
        created += 1
    print(f"Created {created} draft(s) from {source}")


def read_x_archive_json(path: Path) -> Any:
    raw = path.read_text(encoding="utf-8", errors="replace").strip()
    # X archive files commonly look like: window.YTD.tweets.part0 = [...]
    if raw.startswith("window."):
        eq = raw.find("=")
        if eq != -1:
            raw = raw[eq + 1 :].strip()
            if raw.endswith(";"):
                raw = raw[:-1]
    return json.loads(raw)


def iter_archive_tweets(payload: Any) -> Iterable[Dict[str, Any]]:
    if isinstance(payload, dict):
        for key in ("tweets", "tweet"):
            if key in payload:
                yield from iter_archive_tweets(payload[key])
        return
    if isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict) and isinstance(item.get("tweet"), dict):
                yield item["tweet"]
            elif isinstance(item, dict):
                yield item


def clean_tweet_text(tweet: Dict[str, Any]) -> str:
    text = str(tweet.get("full_text") or tweet.get("text") or "")
    text = re.sub(r"https://t\.co/\S+", "", text).strip()
    return text


def ingest_x_archive(args: argparse.Namespace) -> None:
    source = Path(args.archive_path)
    files: List[Path]
    if source.is_dir():
        files = sorted(source.rglob("tweets*.js")) + sorted(source.rglob("tweets*.json"))
    else:
        files = [source]
    if not files:
        raise SystemExit(f"No tweet archive files found under {source}")
    CONTENT_DIR.mkdir(parents=True, exist_ok=True)
    created = 0
    for file_path in files:
        payload = read_x_archive_json(file_path)
        for tweet in iter_archive_tweets(payload):
            text = clean_tweet_text(tweet)
            if len(text) < args.min_chars:
                continue
            created_at = tweet.get("created_at") or dt.date.today().isoformat()
            try:
                date = parse_date(str(created_at)).date().isoformat()
            except ValueError:
                date = dt.date.today().isoformat()
            tweet_id = str(tweet.get("id_str") or tweet.get("id") or slugify(text)[:18])
            path = CONTENT_DIR / f"{date}-x-{tweet_id}.md"
            if path.exists():
                continue
            title = textwrap.shorten(text.splitlines()[0], width=78, placeholder="")
            body = text.replace("\n", "\n\n")
            path.write_text(
                f"""---
title: {title}
subtitle:
date: {date}
status: draft
tags:
original_url:
---

{body}
""",
                encoding="utf-8",
            )
            created += 1
    print(f"Created {created} draft(s) from X archive")


def api_get(url: str, token: str) -> Dict[str, Any]:
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise SystemExit(f"X API error {error.code}: {detail}") from error


def resolve_user_id(username: str, token: str) -> str:
    clean = username.lstrip("@")
    url = f"https://api.x.com/2/users/by/username/{urllib.parse.quote(clean)}"
    payload = api_get(url, token)
    try:
        return str(payload["data"]["id"])
    except KeyError as exc:
        raise SystemExit(f"Could not resolve X username: {username}") from exc


def tweet_body(tweet: Dict[str, Any]) -> str:
    note = tweet.get("note_tweet")
    if isinstance(note, dict) and isinstance(note.get("text"), str):
        return note["text"].strip()
    article = tweet.get("article")
    if isinstance(article, dict):
        parts = []
        for key in ("title", "subtitle", "preview_text", "text"):
            value = article.get(key)
            if isinstance(value, str) and value.strip():
                parts.append(value.strip())
        if parts:
            return "\n\n".join(dict.fromkeys(parts))
    return str(tweet.get("text") or "").strip()


def fetch_x(args: argparse.Namespace) -> None:
    token = os.environ.get("X_BEARER_TOKEN")
    if not token:
        raise SystemExit("Set X_BEARER_TOKEN before running fetch-x")
    user_id = args.user_id or resolve_user_id(args.username, token)
    params = {
        "max_results": str(min(max(args.limit, 5), 100)),
        "exclude": "retweets,replies",
        "tweet.fields": "article,created_at,entities,note_tweet,public_metrics,text",
        "expansions": "article.cover_media,article.media_entities,attachments.media_keys",
        "media.fields": "alt_text,preview_image_url,type,url,width,height",
    }
    url = f"https://api.x.com/2/users/{urllib.parse.quote(user_id)}/tweets?{urllib.parse.urlencode(params)}"
    payload = api_get(url, token)
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    raw_path = RAW_DIR / f"x-posts-{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    raw_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    CONTENT_DIR.mkdir(parents=True, exist_ok=True)
    created = 0
    for tweet in payload.get("data", []):
        body = tweet_body(tweet)
        if len(body) < args.min_chars and not tweet.get("article"):
            continue
        created_at = parse_date(str(tweet.get("created_at", ""))).date().isoformat()
        tweet_id = str(tweet["id"])
        path = CONTENT_DIR / f"{created_at}-x-{tweet_id}.md"
        if path.exists():
            continue
        title = body.splitlines()[0] if body else f"X Article {tweet_id}"
        title = textwrap.shorten(title, width=86, placeholder="")
        original_url = f"https://x.com/{args.username.lstrip('@')}/status/{tweet_id}" if args.username else ""
        path.write_text(
            f"""---
title: {title}
subtitle:
date: {created_at}
status: draft
tags:
original_url: {original_url}
---

{body}
""",
            encoding="utf-8",
        )
        created += 1
    print(f"Saved raw API response to {raw_path}")
    print(f"Created {created} draft(s) from X API")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Programmatic X to Substack bridge")
    sub = parser.add_subparsers(dest="command", required=True)

    generate_parser = sub.add_parser("generate", help="Generate static archive, RSS feed, and CSV")
    generate_parser.add_argument("--include-drafts", action="store_true", help="Include draft posts in generated output")
    generate_parser.add_argument("--base-url", help="Public base URL for links in feed.xml")
    generate_parser.set_defaults(func=generate)

    links_parser = sub.add_parser("import-links", help="Create draft Markdown files from X URLs")
    links_parser.add_argument("links_file")
    links_parser.set_defaults(func=import_links)

    archive_parser = sub.add_parser("ingest-x-archive", help="Create draft Markdown files from an X archive export")
    archive_parser.add_argument("archive_path")
    archive_parser.add_argument("--min-chars", type=int, default=500, help="Only import posts at least this long")
    archive_parser.set_defaults(func=ingest_x_archive)

    fetch_parser = sub.add_parser("fetch-x", help="Fetch recent posts with the official X API")
    who = fetch_parser.add_mutually_exclusive_group(required=True)
    who.add_argument("--username")
    who.add_argument("--user-id")
    fetch_parser.add_argument("--limit", type=int, default=100)
    fetch_parser.add_argument("--min-chars", type=int, default=500)
    fetch_parser.set_defaults(func=fetch_x)

    args = parser.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
