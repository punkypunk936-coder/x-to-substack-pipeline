#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import csv
import datetime as dt
import difflib
import email.utils
import html
import json
import mimetypes
import os
import re
import shutil
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
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
DRAFT_PIPELINE_JSON = DATA_DIR / "draft_pipeline.json"
SYNC_STATE_JSON = DATA_DIR / "sync_state.json"
MEDIA_DIR = DATA_DIR / "media"
PIPELINE_BACKUP_DIR = DATA_DIR / "backups"
EDITOR_BACKUP_DIR = PIPELINE_BACKUP_DIR / "editor"
ENV_FILE = ROOT / ".env"
DRAFT_LOCK = threading.RLock()
SYNC_LOCK = threading.Lock()
PUBLISH_LOCK = threading.Lock()

TEXT_BLOCK_TYPES = {"paragraph", "heading", "subheading", "pull_quote", "quote", "code"}
LIST_BLOCK_TYPES = {"bullet_list", "numbered_list"}
MEDIA_BLOCK_TYPES = {"image", "embed"}
BLOCK_TYPES = TEXT_BLOCK_TYPES | LIST_BLOCK_TYPES | MEDIA_BLOCK_TYPES | {"divider"}
IMAGE_MIME_EXTENSIONS = {
    "image/gif": ".gif",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
MAX_MEDIA_BYTES = 12 * 1024 * 1024
MAX_REMOTE_MEDIA_BYTES = 12 * 1024 * 1024


UA = "ManvinderWritingDesk/1.0"
SUBSTACK_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123 Safari/537.36"
)
FXTWITTER_API_BASE = "https://api.fxtwitter.com"
RICH_EXTRACTION_VERSION = "x_draftjs_api_v1"


try:
    import certifi  # type: ignore
except ModuleNotFoundError:  # pragma: no cover
    certifi = None  # type: ignore[assignment]

try:
    import browser_cookie3  # type: ignore
    import requests  # type: ignore
except ModuleNotFoundError:  # pragma: no cover
    browser_cookie3 = None  # type: ignore[assignment]
    requests = None  # type: ignore[assignment]


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


