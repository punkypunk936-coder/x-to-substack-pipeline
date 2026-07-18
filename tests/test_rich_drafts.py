import base64
import json
import os
import tempfile
import unittest
from pathlib import Path

import server


class RichDraftTests(unittest.TestCase):
    def test_original_draft_can_start_empty_and_keeps_a_stable_pipeline_id(self) -> None:
        draft = server.build_original_draft()

        self.assertEqual(draft["source"], "original")
        self.assertEqual(draft["blocks"][0]["type"], "paragraph")
        self.assertTrue(server.is_draft_usable(draft))
        self.assertEqual(server.pipeline_draft_id(draft), draft["id"])

    def test_original_drafts_are_ignored_by_x_layout_backfill(self) -> None:
        previous_pipeline_path = server.DRAFT_PIPELINE_JSON
        try:
            with tempfile.TemporaryDirectory() as directory:
                server.DRAFT_PIPELINE_JSON = Path(directory) / "pipeline.json"
                draft = server.build_original_draft()
                server.DRAFT_PIPELINE_JSON.write_text(json.dumps({"selected_id": draft["id"], "items": [draft]}))

                result = server.backfill_pipeline_drafts(
                    "http://127.0.0.1:8788",
                    fetcher=lambda _url: self.fail("Original drafts must not enter X backfill."),
                )

                self.assertEqual(result["upgraded"], [])
                self.assertEqual(result["skipped"], [])
        finally:
            server.DRAFT_PIPELINE_JSON = previous_pipeline_path

    def test_official_x_article_preserves_banner_links_and_inline_media(self) -> None:
        body = "Read the linked words exactly as written.\n\nThe second paragraph keeps the article structure intact."
        link_text = "linked words"
        link_start = body.index(link_text)
        payload = {
            "data": {
                "id": "1234567890123456789",
                "created_at": "2026-07-17T08:30:00.000Z",
                "article": {
                    "title": "Exact API capture",
                    "subtitle": "Text, links, and media",
                    "text": body,
                    "entities": {
                        "urls": [
                            {
                                "start": link_start,
                                "end": link_start + len(link_text),
                                "expanded_url": "https://example.com/source?ref=x",
                            }
                        ]
                    },
                    "cover_media": "3_cover",
                    "media_entities": ["3_inline"],
                },
            },
            "includes": {
                "media": [
                    {
                        "media_key": "3_cover",
                        "type": "photo",
                        "url": "https://pbs.twimg.com/media/COVER?format=jpg&name=large",
                        "alt_text": "Article banner",
                    },
                    {
                        "media_key": "3_inline",
                        "type": "photo",
                        "url": "https://pbs.twimg.com/media/INLINE?format=png&name=large",
                        "alt_text": "Inline chart",
                    },
                ]
            },
        }

        draft = server.draft_from_x_payload("https://x.com/0xgoodie/status/1234567890123456789", payload)

        self.assertIsNotNone(draft)
        assert draft is not None
        self.assertEqual([block["type"] for block in draft["blocks"]], ["image", "paragraph", "paragraph", "image"])
        self.assertEqual(draft["blocks"][0]["layout"], "wide")
        self.assertEqual(draft["blocks"][0]["alt"], "Article banner")
        self.assertEqual(draft["blocks"][-1]["alt"], "Inline chart")
        self.assertIn(
            '<a href="https://example.com/source?ref=x" target="_blank" rel="noopener noreferrer">linked words</a>',
            draft["blocks"][1]["html"],
        )
        self.assertNotIn("display_url", draft["blocks"][1]["html"])
        self.assertFalse(draft["fidelity"]["browser_used"])

    def test_official_x_article_rejects_unresolved_media(self) -> None:
        payload = {
            "data": {
                "article": {
                    "title": "Incomplete media response",
                    "text": "This article has enough exact source words to exercise strict media validation without creating a partial draft.",
                    "cover_media": "3_missing",
                }
            },
            "includes": {"media": []},
        }

        with self.assertRaisesRegex(ValueError, "unresolved Article media"):
            server.draft_from_x_payload("https://x.com/0xgoodie/status/1234567890123456789", payload)

    def test_build_draft_uses_the_rich_read_api_without_an_x_token(self) -> None:
        previous_token = os.environ.pop("X_BEARER_TOKEN", None)
        previous_rich_api = server.draft_from_fxtwitter_api
        previous_official_api = server.draft_from_x_api
        try:
            server.draft_from_fxtwitter_api = lambda url: {
                "url": url,
                "title": "Rich API article",
                "body": "This complete rich API article contains enough exact words to pass validation without an X token or any browser fallback. It preserves every heading, list, quote, link, image, caption, and divider while keeping the article ready for careful editing inside the local writing dashboard.",
                "blocks": [
                    {
                        "type": "paragraph",
                        "html": "This complete rich API article contains enough exact words to pass validation without an X token or any browser fallback. It preserves every heading, list, quote, link, image, caption, and divider while keeping the article ready for careful editing inside the local writing dashboard.",
                    }
                ],
                "media": [],
            }
            server.draft_from_x_api = lambda _url: self.fail("The lower-fidelity API should not run")

            draft = server.build_draft("https://x.com/0xgoodie/status/1234567890123456789")

            self.assertEqual(draft["title"], "Rich API article")
        finally:
            server.draft_from_fxtwitter_api = previous_rich_api
            server.draft_from_x_api = previous_official_api
            if previous_token is not None:
                os.environ["X_BEARER_TOKEN"] = previous_token

    def test_draftjs_article_preserves_rich_layout_unicode_links_and_media_positions(self) -> None:
        payload = {
            "code": 200,
            "status": {
                "created_at": "Fri Jul 17 03:00:46 +0000 2026",
                "article": {
                    "title": "Rich Draft.js article",
                    "cover_media": {
                        "media_id": "cover",
                        "media_info": {
                            "original_img_url": "https://pbs.twimg.com/media/COVER.jpg",
                            "original_img_width": 1200,
                            "original_img_height": 480,
                        },
                    },
                    "media_entities": [
                        {
                            "media_id": "inline",
                            "media_info": {
                                "original_img_url": "https://pbs.twimg.com/media/INLINE.png",
                                "original_img_width": 800,
                                "original_img_height": 600,
                            },
                        }
                    ],
                    "content": {
                        "entityMap": [
                            {
                                "key": "0",
                                "value": {
                                    "type": "LINK",
                                    "data": {"url": "https://example.com/exact"},
                                },
                            },
                            {
                                "key": "1",
                                "value": {
                                    "type": "MEDIA",
                                    "data": {
                                        "caption": "Inline caption",
                                        "mediaItems": [{"mediaId": "inline"}],
                                    },
                                },
                            },
                            {"key": "2", "value": {"type": "DIVIDER", "data": {}}},
                        ],
                        "blocks": [
                            {
                                "key": "p1",
                                "type": "unstyled",
                                "text": "🚀 linked bold",
                                "inlineStyleRanges": [
                                    {"offset": 3, "length": 6, "style": "Bold"},
                                    {"offset": 10, "length": 4, "style": "Italic"},
                                ],
                                "entityRanges": [{"offset": 3, "length": 6, "key": 0}],
                            },
                            {"key": "h1", "type": "header-two", "text": "Heading", "inlineStyleRanges": [], "entityRanges": []},
                            {"key": "l1", "type": "unordered-list-item", "text": "First", "inlineStyleRanges": [], "entityRanges": []},
                            {"key": "l2", "type": "unordered-list-item", "text": "Second", "inlineStyleRanges": [], "entityRanges": []},
                            {"key": "m1", "type": "atomic", "text": " ", "inlineStyleRanges": [], "entityRanges": [{"offset": 0, "length": 1, "key": 1}]},
                            {"key": "d1", "type": "atomic", "text": " ", "inlineStyleRanges": [], "entityRanges": [{"offset": 0, "length": 1, "key": 2}]},
                            {"key": "q1", "type": "blockquote", "text": "Quoted", "inlineStyleRanges": [], "entityRanges": []},
                        ],
                    },
                },
            },
        }

        draft = server.draft_from_fxtwitter_payload("https://x.com/0xgoodie/status/1234567890123456789", payload)

        self.assertIsNotNone(draft)
        assert draft is not None
        self.assertEqual(
            [block["type"] for block in draft["blocks"]],
            ["image", "paragraph", "heading", "bullet_list", "image", "divider", "quote"],
        )
        self.assertEqual(draft["blocks"][0]["layout"], "wide")
        self.assertEqual(draft["blocks"][4]["caption"], "Inline caption")
        self.assertIn(
            '<a href="https://example.com/exact" target="_blank" rel="noopener noreferrer"><strong>linked</strong></a>',
            draft["blocks"][1]["html"],
        )
        self.assertIn("<em>bold</em>", draft["blocks"][1]["html"])
        self.assertEqual(draft["blocks"][3]["items"], ["First", "Second"])
        self.assertTrue(draft["fidelity"]["rich_layout"])

    def test_rich_backfill_preserves_workflow_metadata(self) -> None:
        previous_pipeline_path = server.DRAFT_PIPELINE_JSON
        previous_backup_dir = server.PIPELINE_BACKUP_DIR
        try:
            with tempfile.TemporaryDirectory() as directory:
                server.DRAFT_PIPELINE_JSON = Path(directory) / "pipeline.json"
                server.PIPELINE_BACKUP_DIR = Path(directory) / "backups"
                article_copy = "This exact article copy contains enough words to verify a safe rich-layout migration for an existing pipeline record."
                server.DRAFT_PIPELINE_JSON.write_text(
                    json.dumps(
                        {
                            "selected_id": None,
                            "items": [
                                {
                                    "id": "1234567890123456789",
                                    "url": "https://x.com/0xgoodie/status/1234567890123456789",
                                    "title": "Existing article",
                                    "body": article_copy,
                                    "status": "published",
                                    "substack_url": "https://example.substack.com/p/existing",
                                    "published_at": "2026-07-17T03:00:46+00:00",
                                }
                            ],
                        }
                    )
                )
                replacement = {
                    "url": "https://x.com/0xgoodie/status/1234567890123456789",
                    "title": "Existing article",
                    "body": article_copy,
                    "blocks": [{"type": "paragraph", "html": article_copy}],
                    "media": [],
                    "source": "fxtwitter_read_api",
                    "extraction_version": server.RICH_EXTRACTION_VERSION,
                }

                result = server.backfill_pipeline_drafts("http://127.0.0.1:8788", fetcher=lambda _url: replacement)
                migrated = json.loads(server.DRAFT_PIPELINE_JSON.read_text())["items"][0]

                self.assertEqual(result["upgraded"], ["1234567890123456789"])
                self.assertEqual(migrated["extraction_version"], server.RICH_EXTRACTION_VERSION)
                self.assertEqual(migrated["status"], "published")
                self.assertEqual(migrated["substack_url"], "https://example.substack.com/p/existing")
                self.assertTrue(Path(result["backup_path"]).exists())
        finally:
            server.DRAFT_PIPELINE_JSON = previous_pipeline_path
            server.PIPELINE_BACKUP_DIR = previous_backup_dir

    def test_rich_article_discovery_uses_article_titles_and_status_ids(self) -> None:
        payload = {
            "code": 200,
            "results": [
                {
                    "id": "1234567890123456790",
                    "url": "https://x.com/0xgoodie/status/1234567890123456790",
                    "article": {"title": "Newer article"},
                },
                {
                    "id": "1234567890123456789",
                    "article": {"title": "Older article"},
                },
                {"id": "1234567890123456788", "text": "Regular post"},
            ],
        }

        articles = server.discover_fxtwitter_articles_payload(payload, "0xgoodie")

        self.assertEqual([item["id"] for item in articles], ["1234567890123456790", "1234567890123456789"])
        self.assertEqual(articles[1]["url"], "https://x.com/0xgoodie/status/1234567890123456789")

    def test_substack_publish_connection_is_api_only_and_unavailable(self) -> None:
        config = server.publish_config()

        self.assertEqual(config["mode"], "api_only")
        self.assertFalse(config["browser_automation"])
        self.assertFalse(config["write_api_available"])
        self.assertFalse(config["configured"])

    def test_legacy_draft_becomes_ordered_blocks(self) -> None:
        draft = {
            "body": "Opening paragraph with enough words to remain useful.\n\n## A real heading\n\nClosing paragraph.",
            "media": [{"type": "image", "url": "https://example.com/chart.png", "alt": "Chart"}],
        }

        blocks = server.normalize_blocks(draft)

        self.assertEqual([block["type"] for block in blocks], ["paragraph", "heading", "paragraph", "image"])
        self.assertEqual(blocks[-1]["url"], "https://example.com/chart.png")

    def test_inline_html_is_sanitized(self) -> None:
        block = server.normalize_block(
            {
                "type": "paragraph",
                "html": '<strong>Keep</strong><script>drop()</script><a href="javascript:alert(1)">link</a>',
            }
        )

        self.assertIsNotNone(block)
        self.assertIn("<strong>Keep</strong>", block["html"])
        self.assertNotIn("<script", block["html"])
        self.assertNotIn("javascript:", block["html"])

    def test_blocks_render_substack_compatible_html(self) -> None:
        blocks = server.normalize_blocks(
            {
                "blocks": [
                    {"type": "heading", "html": "Market structure"},
                    {"type": "paragraph", "html": "A <strong>clear</strong> thesis."},
                    {"type": "bullet_list", "items": ["First", "Second"]},
                    {"type": "pull_quote", "html": "The line worth remembering."},
                    {"type": "quote", "html": "Hold the line."},
                    {"type": "divider"},
                    {
                        "type": "image",
                        "url": "https://example.com/chart.png",
                        "alt": "Price chart",
                        "caption": "The setup",
                        "layout": "wide",
                    },
                ]
            }
        )

        rendered = server.blocks_to_html(blocks)

        self.assertIn("<h2>Market structure</h2>", rendered)
        self.assertIn("<ul><li>First</li><li>Second</li></ul>", rendered)
        self.assertIn('<blockquote class="pull-quote"><p>The line worth remembering.</p></blockquote>', rendered)
        self.assertIn("<blockquote><p>Hold the line.</p></blockquote>", rendered)
        self.assertIn('figure data-layout="wide"', rendered)
        self.assertIn("<figcaption>The setup</figcaption>", rendered)

    def test_x_profile_images_are_not_article_blocks(self) -> None:
        blocks = server.normalize_blocks(
            {
                "blocks": [
                    {"type": "paragraph", "html": "Article copy"},
                    {
                        "type": "image",
                        "url": "https://pbs.twimg.com/profile_images/123/avatar_x96.jpg",
                        "alt": "Author avatar",
                    },
                    {
                        "type": "image",
                        "url": "https://pbs.twimg.com/media/ABC123?format=jpg&name=large",
                        "alt": "Article chart",
                    },
                ]
            }
        )

        self.assertEqual([block["type"] for block in blocks], ["paragraph", "image"])
        self.assertIn("/media/ABC123", blocks[1]["url"])

    def test_near_identical_cross_platform_titles_match(self) -> None:
        score = server.title_match_score(
            "Everyone’s a Trader Now and Everything Is Tradable",
            "Everyone’s a Trader and Everything Is Tradable",
        )

        self.assertGreaterEqual(score, 0.88)
        self.assertEqual(server.title_match_score("Trading apps", "India's tech-nomy"), 0.0)

    def test_archive_api_fills_rss_publication_lag(self) -> None:
        previous_feed_fetch = server.fetch_substack_feed_publications
        previous_archive_fetch = server.fetch_substack_archive_publications
        try:
            server.fetch_substack_feed_publications = lambda: [
                {
                    "title": "Older published post",
                    "url": "https://example.substack.com/p/older",
                    "published_at": "2026-07-15T08:00:00+00:00",
                    "source": "rss",
                }
            ]
            server.fetch_substack_archive_publications = lambda: [
                {
                    "title": "Newest published post",
                    "url": "https://example.substack.com/p/newest",
                    "published_at": "2026-07-16T08:22:11+00:00",
                    "source": "archive_api",
                },
                {
                    "title": "Older published post",
                    "url": "https://example.substack.com/p/older",
                    "published_at": "2026-07-15T08:00:00+00:00",
                    "source": "archive_api",
                },
            ]

            publications = server.fetch_substack_publications()

            self.assertEqual([post["title"] for post in publications], ["Newest published post", "Older published post"])
            self.assertEqual(publications[1]["source"], "archive_api")
        finally:
            server.fetch_substack_feed_publications = previous_feed_fetch
            server.fetch_substack_archive_publications = previous_archive_fetch

    def test_reconciliation_retires_published_selection(self) -> None:
        previous_pipeline_path = server.DRAFT_PIPELINE_JSON
        previous_fetch = server.fetch_substack_publications
        try:
            with tempfile.TemporaryDirectory() as directory:
                server.DRAFT_PIPELINE_JSON = Path(directory) / "pipeline.json"
                server.DRAFT_PIPELINE_JSON.write_text(
                    json.dumps(
                        {
                            "selected_id": "200",
                            "items": [
                                {
                                    "id": "200",
                                    "title": "Everyone’s a Trader Now and Everything Is Tradable",
                                    "date": "2026-07-16",
                                    "status": "draft",
                                },
                                {
                                    "id": "201",
                                    "title": "The next great trading app will help you trade less",
                                    "date": "2026-07-16",
                                    "status": "draft",
                                },
                            ],
                        }
                    )
                )
                server.fetch_substack_publications = lambda: [
                    {
                        "title": "Everyone’s a Trader and Everything Is Tradable",
                        "url": "https://manvinder.substack.com/p/everyones-a-trader",
                        "published_at": "2026-07-16T04:23:26+00:00",
                    }
                ]

                result = server.reconcile_substack_publications()
                pipeline = json.loads(server.DRAFT_PIPELINE_JSON.read_text())

                self.assertEqual(result["updated"], ["200"])
                self.assertEqual(pipeline["selected_id"], "201")
                self.assertEqual(pipeline["items"][0]["status"], "published")
                self.assertEqual(pipeline["items"][0]["substack_url"], "https://manvinder.substack.com/p/everyones-a-trader")
        finally:
            server.DRAFT_PIPELINE_JSON = previous_pipeline_path
            server.fetch_substack_publications = previous_fetch

    def test_reconciliation_does_not_reannounce_published_posts(self) -> None:
        previous_pipeline_path = server.DRAFT_PIPELINE_JSON
        previous_fetch = server.fetch_substack_publications
        try:
            with tempfile.TemporaryDirectory() as directory:
                server.DRAFT_PIPELINE_JSON = Path(directory) / "pipeline.json"
                server.DRAFT_PIPELINE_JSON.write_text(
                    json.dumps(
                        {
                            "selected_id": None,
                            "items": [
                                {
                                    "id": "200",
                                    "title": "Already published",
                                    "date": "2026-07-16",
                                    "status": "published",
                                    "substack_url": "https://example.substack.com/p/already-published",
                                    "published_at": "2026-07-16T08:22:11+00:00",
                                }
                            ],
                        }
                    )
                )
                server.fetch_substack_publications = lambda: [
                    {
                        "title": "Already published",
                        "url": "https://example.substack.com/p/already-published",
                        "published_at": "2026-07-16T08:22:11.316Z",
                        "source": "archive_api",
                    }
                ]

                result = server.reconcile_substack_publications()

                self.assertEqual(result["updated"], [])
                self.assertFalse(result["changed"])
        finally:
            server.DRAFT_PIPELINE_JSON = previous_pipeline_path
            server.fetch_substack_publications = previous_fetch

    def test_x_high_water_mark_accepts_only_newer_articles(self) -> None:
        existing = {"200", "195"}

        self.assertTrue(server.should_ingest_discovered_id("201", existing))
        self.assertFalse(server.should_ingest_discovered_id("199", existing))
        self.assertFalse(server.should_ingest_discovered_id("200", existing))
        self.assertTrue(server.should_ingest_discovered_id("199", existing, allow_backfill=True))

    def test_bootstrap_uses_the_pipeline_selection(self) -> None:
        previous_pipeline_path = server.DRAFT_PIPELINE_JSON
        previous_publish_result_path = server.PUBLISH_RESULT_JSON
        try:
            with tempfile.TemporaryDirectory() as directory:
                server.DRAFT_PIPELINE_JSON = Path(directory) / "pipeline.json"
                server.PUBLISH_RESULT_JSON = Path(directory) / "publish-result.json"
                server.DRAFT_PIPELINE_JSON.write_text(
                    json.dumps(
                        {
                            "selected_id": "newer",
                            "items": [
                                {
                                    "id": "older",
                                    "title": "Older draft",
                                    "body": (
                                        "This older article body contains enough detail to be considered a usable draft. "
                                        "It explains the setup, the market context, the core thesis, the risks involved, "
                                        "and the practical conclusion a reader should take away after reviewing it."
                                    ),
                                    "status": "draft",
                                },
                                {
                                    "id": "newer",
                                    "title": "Selected draft",
                                    "body": (
                                        "This selected article body contains enough detail to be considered a usable draft. "
                                        "It explains the setup, the market context, the core thesis, the risks involved, "
                                        "and the practical conclusion a reader should take away after reviewing it."
                                    ),
                                    "status": "draft",
                                },
                            ],
                        }
                    )
                )

                payload = server.bootstrap_payload()

                self.assertEqual(payload["draft"]["id"], "newer")
                self.assertEqual(payload["pipeline"]["selected_id"], "newer")
        finally:
            server.DRAFT_PIPELINE_JSON = previous_pipeline_path
            server.PUBLISH_RESULT_JSON = previous_publish_result_path

    def test_local_upload_is_embedded_only_in_publish_payload(self) -> None:
        previous_media_dir = server.MEDIA_DIR
        try:
            with tempfile.TemporaryDirectory() as directory:
                server.MEDIA_DIR = Path(directory)
                raw = b"small-image-payload"
                uploaded = server.save_media_upload(
                    {
                        "name": "chart.png",
                        "type": "image/png",
                        "data": base64.b64encode(raw).decode("ascii"),
                    }
                )
                draft = {
                    "body": "This is a sufficiently detailed article body with more than twelve words for the saved draft test.",
                    "blocks": [
                        {"type": "paragraph", "html": "This is a sufficiently detailed article body with more than twelve words for the saved draft test."},
                        {"type": "image", "url": uploaded["url"], "alt": "Chart"},
                    ],
                }

                ready = server.publish_ready_draft(draft)

                self.assertTrue(uploaded["url"].startswith("/media/"))
                self.assertTrue(ready["blocks"][1]["url"].startswith("data:image/png;base64,"))
                self.assertEqual(base64.b64decode(ready["blocks"][1]["url"].split(",", 1)[1]), raw)
        finally:
            server.MEDIA_DIR = previous_media_dir

    def test_remote_x_image_is_embedded_for_substack_paste(self) -> None:
        previous_download = server.download_remote_image
        try:
            server.download_remote_image = lambda url: "data:image/jpeg;base64,VEVTVA=="
            ready = server.publish_ready_draft(
                {
                    "blocks": [
                        {
                            "type": "image",
                            "url": "https://pbs.twimg.com/media/COVER?format=jpg&name=large",
                            "alt": "Cover",
                        }
                    ]
                }
            )

            self.assertEqual(ready["blocks"][0]["url"], "data:image/jpeg;base64,VEVTVA==")
        finally:
            server.download_remote_image = previous_download


if __name__ == "__main__":
    unittest.main()
