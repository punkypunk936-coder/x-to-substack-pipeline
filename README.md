# Manvinder Writing Desk

An API-only local workspace for capturing X Articles, preserving their text, links, and media, then editing them as structured drafts.

## Run

```sh
cd "/Users/manvinderarora/Documents/New project/project/x_substack_bridge"
export X_BEARER_TOKEN="..."
python3 server.py --host 127.0.0.1 --port 8788
```

Open `http://127.0.0.1:8788/`.

## Capture

The ingest and background-sync paths use only the official X API. Browser automation, hidden windows, logged-in Chrome extraction, public embed fallbacks, and metadata fallbacks are disconnected from the server.

The X API request includes Article text and entities plus expanded cover and inline media. The parser:

- Preserves the source Article text before applying entity offsets.
- Reconstructs hyperlinks with their original visible text and expanded targets.
- Keeps the banner as the first wide image and retains referenced inline images.
- Refuses to create a draft when a reported link or media reference cannot be resolved.
- Preserves paragraph breaks while making no false claim that X exposes its complete editor block layout.

Set these values in `.env` for quiet background sync:

```sh
X_BEARER_TOKEN=...
X_ACCOUNT_HANDLE=@0xgoodie
X_AUTO_SYNC=1
X_SYNC_INTERVAL_SECONDS=900
```

Without `X_BEARER_TOKEN`, ingest is disabled with a clear status. The app never falls back to Chrome.

## Editing

The dashboard supports paragraph, heading, subheading, quote, list, code, divider, image, and URL embed blocks. It also supports inline formatting and links, image upload or remote image insertion, block reordering, autosave, and local preview.

Existing plain-text drafts are upgraded into ordered blocks when opened. The same sanitized block document powers preview, RSS, HTML, and CSV output.

Generated outputs:

- `dist/draft.html`
- `dist/feed.xml`
- `dist/substack-import.csv`

## Publishing Boundary

Substack's official Developer API and MCP connection are read-only; neither provides post creation or publishing. Browser publishing is intentionally disabled, so the dashboard will not open Chrome or report a successful Substack publish that it cannot verify.

Substack RSS and public archive reads remain available only to reconcile older pipeline records that were already published there. A truly browser-free Publish button needs a writable publication backend, such as a first-party writing site plus an email delivery API.

## Service

The installed macOS LaunchAgent `com.manvinder.x-substack-bridge` starts the local server at login and keeps it running. Background X sync starts only when `X_AUTO_SYNC` is enabled and `X_BEARER_TOKEN` is present.
