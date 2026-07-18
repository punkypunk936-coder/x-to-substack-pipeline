# Manvinder Writing Desk

An API-only local workspace for capturing X Articles, preserving their text, links, and media, then editing them as structured drafts.

## Run

```sh
cd "/Users/manvinderarora/Documents/New project/project/x_substack_bridge"
python3 server.py --host 127.0.0.1 --port 8788
```

Open `http://127.0.0.1:8788/`.

## Capture

The ingest and background-sync paths use FxTwitter's public, read-only API to retrieve X's rich Draft.js Article document. Browser automation, hidden windows, logged-in extraction, public embed fallbacks, and metadata fallbacks are disconnected from the server.

The rich Article response includes block types, inline styles, entity ranges, cover media, inline media, captions, and dividers. The parser:

- Preserves paragraphs, headings, quotes, lists, code, dividers, bold, italic, underline, strikethrough, and links.
- Applies Draft.js UTF-16 offsets correctly, including ranges after emoji and other multi-unit Unicode characters.
- Keeps the banner first and inserts inline media at its original atomic-block position with captions.
- Refuses to create or migrate a draft when a block, style, link, or media reference cannot be resolved.
- Verifies old and new article copy at 97% or better before replacing an existing pipeline record.

Set these values in `.env` for quiet background sync:

```sh
X_ACCOUNT_HANDLE=@0xgoodie
X_AUTO_SYNC=1
X_SYNC_INTERVAL_SECONDS=900
```

No X credential is required. The API base can be overridden with `FXTWITTER_API_BASE` for a self-hosted FxTwitter instance. The app never falls back to a browser.

## Editing

The dashboard supports paragraph, heading, subheading, quote, list, code, divider, image, and URL embed blocks. It also supports inline formatting and links, image upload or remote image insertion, block reordering, autosave, and local preview.

Existing pipeline records are backfilled once from their X URLs. The migration preserves publication state and Substack links, stores a pre-migration backup under `data/backups/`, and replaces content only after ID, title, and article-copy verification. The same sanitized block document powers preview, RSS, HTML, and CSV output.

Generated outputs:

- `dist/draft.html`
- `dist/feed.xml`
- `dist/substack-import.csv`

## Publishing Boundary

Substack's official Developer API and MCP connection are read-only; neither provides post creation or publishing. Browser publishing is intentionally disabled, so the dashboard will not open Chrome or report a successful Substack publish that it cannot verify.

Substack RSS and public archive reads remain available only to reconcile older pipeline records that were already published there. A truly browser-free Publish button needs a writable publication backend, such as a first-party writing site plus an email delivery API.

## Service

The installed macOS LaunchAgent `com.manvinder.x-substack-bridge` starts the local server at login and keeps it running. Background X sync and rich-layout backfill start when `X_AUTO_SYNC` is enabled.
