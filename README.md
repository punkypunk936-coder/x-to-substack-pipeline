# X Article to Substack Draft

Core workflow:

`Watch @0xgoodie Articles -> deduplicate -> queue rich dashboard drafts -> edit/preview -> publish to Substack`

## Run

```sh
cd "/Users/manvinderarora/Documents/New project/project/x_substack_bridge"
python3 server.py --host 127.0.0.1 --port 8788
```

Open:

```txt
http://127.0.0.1:8788/
```

## How It Works

Paste a single X article/post URL and click `Create draft`.

The server checks `https://x.com/0xgoodie/articles` and Substack every 15 minutes. X status IDs are ordered chronologically and compared against a stored high-water mark, so newly posted articles are captured without backfilling older history. Substack reconciliation merges the live Archive API with RSS, avoiding the publication lag that can leave a newly published post absent from the feed. Matching uses exact titles first, then tightly guarded near-title matching with token, sequence, one-to-one, and publication-date checks. Published records move into a collapsed history section, while the newest active X draft opens immediately. The dashboard performs an additional quiet sync after opening and offers a manual `Sync all` control.

The dashboard is the primary article editor. It supports:

- Paragraph, heading, subheading, quote, bulleted list, numbered list, code, divider, image, and URL embed blocks.
- Bold, italic, underline, strikethrough, and inline links.
- Reordering and converting blocks without flattening the article back to plain text.
- Image upload or remote image insertion, plus alt text, captions, and regular/wide/full layouts.
- Local autosave, a faithful article preview, background Substack draft transfer, and direct publish.

Existing plain-text drafts are upgraded into ordered blocks when opened. The same sanitized block document powers local preview, RSS output, Substack draft transfer, and direct publishing, so formatting is not maintained in separate copies.

The installed macOS LaunchAgent `com.manvinder.x-substack-bridge` starts the local server at login and keeps it running. Publishing is never automatic: new items enter as drafts and require the dashboard's explicit `Publish now` confirmation.

The tool tries, in order:

1. An invisible worker window in the existing logged-in Chrome profile on macOS. This is the exact rich DOM path and preserves links, block formatting, and cover media.
2. Logged-in X browser extraction through a persistent local Playwright profile.
3. Official X API via `X_BEARER_TOKEN`, including article fields and media.
4. Public X embed extraction, only if it contains a real article body.
5. HTML metadata extraction, only if it contains a real article body.

Byline-only embeds, login walls, and metadata-only responses are rejected. The app should not save or display those as drafts.

Outputs:

- `draft.html`: rendered Substack-style draft preview.
- `feed.xml`: RSS import feed for Substack.
- `substack-import.csv`: CSV handoff.
- Rich, block-ordered HTML for Substack transfer.
- `Save to Substack`: saves the rich draft through the configured background Chrome connector.
- `Publish now`: sends the same saved dashboard version through Substack's final publish flow.

## Exact Capture

For exact long-form article body and media, set an X API bearer token before running:

```sh
export X_BEARER_TOKEN="..."
python3 server.py --host 127.0.0.1 --port 8788
```

Without `X_BEARER_TOKEN`, the app uses an invisible Chrome worker window so it can reuse the profile where X is already logged in without changing tabs or taking focus. Keep Chrome running in the signed-in profile. If you use multiple Chrome profiles, the worker follows the current Chrome profile.

If Chrome blocks direct extraction, enable `View > Developer > Allow JavaScript from Apple Events` once. You can set a different Chromium app name with:

```sh
export X_BROWSER_APP="Google Chrome"
```

The old visible clipboard fallback is disabled by default so automatic work never steals focus. It can be enabled explicitly for troubleshooting only:

```bash
export X_ALLOW_VISIBLE_CHROME_FALLBACK=1
```

The separate `.x-profile` browser is disabled by default. Only enable it explicitly if you want that fallback:

```sh
export X_ALLOW_ISOLATED_X_PROFILE=1
```

Do not paste X passwords into this app or chat.

Substack does not expose a stable official public draft-publish API. This tool prepares the draft package and import feed. Publishing/emailing subscribers should remain a deliberate final action.

## Publish Button Setup

The local Substack actions use Substack's normal web editor in your already-running, logged-in Chrome profile, but keep the dashboard in front. Editing and preview remain in the dashboard; Chrome is only the authenticated background bridge.

Set your Substack editor/dashboard URL before starting the server:

```sh
export SUBSTACK_EDITOR_URL="https://manvinder.substack.com/publish/post/new"
python3 server.py --host 127.0.0.1 --port 8788
```

Chrome must have `View > Developer > Allow JavaScript from Apple Events` enabled so the local connector can populate the editor. Optional isolated Playwright fallback:

```sh
export SUBSTACK_PROFILE_DIR="/Users/manvinderarora/.substack-publisher-profile"
export SUBSTACK_USE_PLAYWRIGHT=1
```

The isolated fallback may ask you to log in inside its Playwright browser profile. The default existing-Chrome path reuses your current Substack login.

Final live publishing is disabled unless this is set:

```sh
export SUBSTACK_AUTOPUBLISH=1
```

Without `SUBSTACK_AUTOPUBLISH=1`, the connector populates the Substack editor/draft and stops before the final publish/send step.
