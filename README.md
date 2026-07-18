# Manvinder Writing Desk

A local writing workspace for capturing X Articles or starting original drafts, editing them as structured documents, and publishing to Substack.

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

## Substack Publishing

`Save to Substack` creates or updates a real Substack draft. `Publish now` saves that same document, runs Substack's pre-publish check, publishes it to the web, and sends it to the configured audience after dashboard confirmation.

The connector makes authenticated background requests using the existing signed-in Substack session stored in the local Chrome profile. It reads the encrypted local cookie store but never opens, focuses, or automates Chrome. Session values are not written to project files. Set `SUBSTACK_PUBLICATION_URL` explicitly or provide `SUBSTACK_FEED_URL`, from which the publication origin is derived. `SUBSTACK_CHROME_COOKIE_PATH` can select a specific Chrome profile when needed.

Images are uploaded to Substack first. The dashboard block document is converted into Substack's current editor schema while preserving headings, lists, quotes, inline formatting, links, captions, and the cover image. Every write response is checked before the local pipeline is marked published.

## Service

The installed macOS LaunchAgent `com.manvinder.x-substack-bridge` starts the local server at login and keeps it running. Background X sync and rich-layout backfill start when `X_AUTO_SYNC` is enabled.