class ProseMirrorInlineParser(HTMLParser):
    MARK_TAGS = {
        "b": "strong",
        "strong": "strong",
        "i": "em",
        "em": "em",
        "u": "underline",
        "s": "strike",
        "strike": "strike",
        "code": "code",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.nodes: List[Dict[str, Any]] = []
        self.mark_stack: List[Tuple[str, Dict[str, Any]]] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        tag = tag.lower()
        if tag == "br":
            self.nodes.append({"type": "hard_break"})
            return
        mark_type = self.MARK_TAGS.get(tag)
        mark: Optional[Dict[str, Any]] = {"type": mark_type} if mark_type else None
        if tag == "a":
            values = {key.lower(): value or "" for key, value in attrs}
            href = safe_web_url(values.get("href"), allow_mailto=True)
            if href:
                mark = {"type": "link", "attrs": {"href": href, "target": "_blank"}}
        if mark:
            self.mark_stack.append((tag, mark))

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        for index in range(len(self.mark_stack) - 1, -1, -1):
            if self.mark_stack[index][0] == tag:
                self.mark_stack.pop(index)
                break

    def handle_data(self, data: str) -> None:
        if not data:
            return
        node: Dict[str, Any] = {"type": "text", "text": data}
        if self.mark_stack:
            node["marks"] = [dict(mark) for _, mark in self.mark_stack]
        self.nodes.append(node)


class InlineHTMLSanitizer(HTMLParser):
    allowed_tags = {"a", "b", "br", "code", "em", "i", "mark", "s", "strike", "strong", "sub", "sup", "u"}
    void_tags = {"br"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.parts: List[str] = []
        self.open_tags: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        tag = tag.lower()
        if tag not in self.allowed_tags:
            return
        rendered_attrs = ""
        if tag == "a":
            values = {key.lower(): value or "" for key, value in attrs}
            href = safe_web_url(values.get("href", ""), allow_mailto=True)
            if href:
                rendered_attrs = f' href="{html.escape(href, quote=True)}" target="_blank" rel="noopener noreferrer"'
        self.parts.append(f"<{tag}{rendered_attrs}>")
        if tag not in self.void_tags:
            self.open_tags.append(tag)

    def handle_startendtag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag not in self.allowed_tags or tag in self.void_tags or tag not in self.open_tags:
            return
        while self.open_tags:
            opened = self.open_tags.pop()
            self.parts.append(f"</{opened}>")
            if opened == tag:
                break

    def handle_data(self, data: str) -> None:
        self.parts.append(html.escape(data, quote=False))

    def handle_entityref(self, name: str) -> None:
        self.parts.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self.parts.append(f"&#{name};")

    @property
    def value(self) -> str:
        while self.open_tags:
            self.parts.append(f"</{self.open_tags.pop()}>")
        return "".join(self.parts).strip()


def safe_web_url(value: Any, *, allow_mailto: bool = False, allow_local_media: bool = False) -> str:
    url = str(value or "").strip()
    if not url:
        return ""
    if allow_local_media and re.fullmatch(r"/media/[A-Za-z0-9._-]+", url):
        return url
    parsed = urllib.parse.urlparse(url)
    allowed_schemes = {"http", "https"} | ({"mailto"} if allow_mailto else set())
    return url if parsed.scheme.lower() in allowed_schemes else ""


def sanitize_inline_html(value: Any) -> str:
    parser = InlineHTMLSanitizer()
    parser.feed(str(value or ""))
    parser.close()
    return parser.value


def strip_inline_html(value: Any) -> str:
    parser = TextParser()
    parser.feed(str(value or ""))
    return html.unescape(parser.text)


def block_id(value: Any = None) -> str:
    candidate = re.sub(r"[^A-Za-z0-9_-]", "", str(value or ""))[:64]
    return candidate or uuid.uuid4().hex[:12]


def normalize_block(raw: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    block_type = str(raw.get("type") or "paragraph")
    if block_type not in BLOCK_TYPES:
        block_type = "paragraph"
    block: Dict[str, Any] = {"id": block_id(raw.get("id")), "type": block_type}
    if block_type in TEXT_BLOCK_TYPES:
        content = sanitize_inline_html(raw.get("html") if raw.get("html") is not None else raw.get("text"))
        if block_type != "code" and not strip_inline_html(content).strip() and "<br" not in content:
            content = ""
        block["html"] = content
    elif block_type in LIST_BLOCK_TYPES:
        items = raw.get("items") if isinstance(raw.get("items"), list) else []
        block["items"] = [sanitize_inline_html(item) for item in items if strip_inline_html(item).strip()]
        if not block["items"]:
            block["items"] = [""]
    elif block_type == "image":
        image_url = safe_web_url(raw.get("url"), allow_local_media=True)
        if re.search(r"/(profile_images|profile_banners|emoji)/", image_url, re.I):
            return None
        block.update(
            {
                "url": image_url,
                "alt": clean_text(str(raw.get("alt") or ""))[:500],
                "caption": clean_text(str(raw.get("caption") or ""))[:500],
                "layout": str(raw.get("layout") or "regular") if str(raw.get("layout") or "regular") in {"regular", "wide", "full"} else "regular",
            }
        )
        if not block["url"]:
            return None
    elif block_type == "embed":
        url = safe_web_url(raw.get("url"))
        if not url:
            return None
        block.update({"url": url, "caption": clean_text(str(raw.get("caption") or ""))[:500]})
    return block


def legacy_blocks(draft: Dict[str, Any]) -> List[Dict[str, Any]]:
    blocks: List[Dict[str, Any]] = []
    body = clean_text(str(draft.get("body") or ""))
    parts = [part.strip() for part in re.split(r"\n{2,}", body) if part.strip()]
    if "\n\n" not in body and len(body.splitlines()) >= 8:
        parts = []
        lines = [line.strip() for line in body.splitlines() if line.strip()]

        def looks_like_heading(index: int) -> bool:
            line = lines[index]
            if len(line) > 72 or word_count(line) > 11 or re.search(r"[.!?,:;]$", line):
                return False
            if index == len(lines) - 1:
                return False
            following = lines[index + 1]
            return len(following) >= 70 or word_count(following) >= 12 or (
                len(following) >= len(line) + 20 and bool(re.search(r"[.!?]$", following))
            )

        index = 0
        while index < len(lines):
            line = lines[index]
            if looks_like_heading(index):
                parts.append(f"## {line}")
                index += 1
                continue
            parts.append(line)
            if line.endswith(":"):
                list_items: List[str] = []
                cursor = index + 1
                while cursor < len(lines):
                    candidate = lines[cursor]
                    if (list_items and candidate.endswith(":")) or len(candidate) > 58 or word_count(candidate) > 8:
                        break
                    list_items.append(candidate)
                    cursor += 1
                if len(list_items) >= 2:
                    parts.extend([f"- {item}" for item in list_items])
                    index = cursor
                    continue
            index += 1

        grouped: List[str] = []
        index = 0
        while index < len(parts):
            if parts[index].startswith("- "):
                items: List[str] = []
                while index < len(parts) and parts[index].startswith("- "):
                    items.append(parts[index][2:])
                    index += 1
                grouped.append("\n".join(f"- {item}" for item in items))
                continue
            grouped.append(parts[index])
            index += 1
        parts = grouped

    for part in parts:
        text = part.strip()
        if not text:
            continue
        block_type = "paragraph"
        if text.startswith("### "):
            block_type, text = "subheading", text[4:]
        elif text.startswith("## "):
            block_type, text = "heading", text[3:]
        elif text.startswith("> "):
            block_type, text = "quote", text[2:]
        elif text.startswith("- "):
            blocks.append(
                {
                    "id": block_id(),
                    "type": "bullet_list",
                    "items": [html.escape(line[2:].strip()) for line in text.splitlines() if line.startswith("- ")],
                }
            )
            continue
        blocks.append({"id": block_id(), "type": block_type, "html": html.escape(text).replace("\n", "<br>")})
    for media in list(draft.get("media") or []):
        if not isinstance(media, dict):
            continue
        url = safe_web_url(media.get("url"), allow_local_media=True)
        if not url:
            continue
        if str(media.get("type") or "image") == "image":
            blocks.append(
                {
                    "id": block_id(),
                    "type": "image",
                    "url": url,
                    "alt": clean_text(str(media.get("alt") or ""))[:500],
                    "caption": "",
                    "layout": "regular",
                }
            )
        else:
            blocks.append({"id": block_id(), "type": "embed", "url": url, "caption": "Media from X"})
    return blocks or [{"id": block_id(), "type": "paragraph", "html": ""}]


def normalize_blocks(draft: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw_blocks = draft.get("blocks")
    if not isinstance(raw_blocks, list):
        return legacy_blocks(draft)
    blocks = [block for raw in raw_blocks if (block := normalize_block(raw)) is not None]
    return blocks or [{"id": block_id(), "type": "paragraph", "html": ""}]


def block_plain_text(block: Dict[str, Any]) -> str:
    block_type = str(block.get("type") or "paragraph")
    if block_type in TEXT_BLOCK_TYPES:
        return strip_inline_html(block.get("html"))
    if block_type in LIST_BLOCK_TYPES:
        return " ".join(strip_inline_html(item) for item in block.get("items") or [])
    if block_type in MEDIA_BLOCK_TYPES:
        return " ".join([str(block.get("alt") or ""), str(block.get("caption") or "")]).strip()
    return ""


def blocks_plain_text(blocks: List[Dict[str, Any]]) -> str:
    return "\n\n".join(text for block in blocks if (text := block_plain_text(block).strip()))


def blocks_to_markdown(blocks: List[Dict[str, Any]]) -> str:
    chunks: List[str] = []
    for block in blocks:
        block_type = str(block.get("type") or "paragraph")
        text = block_plain_text(block).strip()
        if block_type == "heading":
            chunks.append(f"## {text}")
        elif block_type == "subheading":
            chunks.append(f"### {text}")
        elif block_type in {"pull_quote", "quote"}:
            chunks.append("\n".join(f"> {line}" for line in text.splitlines()))
        elif block_type == "code":
            chunks.append(f"```\n{text}\n```")
        elif block_type == "bullet_list":
            chunks.append("\n".join(f"- {strip_inline_html(item)}" for item in block.get("items") or []))
        elif block_type == "numbered_list":
            chunks.append("\n".join(f"{index}. {strip_inline_html(item)}" for index, item in enumerate(block.get("items") or [], 1)))
        elif block_type == "divider":
            chunks.append("---")
        elif block_type == "image":
            chunks.append(f"![{block.get('alt') or ''}]({block.get('url') or ''})")
            if block.get("caption"):
                chunks.append(str(block["caption"]))
        elif block_type == "embed":
            chunks.append(str(block.get("url") or ""))
        elif text:
            chunks.append(text)
    return "\n\n".join(chunks).strip()


def blocks_to_html(blocks: List[Dict[str, Any]]) -> str:
    chunks: List[str] = []
    for block in blocks:
        block_type = str(block.get("type") or "paragraph")
        content = str(block.get("html") or "")
        if block_type == "paragraph":
            chunks.append(f"<p>{content or '<br>'}</p>")
        elif block_type == "heading":
            chunks.append(f"<h2>{content}</h2>")
        elif block_type == "subheading":
            chunks.append(f"<h3>{content}</h3>")
        elif block_type == "pull_quote":
            chunks.append(f'<blockquote class="pull-quote"><p>{content}</p></blockquote>')
        elif block_type == "quote":
            chunks.append(f"<blockquote><p>{content}</p></blockquote>")
        elif block_type == "code":
            chunks.append(f"<pre><code>{html.escape(strip_inline_html(content))}</code></pre>")
        elif block_type in LIST_BLOCK_TYPES:
            tag = "ul" if block_type == "bullet_list" else "ol"
            items = "".join(f"<li>{item}</li>" for item in block.get("items") or [])
            chunks.append(f"<{tag}>{items}</{tag}>")
        elif block_type == "divider":
            chunks.append("<hr>")
        elif block_type == "image":
            url = html.escape(str(block.get("url") or ""), quote=True)
            alt = html.escape(str(block.get("alt") or ""), quote=True)
            caption = html.escape(str(block.get("caption") or ""))
            layout = html.escape(str(block.get("layout") or "regular"), quote=True)
            chunks.append(
                f'<figure data-layout="{layout}"><img src="{url}" alt="{alt}">'
                + (f"<figcaption>{caption}</figcaption>" if caption else "")
                + "</figure>"
            )
        elif block_type == "embed":
            url = html.escape(str(block.get("url") or ""), quote=True)
            caption = html.escape(str(block.get("caption") or ""))
            chunks.append(f'<p class="embed"><a href="{url}">{caption or url}</a></p>')
    return "\n".join(chunks)


def inline_html_to_prosemirror(value: Any) -> List[Dict[str, Any]]:
    parser = ProseMirrorInlineParser()
    parser.feed(sanitize_inline_html(value))
    parser.close()
    return parser.nodes


def substack_document(
    draft: Dict[str, Any],
    image_urls: Optional[Dict[str, str]] = None,
) -> Tuple[Dict[str, Any], Optional[str]]:
    image_urls = image_urls or {}
    blocks = normalize_blocks(draft)
    cover_block_id: Optional[str] = None
    cover_url: Optional[str] = None
    if blocks and blocks[0].get("type") == "image" and blocks[0].get("layout") in {"wide", "full"}:
        cover_block_id = str(blocks[0].get("id") or "")
        cover_url = image_urls.get(cover_block_id) or str(blocks[0].get("url") or "") or None

    nodes: List[Dict[str, Any]] = []
    for block in blocks:
        block_type = str(block.get("type") or "paragraph")
        block_id_value = str(block.get("id") or "")
        if block_type == "image":
            image_url = image_urls.get(block_id_value) or str(block.get("url") or "")
            attrs = {
                "src": image_url,
                "fullscreen": True if block.get("layout") == "full" else None,
                "imageSize": None,
                "height": None,
                "width": None,
                "resizeWidth": None,
                "bytes": None,
                "alt": str(block.get("alt") or "") or None,
                "title": None,
                "type": None,
                "href": None,
                "belowTheFold": True,
                "internalRedirect": None,
            }
            content: List[Dict[str, Any]] = [{"type": "image2", "attrs": attrs}]
            caption = str(block.get("caption") or "").strip()
            if caption:
                content.append({"type": "caption", "content": [{"type": "text", "text": caption}]})
            nodes.append({"type": "captionedImage", "content": content})
            continue
        if block_type == "divider":
            nodes.append({"type": "horizontal_rule"})
            continue
        if block_type == "embed":
            url = safe_web_url(block.get("url"))
            if url:
                label = str(block.get("caption") or url)
                nodes.append(
                    {
                        "type": "paragraph",
                        "content": [
                            {
                                "type": "text",
                                "text": label,
                                "marks": [{"type": "link", "attrs": {"href": url, "target": "_blank"}}],
                            }
                        ],
                    }
                )
            continue
        if block_type in LIST_BLOCK_TYPES:
            list_type = "bullet_list" if block_type == "bullet_list" else "ordered_list"
            attrs: Dict[str, Any] = {"tight": False}
            if list_type == "ordered_list":
                attrs["order"] = 1
            items = [
                {"type": "list_item", "content": [{"type": "paragraph", "content": inline_html_to_prosemirror(item)}]}
                for item in block.get("items") or []
            ]
            nodes.append({"type": list_type, "attrs": attrs, "content": items})
            continue
        content = inline_html_to_prosemirror(block.get("html") or "")
        if block_type == "heading":
            nodes.append({"type": "heading", "attrs": {"level": 2}, "content": content})
        elif block_type == "subheading":
            nodes.append({"type": "heading", "attrs": {"level": 3}, "content": content})
        elif block_type in {"pull_quote", "quote"}:
            nodes.append({"type": "blockquote", "content": [{"type": "paragraph", "content": content}]})
        elif block_type == "code":
            text = strip_inline_html(str(block.get("html") or ""))
            nodes.append({"type": "code_block", "content": [{"type": "text", "text": text}] if text else []})
        else:
            nodes.append({"type": "paragraph", "content": content})
    if not nodes:
        nodes.append({"type": "paragraph"})
    return {"type": "doc", "content": nodes}, cover_url


def save_media_upload(payload: Dict[str, Any]) -> Dict[str, Any]:
    mime_type = str(payload.get("type") or "").lower().split(";", 1)[0]
    extension = IMAGE_MIME_EXTENSIONS.get(mime_type)
    if not extension:
        raise ValueError("Upload a PNG, JPEG, WebP, or GIF image.")
    encoded = str(payload.get("data") or "")
    if encoded.startswith("data:"):
        _, _, encoded = encoded.partition(",")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("That image could not be read.") from exc
    if not raw:
        raise ValueError("That image is empty.")
    if len(raw) > MAX_MEDIA_BYTES:
        raise ValueError("Images must be 12 MB or smaller.")
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{uuid.uuid4().hex}{extension}"
    path = MEDIA_DIR / filename
    path.write_bytes(raw)
    return {
        "url": f"/media/{filename}",
        "name": clean_text(str(payload.get("name") or filename))[:240],
        "type": mime_type,
        "size": len(raw),
    }


def media_file_from_url(url: str) -> Optional[Path]:
    match = re.fullmatch(r"/media/([A-Za-z0-9._-]+)", url)
    if not match:
        return None
    candidate = (MEDIA_DIR / match.group(1)).resolve()
    try:
        candidate.relative_to(MEDIA_DIR.resolve())
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def download_remote_image(url: str) -> Optional[str]:
    """Make a remote image pasteable by Substack's editor.

    Substack can accept image files from a paste, but it does not reliably
    ingest an external <img src="https://..."> from synthetic clipboard HTML.
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return None
    request = urllib.request.Request(
        url,
        headers={
            "accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
            "user-agent": UA,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=25, context=ssl_context()) as response:
            content_type = (response.headers.get_content_type() or "").lower()
            if not content_type.startswith("image/"):
                return None
            raw = response.read(MAX_REMOTE_MEDIA_BYTES + 1)
    except (urllib.error.URLError, TimeoutError, OSError):
        return None
    if not raw or len(raw) > MAX_REMOTE_MEDIA_BYTES:
        return None
    return f"data:{content_type};base64,{base64.b64encode(raw).decode('ascii')}"


def publish_ready_draft(draft: Dict[str, Any]) -> Dict[str, Any]:
    ready = dict(draft)
    blocks = normalize_blocks(draft)
    publish_blocks: List[Dict[str, Any]] = []
    for block in blocks:
        item = dict(block)
        if item.get("type") == "image":
            path = media_file_from_url(str(item.get("url") or ""))
            if path:
                mime_type = mimetypes.guess_type(path.name)[0] or "image/jpeg"
                item["url"] = f"data:{mime_type};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"
            elif str(item.get("url") or "").startswith(("http://", "https://")):
                embedded = download_remote_image(str(item["url"]))
                if embedded:
                    item["url"] = embedded
        publish_blocks.append(item)
    ready["blocks"] = publish_blocks
    ready["body"] = blocks_plain_text(publish_blocks)
    return ready


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


def draft_word_count(draft: Dict[str, Any]) -> int:
    if isinstance(draft.get("blocks"), list):
        return word_count(blocks_plain_text(normalize_blocks(draft)))
    return word_count(str(draft.get("body") or ""))


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
    blocks = normalize_blocks(draft) if isinstance(draft.get("blocks"), list) else []
    if str(draft.get("source") or "") == "original":
        return bool(str(draft.get("title") or "").strip()) and bool(blocks)
    media = list(draft.get("media") or []) or [block for block in blocks if block.get("type") in MEDIA_BLOCK_TYPES]
    if is_login_wall(body):
        return False
    if is_byline_only(body):
        return False
    words = word_count(blocks_plain_text(blocks)) if blocks else word_count(body)
    if words >= 35:
        return True
    return bool(media) and words >= 12


def first_str(obj: Dict[str, Any], keys: List[str]) -> str:
    for key in keys:
        value = obj.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def x_url_entities(entities: Any) -> List[Dict[str, Any]]:
    if not isinstance(entities, dict) or not isinstance(entities.get("urls"), list):
        return []
    result: List[Dict[str, Any]] = []
    for raw in entities["urls"]:
        if not isinstance(raw, dict):
            continue
        indices = raw.get("indices") if isinstance(raw.get("indices"), list) else []
        start = raw.get("start", indices[0] if len(indices) > 1 else None)
        end = raw.get("end", indices[1] if len(indices) > 1 else None)
        if not isinstance(start, int) or not isinstance(end, int) or start < 0 or end <= start:
            continue
        href = safe_web_url(first_str(raw, ["unwound_url", "expanded_url", "url"]), allow_mailto=True)
        if not href:
            continue
        result.append({**raw, "start": start, "end": end, "href": href})
    return sorted(result, key=lambda item: (int(item["start"]), int(item["end"])))


def linked_segment_html(text: str, start_offset: int, entities: Any) -> str:
    cursor = 0
    parts: List[str] = []
    segment_end = start_offset + len(text)
    for entity in x_url_entities(entities):
        start = int(entity["start"])
        end = int(entity["end"])
        if start < start_offset or end > segment_end:
            continue
        local_start = start - start_offset
        local_end = end - start_offset
        if local_start < cursor:
            continue
        parts.append(html.escape(text[cursor:local_start]).replace("\n", "<br>"))
        visible = text[local_start:local_end]
        parts.append(
            f'<a href="{html.escape(str(entity["href"]), quote=True)}" target="_blank" rel="noopener noreferrer">'
            f"{html.escape(visible)}</a>"
        )
        cursor = local_end
    parts.append(html.escape(text[cursor:]).replace("\n", "<br>"))
    return "".join(parts)


def blocks_from_x_text(text: str, entities: Any) -> List[Dict[str, Any]]:
    blocks: List[Dict[str, Any]] = []
    for match in re.finditer(r"(?:[^\n]|\n(?!\n))+", text):
        raw = match.group(0)
        leading = len(raw) - len(raw.lstrip("\n"))
        trailing = len(raw) - len(raw.rstrip("\n"))
        content = raw[leading : len(raw) - trailing if trailing else len(raw)]
        if not content.strip():
            continue
        blocks.append(
            {
                "id": block_id(),
                "type": "paragraph",
                "html": linked_segment_html(content, match.start() + leading, entities),
            }
        )
    return blocks


def media_key(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return first_str(value, ["media_key", "id", "key"])
    return ""


def utf16_offset_to_index(text: str, offset: int) -> int:
    if offset < 0:
        raise ValueError("Negative Draft.js text offset.")
    units = 0
    for index, character in enumerate(text):
        if units == offset:
            return index
        units += 2 if ord(character) > 0xFFFF else 1
        if units > offset:
            raise ValueError("Draft.js range splits a Unicode character.")
    if units == offset:
        return len(text)
    raise ValueError("Draft.js range exceeds its text block.")


def draftjs_entity_map(content: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    raw = content.get("entityMap")
    if isinstance(raw, dict):
        return {str(key): value for key, value in raw.items() if isinstance(value, dict)}
    if isinstance(raw, list):
        result: Dict[str, Dict[str, Any]] = {}
        for entry in raw:
            if not isinstance(entry, dict) or not isinstance(entry.get("value"), dict):
                continue
            result[str(entry.get("key"))] = entry["value"]
        return result
    return {}


def draftjs_inline_html(text: str, block: Dict[str, Any], entities: Dict[str, Dict[str, Any]]) -> str:
    intervals: List[Dict[str, Any]] = []
    style_tags = {
        "bold": ("strong", 10),
        "italic": ("em", 20),
        "underline": ("u", 30),
        "strikethrough": ("s", 40),
        "code": ("code", 50),
        "monospace": ("code", 50),
    }

    for index, raw in enumerate(block.get("inlineStyleRanges") or []):
        if not isinstance(raw, dict):
            continue
        style = str(raw.get("style") or "").replace("_", "").replace("-", "").lower()
        if style not in style_tags:
            raise ValueError(f"Unsupported X Article inline style: {raw.get('style')}")
        offset = raw.get("offset")
        length = raw.get("length")
        if not isinstance(offset, int) or not isinstance(length, int) or length <= 0:
            continue
        start = utf16_offset_to_index(text, offset)
        end = utf16_offset_to_index(text, offset + length)
        tag, priority = style_tags[style]
        intervals.append(
            {
                "id": f"style-{index}",
                "start": start,
                "end": end,
                "open": f"<{tag}>",
                "close": f"</{tag}>",
                "priority": priority,
            }
        )

    for index, raw in enumerate(block.get("entityRanges") or []):
        if not isinstance(raw, dict):
            continue
        entity = entities.get(str(raw.get("key")))
        if not isinstance(entity, dict):
            raise ValueError("X Article references a missing Draft.js entity.")
        entity_type = str(entity.get("type") or "").upper()
        if entity_type != "LINK":
            if str(block.get("type") or "") == "atomic":
                continue
            raise ValueError(f"Unsupported inline X Article entity: {entity_type or 'unknown'}")
        data = entity.get("data") if isinstance(entity.get("data"), dict) else {}
        href = safe_web_url(first_str(data, ["url", "href", "expanded_url"]), allow_mailto=True)
        if not href:
            raise ValueError("X Article contains an invalid link target.")
        offset = raw.get("offset")
        length = raw.get("length")
        if not isinstance(offset, int) or not isinstance(length, int) or length <= 0:
            raise ValueError("X Article contains an invalid link range.")
        start = utf16_offset_to_index(text, offset)
        end = utf16_offset_to_index(text, offset + length)
        intervals.append(
            {
                "id": f"entity-{index}",
                "start": start,
                "end": end,
                "open": f'<a href="{html.escape(href, quote=True)}" target="_blank" rel="noopener noreferrer">',
                "close": "</a>",
                "priority": 0,
            }
        )

    if not intervals:
        return html.escape(text).replace("\n", "<br>")

    boundaries = sorted({0, len(text), *(int(item["start"]) for item in intervals), *(int(item["end"]) for item in intervals)})
    output: List[str] = []
    active: List[Dict[str, Any]] = []
    for boundary_index, start in enumerate(boundaries[:-1]):
        end = boundaries[boundary_index + 1]
        next_active = sorted(
            [item for item in intervals if int(item["start"]) <= start < int(item["end"])],
            key=lambda item: (int(item["priority"]), int(item["start"]), -int(item["end"]), str(item["id"])),
        )
        common = 0
        while common < len(active) and common < len(next_active) and active[common]["id"] == next_active[common]["id"]:
            common += 1
        output.extend(str(item["close"]) for item in reversed(active[common:]))
        output.extend(str(item["open"]) for item in next_active[common:])
        output.append(html.escape(text[start:end]).replace("\n", "<br>"))
        active = next_active
    output.extend(str(item["close"]) for item in reversed(active))
    return "".join(output)


def fxtwitter_media_block(raw: Any, *, layout: str = "regular", caption: str = "") -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    info = raw.get("media_info") if isinstance(raw.get("media_info"), dict) else raw
    url = first_str(info, ["original_img_url", "url", "preview_image_url"])
    width = info.get("original_img_width") or info.get("width") or ""
    height = info.get("original_img_height") or info.get("height") or ""
    if url:
        return {
            "id": block_id(),
            "type": "image",
            "url": url,
            "alt": first_str(raw, ["alt_text", "altText"]) or first_str(info, ["alt_text", "altText"]) or "Image",
            "caption": clean_text(caption)[:500],
            "layout": layout,
            "width": str(width),
            "height": str(height),
            "x_media_id": str(raw.get("media_id") or ""),
        }
    video_info = info.get("video_info") if isinstance(info.get("video_info"), dict) else info
    variants = video_info.get("variants") if isinstance(video_info.get("variants"), list) else []
    videos = [
        item
        for item in variants
        if isinstance(item, dict) and str(item.get("content_type") or "").startswith("video/") and safe_web_url(item.get("url"))
    ]
    if videos:
        selected = max(videos, key=lambda item: int(item.get("bitrate") or 0))
        return {
            "id": block_id(),
            "type": "embed",
            "url": str(selected["url"]),
            "caption": clean_text(caption)[:500] or "Video from X",
            "x_media_id": str(raw.get("media_id") or ""),
        }
    return None


def draftjs_blocks(article: Dict[str, Any]) -> List[Dict[str, Any]]:
    content = article.get("content") if isinstance(article.get("content"), dict) else {}
    raw_blocks = content.get("blocks") if isinstance(content.get("blocks"), list) else []
    if not raw_blocks:
        raise ValueError("The X Article API did not return its rich block document.")
    entities = draftjs_entity_map(content)
    article_media = article.get("media_entities") if isinstance(article.get("media_entities"), list) else []
    media_by_id = {
        str(item.get("media_id")): item
        for item in article_media
        if isinstance(item, dict) and item.get("media_id") is not None
    }
    used_media_ids: set[str] = set()
    blocks: List[Dict[str, Any]] = []
    cover = fxtwitter_media_block(article.get("cover_media"), layout="wide")
    if cover:
        blocks.append(cover)

    pending_list_type = ""
    pending_list_items: List[str] = []

    def flush_list() -> None:
        nonlocal pending_list_type, pending_list_items
        if pending_list_items:
            blocks.append({"id": block_id(), "type": pending_list_type, "items": pending_list_items})
        pending_list_type = ""
        pending_list_items = []

    for raw in raw_blocks:
        if not isinstance(raw, dict):
            raise ValueError("X Article contains an invalid Draft.js block.")
        raw_type = str(raw.get("type") or "unstyled")
        text = str(raw.get("text") or "")
        if raw_type in {"unordered-list-item", "ordered-list-item"}:
            list_type = "bullet_list" if raw_type == "unordered-list-item" else "numbered_list"
            if pending_list_type and pending_list_type != list_type:
                flush_list()
            pending_list_type = list_type
            pending_list_items.append(draftjs_inline_html(text, raw, entities))
            continue
        flush_list()

        if raw_type == "atomic":
            ranges = raw.get("entityRanges") if isinstance(raw.get("entityRanges"), list) else []
            if len(ranges) != 1 or not isinstance(ranges[0], dict):
                raise ValueError("X Article contains an unresolved atomic block.")
            entity = entities.get(str(ranges[0].get("key")))
            if not isinstance(entity, dict):
                raise ValueError("X Article atomic block references a missing entity.")
            entity_type = str(entity.get("type") or "").upper()
            data = entity.get("data") if isinstance(entity.get("data"), dict) else {}
            if entity_type == "DIVIDER":
                blocks.append({"id": block_id(), "type": "divider"})
                continue
            if entity_type == "MEDIA":
                media_items = data.get("mediaItems") if isinstance(data.get("mediaItems"), list) else []
                if not media_items:
                    raise ValueError("X Article media block has no referenced media.")
                for reference in media_items:
                    media_id = str(reference.get("mediaId") or "") if isinstance(reference, dict) else ""
                    media_block = fxtwitter_media_block(
                        media_by_id.get(media_id),
                        caption=str(data.get("caption") or ""),
                    )
                    if not media_block:
                        raise ValueError("X Article contains unresolved inline media.")
                    used_media_ids.add(media_id)
                    blocks.append(media_block)
                continue
            if entity_type in {"EMBED", "LINK"}:
                embed_url = safe_web_url(first_str(data, ["url", "href", "expanded_url"]))
                if not embed_url:
                    raise ValueError("X Article contains an invalid embed.")
                blocks.append({"id": block_id(), "type": "embed", "url": embed_url, "caption": clean_text(str(data.get("caption") or ""))[:500]})
                continue
            raise ValueError(f"Unsupported X Article atomic entity: {entity_type or 'unknown'}")

        type_map = {
            "unstyled": "paragraph",
            "paragraph": "paragraph",
            "header-one": "heading",
            "header-two": "heading",
            "header-three": "subheading",
            "header-four": "subheading",
            "header-five": "subheading",
            "header-six": "subheading",
            "blockquote": "quote",
            "code-block": "code",
        }
        if raw_type not in type_map:
            raise ValueError(f"Unsupported X Article block type: {raw_type}")
        blocks.append(
            {
                "id": block_id(raw.get("key")),
                "type": type_map[raw_type],
                "html": draftjs_inline_html(text, raw, entities),
            }
        )
    flush_list()
    unresolved_media = set(media_by_id) - used_media_ids
    if unresolved_media:
        raise ValueError("X Article returned inline media without a matching block position.")
    return blocks


def draft_from_fxtwitter_payload(url: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    status = payload.get("status") if isinstance(payload.get("status"), dict) else payload.get("tweet")
    if not isinstance(status, dict):
        return None
    article = status.get("article") if isinstance(status.get("article"), dict) else {}
    if not article:
        return None
    blocks = normalize_blocks({"blocks": draftjs_blocks(article)})
    title = first_str(article, ["title", "name"])
    if not title:
        raise ValueError("X Article title is missing.")
    created = first_str(status, ["created_at"])
    try:
        date = email.utils.parsedate_to_datetime(created).date().isoformat()
    except (TypeError, ValueError, OverflowError):
        article_created = first_str(article, ["created_at"])
        date = article_created[:10] if re.match(r"\d{4}-\d{2}-\d{2}", article_created) else dt.date.today().isoformat()
    media = [dict(block) for block in blocks if block.get("type") in MEDIA_BLOCK_TYPES]
    return {
        "url": url,
        "title": title,
        "subtitle": "",
        "date": date,
        "body": blocks_plain_text(blocks),
        "blocks": blocks,
        "media": media,
        "source": "fxtwitter_read_api",
        "extraction_version": RICH_EXTRACTION_VERSION,
        "warnings": [],
        "fidelity": {
            "text": True,
            "links": True,
            "media": True,
            "browser_used": False,
            "rich_layout": True,
        },
    }


def draft_from_fxtwitter_api(url: str) -> Optional[Dict[str, Any]]:
    x_id = extract_x_id(url)
    if not x_id:
        return None
    base = str(os.environ.get("FXTWITTER_API_BASE") or FXTWITTER_API_BASE).rstrip("/")
    payload = request_json(f"{base}/2/status/{urllib.parse.quote(x_id)}", {"user-agent": UA})
    if payload.get("code") not in {None, 200}:
        raise ValueError(f"X Article read API returned status {payload.get('code')}.")
    return draft_from_fxtwitter_payload(url, payload)


def raw_article_text(article: Dict[str, Any]) -> str:
    for key in ("text", "body", "content", "preview_text"):
        value = article.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def ordered_article_media(payload: Dict[str, Any], article: Dict[str, Any]) -> List[Dict[str, Any]]:
    includes = payload.get("includes") if isinstance(payload.get("includes"), dict) else {}
    included = [item for item in includes.get("media", []) if isinstance(item, dict)] if isinstance(includes, dict) else []
    by_key = {media_key(item): item for item in included if media_key(item)}
    cover_key = media_key(article.get("cover_media"))
    raw_order: List[Any] = [article.get("cover_media")]
    article_media = article.get("media_entities")
    if isinstance(article_media, list):
        raw_order.extend(article_media)
    raw_order.extend(included)

    result: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_order:
        item = by_key.get(media_key(raw), raw if isinstance(raw, dict) else None)
        if not isinstance(item, dict):
            continue
        url = first_str(item, ["url", "preview_image_url"])
        if not url or url in seen:
            continue
        seen.add(url)
        item_type = str(item.get("type") or "image")
        if item_type not in {"image", "photo"}:
            continue
        key = media_key(item)
        result.append(
            {
                "id": block_id(),
                "type": "image",
                "url": url,
                "alt": first_str(item, ["alt_text"]) or "Image",
                "caption": "",
                "layout": "wide" if cover_key and key == cover_key else "regular",
                "width": str(item.get("width") or ""),
                "height": str(item.get("height") or ""),
            }
        )
    return result


def draft_from_x_payload(url: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    article = data.get("article") if isinstance(data.get("article"), dict) else {}
    if not article:
        return None
    # Entity offsets refer to the unmodified Article text. Cleaning or trimming it
    # before link reconstruction shifts those offsets and corrupts hyperlinks.
    body = raw_article_text(article)
    if not body.strip():
        return None
    entities = article.get("entities") if isinstance(article.get("entities"), dict) else data.get("entities")
    text_blocks = blocks_from_x_text(body, entities)
    media_blocks = ordered_article_media(payload, article)
    expected_links = len(x_url_entities(entities))
    rendered_links = sum(str(block.get("html") or "").count("<a ") for block in text_blocks)
    if rendered_links != expected_links:
        raise ValueError("X returned link offsets that do not match the Article text; no incomplete draft was created.")
    article_media_refs = article.get("media_entities") if isinstance(article.get("media_entities"), list) else []
    referenced_media = {
        media_key(item)
        for item in [article.get("cover_media"), *article_media_refs]
        if media_key(item)
    }
    resolved_media = {
        media_key(item)
        for item in (payload.get("includes", {}).get("media", []) if isinstance(payload.get("includes"), dict) else [])
        if isinstance(item, dict) and media_key(item)
    }
    if referenced_media - resolved_media:
        raise ValueError("X returned unresolved Article media; no incomplete draft was created.")
    cover = [block for block in media_blocks if block.get("layout") == "wide"]
    inline_media = [block for block in media_blocks if block.get("layout") != "wide"]
    blocks = cover + text_blocks + inline_media
    title = first_str(article, ["title", "name"]) or infer_title(body)
    subtitle = first_str(article, ["subtitle", "dek", "description", "preview_text"])
    created = first_str(data, ["created_at"])
    date = created[:10] if re.match(r"\d{4}-\d{2}-\d{2}", created) else dt.date.today().isoformat()
    return {
        "url": url,
        "title": title,
        "subtitle": subtitle,
        "date": date,
        "body": blocks_plain_text(blocks),
        "blocks": blocks,
        "media": media_blocks,
        "source": "x_api",
        "extraction_version": "x_official_api_v1",
        "warnings": [
            "X's official API preserves Article text, link targets, cover media, and inline media, but does not expose the full X editor block layout."
        ],
        "fidelity": {
            "text": True,
            "links": True,
            "media": True,
            "browser_used": False,
            "rich_layout": False,
        },
    }


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
    return draft_from_x_payload(url, payload)


def build_draft(url: str) -> Dict[str, Any]:
    normalized = normalize_x_url(url)
    draft = draft_from_fxtwitter_api(normalized)
    if draft and is_draft_usable(draft):
        return draft
    raise ValueError("The X Article read API did not return a complete rich document, so no draft was created.")


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
    body_html = blocks_to_html(normalize_blocks(draft))
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
    content.text = blocks_to_html(normalize_blocks(draft))
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
.body { font: 1.08rem/1.72 Georgia, "Times New Roman", serif; }
.body h2 { margin: 38px 0 12px; font-size: 1.7rem; line-height: 1.2; }
.body h3 { margin: 30px 0 10px; font-size: 1.28rem; line-height: 1.25; }
.body blockquote { margin: 28px 0; border-left: 3px solid var(--ink); padding-left: 20px; font-size: 1.18rem; font-style: italic; }
.body blockquote.pull-quote { margin: 38px 7%; border-top: 1px solid var(--ink); border-right: 0; border-bottom: 1px solid var(--ink); border-left: 0; padding: 24px 14px; text-align: center; font-size: 1.42rem; line-height: 1.48; }
.body pre { overflow-x: auto; border: 1px solid var(--line); border-radius: 6px; background: #f4f5f2; padding: 16px; font: .92rem/1.55 ui-monospace, SFMono-Regular, Menlo, monospace; }
.body hr { margin: 34px auto; width: 88px; border: 0; border-top: 1px solid var(--line); }
.body figure[data-layout="wide"] { margin-left: max(-8vw, -80px); margin-right: max(-8vw, -80px); }
.body figure[data-layout="full"] { margin-left: calc((min(100vw, 1180px) - 100%) / -2); margin-right: calc((min(100vw, 1180px) - 100%) / -2); }
.body .embed { border-left: 3px solid var(--accent); background: #f2f5f3; padding: 14px 16px; }
figure { margin: 28px 0 0; }
img { max-width: 100%; border-radius: 8px; border: 1px solid var(--line); display: block; }
figcaption { color: var(--muted); font-size: .9rem; margin-top: 7px; }
@media (max-width: 640px) { .article { padding: 20px; } h1 { font-size: clamp(2rem, 13vw, 3.6rem); } }
""".strip()
        + "\n",
        encoding="utf-8",
    )


def persist_draft(draft: Dict[str, Any], base_url: str) -> Dict[str, Any]:
    draft = dict(draft)
    draft["blocks"] = normalize_blocks(draft)
    draft["body"] = blocks_plain_text(draft["blocks"])
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
    markdown = blocks_to_markdown(draft["blocks"])
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
    draft["blocks"] = normalize_blocks(draft)
    draft["body"] = blocks_plain_text(draft["blocks"])
    return draft if is_draft_usable(draft) else None


def archive_editor_revision(draft: Dict[str, Any]) -> None:
    EDITOR_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    draft_id = block_id(draft.get("id") or pipeline_draft_id(draft))
    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = EDITOR_BACKUP_DIR / f"{draft_id}-{timestamp}.json"
    path.write_text(json.dumps(draft, indent=2, ensure_ascii=False), encoding="utf-8")
    revisions = sorted(EDITOR_BACKUP_DIR.glob(f"{draft_id}-*.json"), key=lambda item: item.stat().st_mtime, reverse=True)
    for old_revision in revisions[50:]:
        old_revision.unlink(missing_ok=True)


def save_dashboard_draft(payload: Dict[str, Any], base_url: str) -> Dict[str, Any]:
    draft = current_draft()
    if not draft:
        raise ValueError("Create or open a draft first.")
    title = str(payload.get("title") or "").strip()
    if not isinstance(payload.get("blocks"), list):
        raise ValueError("Draft blocks are required.")
    blocks = normalize_blocks({"blocks": payload["blocks"]})
    body = blocks_plain_text(blocks)
    if not title:
        raise ValueError("Draft title cannot be empty.")
    if str(draft.get("source") or "") != "original" and word_count(body) < 12:
        raise ValueError("Draft body is too short to save.")
    updated = dict(draft)
    updated.update(
        {
            "title": title[:240],
            "subtitle": str(payload.get("subtitle") or "").strip()[:500],
            "body": body,
            "blocks": blocks,
        }
    )
    previous_state = json.dumps(
        {"title": draft.get("title"), "subtitle": draft.get("subtitle"), "blocks": normalize_blocks(draft)},
        sort_keys=True,
        ensure_ascii=False,
    )
    next_state = json.dumps(
        {"title": updated.get("title"), "subtitle": updated.get("subtitle"), "blocks": blocks},
        sort_keys=True,
        ensure_ascii=False,
    )
    if previous_state != next_state:
        archive_editor_revision(draft)
    return upsert_pipeline_draft(updated, base_url, select=True)


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def pipeline_draft_id(draft: Dict[str, Any]) -> str:
    if str(draft.get("source") or "") == "original" and draft.get("id"):
        return block_id(draft["id"])
    x_id = extract_x_id(str(draft.get("url") or ""))
    if x_id:
        return x_id
    return bridge.slugify(
        f"{draft.get('date') or dt.date.today().isoformat()}-{draft.get('title') or 'draft'}",
        "draft",
    )


def build_original_draft() -> Dict[str, Any]:
    return {
        "id": f"original-{uuid.uuid4().hex[:12]}",
        "source": "original",
        "title": "Untitled draft",
        "subtitle": "",
        "url": "",
        "date": dt.date.today().isoformat(),
        "body": "",
        "blocks": [{"id": block_id(), "type": "paragraph", "html": ""}],
        "media": [],
        "extraction_version": "original_v1",
    }


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
        if existing is not None:
            merged = dict(existing)
            merged.update(draft)
            draft = merged
        draft["blocks"] = normalize_blocks(draft)
        draft["body"] = blocks_plain_text(draft["blocks"])
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


def mark_selected_published(url: Any = None) -> None:
    published_url = safe_web_url(url)
    with DRAFT_LOCK:
        pipeline = ensure_pipeline()
        selected_id = str(pipeline.get("selected_id") or "")
        timestamp = now_iso()
        for item in pipeline["items"]:
            if str(item.get("id")) == selected_id:
                item["status"] = "published"
                item["published_at"] = timestamp
                item["updated_at"] = timestamp
                if published_url:
                    item["substack_url"] = published_url
                break
        write_pipeline(pipeline)


def remember_substack_draft_url(url: Any) -> None:
    draft_url = safe_web_url(url)
    if not draft_url:
        return
    configured_host = urllib.parse.urlparse(str(publish_config().get("editor_url") or "")).netloc.lower()
    if configured_host and urllib.parse.urlparse(draft_url).netloc.lower() != configured_host:
        return
    with DRAFT_LOCK:
        draft = current_draft()
        if not draft:
            return
        draft["substack_draft_url"] = draft_url
        CURRENT_JSON.write_text(json.dumps(draft, indent=2), encoding="utf-8")
        pipeline = ensure_pipeline()
        selected_id = str(pipeline.get("selected_id") or "")
        for item in pipeline["items"]:
            if str(item.get("id") or "") == selected_id:
                item["substack_draft_url"] = draft_url
                item["updated_at"] = now_iso()
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


def title_match_score(left: Any, right: Any) -> float:
    left_title = normalized_title(left)
    right_title = normalized_title(right)
    if not left_title or not right_title:
        return 0.0
    if left_title == right_title:
        return 1.0
    left_tokens = {token for token in left_title.split() if len(token) > 1}
    right_tokens = {token for token in right_title.split() if len(token) > 1}
    if not left_tokens or not right_tokens:
        return 0.0
    shared = left_tokens & right_tokens
    if len(shared) < 4:
        return 0.0
    containment = len(shared) / min(len(left_tokens), len(right_tokens))
    if containment < 0.75:
        return 0.0
    union = left_tokens | right_tokens
    jaccard = len(shared) / len(union)
    sequence = difflib.SequenceMatcher(None, left_title, right_title).ratio()
    if sequence < 0.82:
        return 0.0
    return round((sequence * 0.55) + (containment * 0.25) + (jaccard * 0.20), 6)


def publication_date_matches(item: Dict[str, Any], post: Dict[str, Any], max_days: int = 21) -> bool:
    item_date = str(item.get("date") or item.get("discovered_at") or "")[:10]
    post_date = str(post.get("published_at") or "")[:10]
    try:
        left = dt.date.fromisoformat(item_date)
        right = dt.date.fromisoformat(post_date)
    except ValueError:
        return True
    return abs((left - right).days) <= max_days


def substack_feed_url() -> str:
    return os.environ.get("SUBSTACK_FEED_URL") or ""


def substack_publication_url() -> str:
    configured = str(os.environ.get("SUBSTACK_PUBLICATION_URL") or "").strip()
    parsed = urllib.parse.urlparse(configured or substack_feed_url())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}"


def substack_archive_api_url() -> str:
    configured = os.environ.get("SUBSTACK_ARCHIVE_API_URL") or ""
    if configured:
        return configured
    feed_url = substack_feed_url()
    parsed = urllib.parse.urlparse(feed_url)
    if not parsed.scheme or not parsed.netloc:
        return ""
    query = urllib.parse.urlencode({"sort": "new", "search": "", "offset": 0, "limit": 50})
    return f"{parsed.scheme}://{parsed.netloc}/api/v1/archive?{query}"


def fetch_substack_feed_publications() -> List[Dict[str, Any]]:
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
        publications.append({"title": title, "url": link, "published_at": published_at, "source": "rss"})
    return publications


def fetch_substack_archive_publications() -> List[Dict[str, Any]]:
    archive_url = substack_archive_api_url()
    if not archive_url:
        raise ValueError("Substack archive API URL is not configured.")
    payload = request_json(archive_url)
    raw_posts = payload if isinstance(payload, list) else payload.get("posts") if isinstance(payload, dict) else []
    if not isinstance(raw_posts, list):
        raise ValueError("Substack archive API returned an unexpected response.")
    publications: List[Dict[str, Any]] = []
    for post in raw_posts:
        if not isinstance(post, dict):
            continue
        title = str(post.get("title") or "").strip()
        url = str(post.get("canonical_url") or "").strip()
        if not url and post.get("slug"):
            parsed = urllib.parse.urlparse(archive_url)
            url = f"{parsed.scheme}://{parsed.netloc}/p/{urllib.parse.quote(str(post['slug']))}"
        published_at = str(post.get("post_date") or post.get("published_at") or "").strip() or None
        if not title or not url or str(post.get("audience") or "everyone") != "everyone":
            continue
        publications.append(
            {
                "title": title,
                "url": url,
                "published_at": published_at,
                "source": "archive_api",
            }
        )
    return publications


def merge_substack_publications(*sources: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    merged: Dict[str, Dict[str, Any]] = {}
    for publications in sources:
        for post in publications:
            url = str(post.get("url") or "").split("?", 1)[0].rstrip("/")
            if not url:
                continue
            existing = merged.get(url)
            if existing is None or post.get("source") == "archive_api":
                merged[url] = dict(post)
    return sorted(
        merged.values(),
        key=lambda post: str(post.get("published_at") or ""),
        reverse=True,
    )


def fetch_substack_publications() -> List[Dict[str, Any]]:
    feed_posts: List[Dict[str, Any]] = []
    archive_posts: List[Dict[str, Any]] = []
    errors: List[str] = []
    try:
        feed_posts = fetch_substack_feed_publications()
    except Exception as exc:
        errors.append(f"RSS: {exc}")
    try:
        archive_posts = fetch_substack_archive_publications()
    except Exception as exc:
        errors.append(f"Archive: {exc}")
    publications = merge_substack_publications(feed_posts, archive_posts)
    if not publications:
        raise ValueError("Could not read published Substack posts. " + " ".join(errors))
    return publications


def reconcile_substack_publications() -> Dict[str, Any]:
    publications = fetch_substack_publications()
    matched: List[str] = []
    updated: List[str] = []
    match_details: List[Dict[str, Any]] = []
    changed = False
    with DRAFT_LOCK:
        pipeline = ensure_pipeline()
        matches: Dict[str, Tuple[Dict[str, Any], float, str]] = {}
        unmatched_posts = set(range(len(publications)))

        for item in pipeline["items"]:
            item_id = str(item.get("id") or "")
            item_title = normalized_title(item.get("title"))
            if not item_id or not item_title:
                continue
            exact_candidates = [
                index
                for index in unmatched_posts
                if normalized_title(publications[index].get("title")) == item_title
                and publication_date_matches(item, publications[index])
            ]
            exact_index = min(exact_candidates, default=None)
            if exact_index is None:
                continue
            matches[item_id] = (publications[exact_index], 1.0, "exact_title")
            unmatched_posts.remove(exact_index)

        for item in pipeline["items"]:
            item_id = str(item.get("id") or "")
            if not item_id or item_id in matches:
                continue
            candidates: List[Tuple[float, int]] = []
            for index in unmatched_posts:
                post = publications[index]
                if not publication_date_matches(item, post):
                    continue
                score = title_match_score(item.get("title"), post.get("title"))
                if score >= 0.88:
                    candidates.append((score, index))
            if not candidates:
                continue
            score, post_index = max(candidates)
            matches[item_id] = (publications[post_index], score, "fuzzy_title")
            unmatched_posts.remove(post_index)

        for item in pipeline["items"]:
            draft_id = str(item.get("id") or "")
            match = matches.get(draft_id)
            if not match:
                continue
            post, score, method = match
            matched.append(draft_id)
            match_details.append(
                {
                    "id": draft_id,
                    "draft_title": item.get("title"),
                    "publication_title": post.get("title"),
                    "method": method,
                    "score": score,
                    "url": post.get("url"),
                    "source": post.get("source"),
                }
            )
            already_linked = item.get("status") == "published" and item.get("substack_url") == post["url"]
            next_published_at = (
                item.get("published_at")
                if already_linked and item.get("published_at")
                else post.get("published_at") or item.get("published_at") or now_iso()
            )
            if (
                item.get("status") != "published"
                or item.get("substack_url") != post["url"]
                or item.get("published_at") != next_published_at
            ):
                item["status"] = "published"
                item["substack_url"] = post["url"]
                item["published_at"] = next_published_at
                item["updated_at"] = now_iso()
                updated.append(draft_id)
                changed = True
        selected_id = str(pipeline.get("selected_id") or "")
        selected = next((item for item in pipeline["items"] if str(item.get("id") or "") == selected_id), None)
        if not selected or selected.get("status") == "published":
            active = [item for item in pipeline["items"] if item.get("status") != "published"]
            next_selected = max(
                active,
                key=lambda item: (
                    int(str(item.get("id") or "0")) if str(item.get("id") or "").isdigit() else 0,
                    str(item.get("discovered_at") or ""),
                ),
                default=None,
            )
            next_selected_id = next_selected.get("id") if next_selected else None
            if pipeline.get("selected_id") != next_selected_id:
                pipeline["selected_id"] = next_selected_id
                changed = True
        if changed:
            write_pipeline(pipeline)
    return {
        "feed_url": substack_feed_url(),
        "publication_count": len(publications),
        "matched": matched,
        "updated": updated,
        "match_details": match_details,
        "changed": changed,
    }


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
                "word_count": draft_word_count(item),
                "media_count": len([block for block in normalize_blocks(item) if block.get("type") in MEDIA_BLOCK_TYPES]),
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


def selected_pipeline_draft() -> Optional[Dict[str, Any]]:
    with DRAFT_LOCK:
        pipeline = ensure_pipeline()
        selected_id = str(pipeline.get("selected_id") or "")
        selected = next(
            (item for item in pipeline["items"] if str(item.get("id") or "") == selected_id),
            None,
        )
        if not isinstance(selected, dict):
            return None
        draft = dict(selected)
        draft["blocks"] = normalize_blocks(draft)
        draft["body"] = blocks_plain_text(draft["blocks"])
        return draft if is_draft_usable(draft) else None


def bootstrap_payload() -> Dict[str, Any]:
    last_publish = json.loads(PUBLISH_RESULT_JSON.read_text(encoding="utf-8")) if PUBLISH_RESULT_JSON.exists() else None
    with DRAFT_LOCK:
        return {
            "draft": selected_pipeline_draft(),
            "pipeline": pipeline_payload(),
            "ingest": ingest_config(),
            "publish": publish_config(),
            "last_publish": last_publish,
        }


def should_ingest_discovered_id(draft_id: str, existing_ids: set[str], allow_backfill: bool = False) -> bool:
    if not draft_id or draft_id in existing_ids:
        return False
    numeric_existing_ids = [int(item_id) for item_id in existing_ids if item_id.isdigit()]
    newest_existing_id = max(numeric_existing_ids, default=0)
    if newest_existing_id and draft_id.isdigit() and int(draft_id) <= newest_existing_id and not allow_backfill:
        return False
    return True


def discover_x_articles_api() -> List[Dict[str, Any]]:
    token = os.environ.get("X_BEARER_TOKEN")
    if not token:
        raise ValueError("X_BEARER_TOKEN is required for browser-free article sync.")
    handle = str(os.environ.get("X_ACCOUNT_HANDLE") or "@0xgoodie").strip().lstrip("@")
    headers = {"authorization": f"Bearer {token}", "user-agent": UA}
    user_payload = request_json(f"https://api.x.com/2/users/by/username/{urllib.parse.quote(handle)}", headers)
    user_data = user_payload.get("data") if isinstance(user_payload.get("data"), dict) else {}
    user_id = str(user_data.get("id") or "")
    if not user_id:
        raise ValueError(f"The official X API could not resolve @{handle}.")
    max_results = max(5, min(100, int(os.environ.get("X_SYNC_MAX_ARTICLES") or "30")))
    params = {
        "max_results": max_results,
        "exclude": "retweets,replies",
        "tweet.fields": "article,created_at",
    }
    timeline_url = (
        f"https://api.x.com/2/users/{urllib.parse.quote(user_id)}/tweets?"
        f"{urllib.parse.urlencode(params)}"
    )
    payload = request_json(timeline_url, headers)
    posts = payload.get("data") if isinstance(payload.get("data"), list) else []
    items: List[Dict[str, Any]] = []
    for post in posts:
        if not isinstance(post, dict) or not isinstance(post.get("article"), dict):
            continue
        post_id = str(post.get("id") or "")
        if not post_id:
            continue
        article = post["article"]
        items.append(
            {
                "id": post_id,
                "url": f"https://x.com/{handle}/status/{post_id}",
                "title": first_str(article, ["title", "name"]),
            }
        )
    return sorted(items, key=lambda item: int(item["id"]), reverse=True)


def discover_fxtwitter_articles_payload(payload: Dict[str, Any], handle: str) -> List[Dict[str, Any]]:
    posts = payload.get("results") if isinstance(payload.get("results"), list) else []
    items: List[Dict[str, Any]] = []
    for post in posts:
        if not isinstance(post, dict) or not isinstance(post.get("article"), dict):
            continue
        post_id = str(post.get("id") or "")
        title = first_str(post["article"], ["title", "name"])
        if not post_id or not title:
            continue
        items.append(
            {
                "id": post_id,
                "url": first_str(post, ["url"]) or f"https://x.com/{handle}/status/{post_id}",
                "title": title,
            }
        )
    return sorted(items, key=lambda item: int(item["id"]), reverse=True)


def discover_fxtwitter_articles_api() -> List[Dict[str, Any]]:
    handle = str(os.environ.get("X_ACCOUNT_HANDLE") or "@0xgoodie").strip().lstrip("@")
    base = str(os.environ.get("FXTWITTER_API_BASE") or FXTWITTER_API_BASE).rstrip("/")
    payload = request_json(
        f"{base}/2/profile/{urllib.parse.quote(handle)}/articles",
        {"user-agent": UA},
    )
    if payload.get("code") not in {None, 200}:
        raise ValueError(f"X Article list API returned status {payload.get('code')}.")
    return discover_fxtwitter_articles_payload(payload, handle)


def article_copy_text(draft: Dict[str, Any], *, prefer_blocks: bool = False) -> str:
    if prefer_blocks and isinstance(draft.get("blocks"), list):
        chunks = [
            block_plain_text(block).strip()
            for block in normalize_blocks(draft)
            if block.get("type") not in MEDIA_BLOCK_TYPES and block.get("type") != "divider"
        ]
        return "\n".join(chunk for chunk in chunks if chunk)
    body = str(draft.get("body") or "")
    body = re.sub(r"(?im)^\s*Image\s*$", " ", body)
    body = re.sub(r"!\[[^\]]*\]\([^\)]+\)", " ", body)
    return body


def article_copy_match_score(existing: Dict[str, Any], replacement: Dict[str, Any]) -> float:
    def comparable(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()

    old = comparable(article_copy_text(existing))
    new = comparable(article_copy_text(replacement, prefer_blocks=True))
    if not old or not new:
        return 0.0
    return difflib.SequenceMatcher(None, old, new, autojunk=False).ratio()


def backfill_pipeline_drafts(
    base_url: str,
    fetcher: Optional[Any] = None,
) -> Dict[str, Any]:
    fetch = fetcher or build_draft
    snapshot = read_pipeline()
    selected_id = str(snapshot.get("selected_id") or "")
    candidates = [
        dict(item)
        for item in snapshot.get("items", [])
        if (
            isinstance(item, dict)
            and extract_x_id(str(item.get("url") or ""))
            and str(item.get("extraction_version") or "") != RICH_EXTRACTION_VERSION
        )
    ]
    upgraded: List[str] = []
    skipped: List[Dict[str, str]] = []
    backup_path = ""
    for existing in candidates:
        draft_id = str(existing.get("id") or "")
        source_url = str(existing.get("url") or "")
        try:
            replacement = fetch(source_url)
            if not isinstance(replacement, dict):
                raise ValueError("No rich X Article document was returned.")
            if pipeline_draft_id(replacement) != draft_id:
                raise ValueError("Returned X Article ID does not match the pipeline record.")
            if title_match_score(str(existing.get("title") or ""), str(replacement.get("title") or "")) < 0.95:
                raise ValueError("Returned X Article title does not match the saved draft.")
            copy_score = article_copy_match_score(existing, replacement)
            if copy_score < 0.97:
                raise ValueError(f"Returned article copy failed verification ({copy_score:.3f}).")
            if not backup_path:
                PIPELINE_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
                backup = PIPELINE_BACKUP_DIR / f"draft_pipeline-before-{RICH_EXTRACTION_VERSION}.json"
                if not backup.exists():
                    backup.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
                backup_path = str(backup)
            upsert_pipeline_draft(replacement, base_url, select=False)
            upgraded.append(draft_id)
        except Exception as exc:
            skipped.append({"id": draft_id, "error": str(exc)})
    if selected_id and selected_id in upgraded:
        select_pipeline_draft(selected_id, base_url)
    return {
        "upgraded": upgraded,
        "skipped": skipped,
        "backup_path": backup_path,
    }


def sync_x_articles(base_url: str) -> Dict[str, Any]:
    if not SYNC_LOCK.acquire(blocking=False):
        return {"ok": True, "status": "already_syncing", "pipeline": pipeline_payload()}
    started = now_iso()
    write_sync_state({"status": "syncing", "started_at": started, "last_sync": sync_state().get("last_sync"), "last_error": None})
    try:
        discovered: List[Dict[str, Any]] = []
        added: List[str] = []
        upgraded: List[str] = []
        errors: List[str] = []
        x_ok = False
        substack_ok = False
        reconciliation: Dict[str, Any] = {}
        try:
            discovered = discover_fxtwitter_articles_api()
            x_ok = True
        except Exception as exc:
            errors.append(f"X sync: {exc}")
        existing_ids = {str(item.get("id")) for item in ensure_pipeline()["items"]}
        allow_backfill = os.environ.get("X_SYNC_BACKFILL") == "1"
        for found in discovered:
            draft_id = str(found.get("id") or "")
            url = str(found.get("url") or "")
            if not url or not should_ingest_discovered_id(draft_id, existing_ids, allow_backfill):
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
            backfill = backfill_pipeline_drafts(base_url)
            upgraded = list(backfill.get("upgraded") or [])
            for skipped in backfill.get("skipped") or []:
                errors.append(f"Rich backfill {skipped.get('id')}: {skipped.get('error')}")
        except Exception as exc:
            errors.append(f"Rich backfill: {exc}")
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
            "upgraded_count": len(upgraded),
            "substack_publication_count": reconciliation.get("publication_count", 0),
            "published_match_count": len(reconciliation.get("matched", [])),
            "substack_feed_url": reconciliation.get("feed_url") or substack_feed_url(),
        }
        write_sync_state(state)
        return {
            "ok": state["status"] != "error",
            "status": state["status"],
            "added": added,
            "upgraded": upgraded,
            "published_matches": reconciliation.get("matched", []),
            "newly_published": reconciliation.get("updated", []),
            "publication_match_details": reconciliation.get("match_details", []),
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


def ingest_config() -> Dict[str, Any]:
    return {
        "mode": "rich_x_read_api",
        "configured": True,
        "browser_automation": False,
        "rich_layout": True,
        "message": "Rich X Article capture is active through a read-only API. Chrome is never used.",
    }


def publish_config() -> Dict[str, Any]:
    publication_url = substack_publication_url()
    configured = bool(publication_url and browser_cookie3 is not None and requests is not None)
    return {
        "mode": "background_session_api",
        "configured": configured,
        "browser_automation": False,
        "write_api_available": configured,
        "editor_url": f"{publication_url}/publish/post/new" if publication_url else "",
        "direct_publish": configured,
        "message": (
            "Background Substack draft and publishing connection is ready. No browser window or tab will be opened."
            if configured
            else "Set SUBSTACK_PUBLICATION_URL (or SUBSTACK_FEED_URL) and install the local session dependencies."
        ),
    }


def substack_cookie_paths() -> List[Path]:
    configured = str(os.environ.get("SUBSTACK_CHROME_COOKIE_PATH") or "").strip()
    paths: List[Path] = [Path(configured).expanduser()] if configured else []
    chrome_root = Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
    paths.extend(sorted(chrome_root.glob("Profile */Cookies")))
    paths.append(chrome_root / "Default" / "Cookies")
    unique: List[Path] = []
    for path in paths:
        if path.is_file() and path not in unique:
            unique.append(path)
    return unique


def substack_error_message(response: Any, fallback: str) -> str:
    try:
        payload = response.json()
    except (ValueError, TypeError):
        payload = {}
    messages: List[str] = []
    if isinstance(payload, dict):
        for error in payload.get("errors") or []:
            if isinstance(error, dict) and error.get("msg"):
                messages.append(str(error["msg"]))
            elif isinstance(error, str):
                messages.append(error)
        for key in ("message", "error"):
            if isinstance(payload.get(key), str):
                messages.append(str(payload[key]))
    detail = ". ".join(dict.fromkeys(message.strip() for message in messages if message.strip()))
    return f"{fallback}: {detail}" if detail else fallback


def substack_authenticated_session() -> Tuple[Any, Dict[str, Any]]:
    if browser_cookie3 is None or requests is None:
        raise ValueError("The local Substack session connector is not installed.")
    publication_url = substack_publication_url()
    if not publication_url:
        raise ValueError("Set SUBSTACK_PUBLICATION_URL or SUBSTACK_FEED_URL first.")
    failures: List[str] = []
    for cookie_path in substack_cookie_paths():
        try:
            cookie_jar = browser_cookie3.chrome(cookie_file=str(cookie_path), domain_name="substack.com")
            if not any(cookie.name == "substack.sid" and cookie.value for cookie in cookie_jar):
                continue
            session = requests.Session()
            session.cookies.update(cookie_jar)
            session.headers.update(
                {
                    "accept": "application/json",
                    "content-type": "application/json",
                    "origin": publication_url,
                    "referer": f"{publication_url}/publish/post",
                    "user-agent": SUBSTACK_UA,
                }
            )
            response = session.get("https://substack.com/api/v1/user/profile/self", timeout=30)
            if response.status_code == 200:
                profile = response.json()
                if isinstance(profile, dict) and profile.get("id"):
                    return session, profile
            failures.append(substack_error_message(response, f"Profile {cookie_path.parent.name} is not signed in"))
        except Exception as exc:
            failures.append(f"{cookie_path.parent.name}: {exc}")
    detail = failures[-1] if failures else "No signed-in Substack session was found."
    raise ValueError(f"Sign in to Substack in Chrome once, then retry. {detail}")


def substack_request_json(
    session: Any,
    method: str,
    path: str,
    *,
    payload: Optional[Dict[str, Any]] = None,
    fallback: str,
) -> Dict[str, Any]:
    url = path if path.startswith("http") else f"{substack_publication_url()}{path}"
    response = session.request(method, url, json=payload, timeout=45)
    if response.status_code >= 400:
        raise ValueError(substack_error_message(response, fallback))
    try:
        data = response.json()
    except ValueError as exc:
        raise ValueError(f"{fallback}: Substack returned an unreadable response.") from exc
    return data if isinstance(data, dict) else {"data": data}


def substack_draft_id(draft: Dict[str, Any]) -> Optional[int]:
    match = re.search(r"/publish/post/(\d+)", str(draft.get("substack_draft_url") or ""))
    return int(match.group(1)) if match else None


def upload_substack_images(session: Any, draft: Dict[str, Any]) -> Dict[str, str]:
    ready = publish_ready_draft(draft)
    uploaded: Dict[str, str] = {}
    for block in normalize_blocks(ready):
        if block.get("type") != "image":
            continue
        image = str(block.get("url") or "")
        if not image.startswith("data:image/"):
            raise ValueError("An article image could not be prepared for Substack; nothing was published.")
        response = substack_request_json(
            session,
            "POST",
            "/api/v1/image",
            payload={"image": image},
            fallback="Substack image upload failed",
        )
        image_url = safe_web_url(response.get("url"))
        if not image_url:
            raise ValueError("Substack image upload returned no usable URL; nothing was published.")
        uploaded[str(block.get("id") or "")] = image_url
    return uploaded


def save_substack_draft(session: Any, profile: Dict[str, Any], draft: Dict[str, Any]) -> Dict[str, Any]:
    publication_url = substack_publication_url()
    image_urls = upload_substack_images(session, draft)
    document, cover_url = substack_document(draft, image_urls)
    payload: Dict[str, Any] = {
        "type": "newsletter",
        "draft_title": str(draft.get("title") or "Untitled draft")[:240],
        "draft_subtitle": str(draft.get("subtitle") or "")[:500],
        "draft_body": json.dumps(document, ensure_ascii=False, separators=(",", ":")),
        "draft_bylines": [{"id": profile["id"], "is_guest": False}],
        "audience": "everyone",
        "should_send_email": True,
        "detect_language": True,
    }
    if cover_url:
        payload["cover_image"] = cover_url

    post_id = substack_draft_id(draft)
    existing: Optional[Dict[str, Any]] = None
    if post_id:
        response = session.get(f"{publication_url}/api/v1/drafts/{post_id}", timeout=30)
        if response.status_code == 200:
            candidate = response.json()
            if isinstance(candidate, dict):
                existing = candidate
        elif response.status_code not in {404, 410}:
            raise ValueError(substack_error_message(response, "Could not open the existing Substack draft"))

    if existing and post_id:
        if existing.get("draft_updated_at"):
            payload["last_updated_at"] = existing["draft_updated_at"]
        saved = substack_request_json(
            session,
            "PUT",
            f"/api/v1/drafts/{post_id}",
            payload=payload,
            fallback="Substack draft update failed",
        )
    else:
        saved = substack_request_json(
            session,
            "POST",
            "/api/v1/drafts",
            payload=payload,
            fallback="Substack draft creation failed",
        )
    saved_id = int(saved.get("id") or post_id or 0)
    if not saved_id:
        raise ValueError("Substack saved the draft without returning its ID.")
    saved["editor_url"] = f"{publication_url}/publish/post/{saved_id}"
    saved["document"] = document
    saved["was_published"] = bool(existing and existing.get("is_published"))
    return saved


def publish_to_substack(confirm_publish: bool, allow_publish: bool = False) -> Dict[str, Any]:
    draft = current_draft()
    if not draft:
        raise ValueError("Create or open a draft first.")
    config = publish_config()
    if not config["configured"]:
        raise ValueError(str(config["message"]))
    if allow_publish and not confirm_publish:
        raise ValueError("Live publishing requires explicit confirmation.")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PUBLISH_PAYLOAD_JSON.write_text(json.dumps(draft, indent=2), encoding="utf-8")
    session, profile = substack_authenticated_session()
    saved = save_substack_draft(session, profile, draft)
    post_id = int(saved["id"])
    editor_url = str(saved["editor_url"])
    remember_substack_draft_url(editor_url)
    if allow_publish:
        prepublish = substack_request_json(
            session,
            "GET",
            f"/api/v1/drafts/{post_id}/prepublish",
            fallback="Substack pre-publish check failed",
        )
        errors = prepublish.get("errors") or []
        if errors:
            messages = [
                str(item.get("message") or item.get("msg") or item)
                if isinstance(item, dict)
                else str(item)
                for item in errors
            ]
            raise ValueError(f"Substack blocked publishing: {' '.join(messages)}")
        published = substack_request_json(
            session,
            "POST",
            f"/api/v1/drafts/{post_id}/publish",
            payload={"send": not bool(saved.get("was_published"))},
            fallback="Substack publish failed",
        )
        latest = substack_request_json(
            session,
            "GET",
            f"/api/v1/drafts/{post_id}",
            fallback="Published post verification failed",
        )
        slug = str(latest.get("slug") or published.get("slug") or "").strip()
        post_url = f"{substack_publication_url()}/p/{slug}" if slug else substack_publication_url()
        result = {
            "ok": True,
            "status": "published",
            "message": (
                "Updated the live Substack post."
                if saved.get("was_published")
                else "Published to Substack and sent to subscribers."
            ),
            "current_url": post_url,
            "post_url": post_url,
            "post_id": post_id,
        }
    else:
        result = {
            "ok": True,
            "status": "draft_saved",
            "message": "Saved as a Substack draft in the background.",
            "current_url": editor_url,
            "post_id": post_id,
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
        elif path == "/api/bootstrap":
            self.send_json(bootstrap_payload())
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
        elif path.startswith("/media/"):
            media_path = media_file_from_url(path)
            if media_path:
                self.serve_file(media_path)
            else:
                self.send_json({"error": "Media not found"}, 404)
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
            elif path == "/api/media":
                self.send_json({"ok": True, "media": save_media_upload(payload)}, 201)
            elif path == "/api/drafts/new":
                draft = upsert_pipeline_draft(build_original_draft(), self.base_url, select=True)
                self.send_json({"ok": True, "draft": draft, "pipeline": pipeline_payload()}, 201)
            elif path == "/api/draft":
                draft = save_dashboard_draft(payload, self.base_url)
                self.send_json({"ok": True, "draft": draft, "pipeline": pipeline_payload()})
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
                if not PUBLISH_LOCK.acquire(blocking=False):
                    raise ValueError("A Substack save or publish is already running. Wait a moment and retry.")
                try:
                    editor_draft = payload.get("draft")
                    if isinstance(editor_draft, dict):
                        save_dashboard_draft(editor_draft, self.base_url)
                    result = publish_to_substack(
                        confirm_publish=confirmed,
                        allow_publish=mode == "publish" and confirmed,
                    )
                finally:
                    PUBLISH_LOCK.release()
                if result.get("ok") and result.get("status") == "published":
                    mark_selected_published(result.get("post_url"))
                elif result.get("ok") and result.get("current_url"):
                    remember_substack_draft_url(result["current_url"])
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
