import base64
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
