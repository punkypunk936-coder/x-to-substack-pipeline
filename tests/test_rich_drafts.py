import base64
import json
import tempfile
import unittest
from pathlib import Path

import server


class RichDraftTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
