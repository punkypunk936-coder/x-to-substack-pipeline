import assert from "node:assert/strict";
import { chromium } from "playwright";
import { draftStructure } from "../rich_draft.mjs";
import { articleExtractionSource } from "../x_article_dom.mjs";

const browser = await chromium.launch({ headless: true });
try {
  const page = await browser.newPage();
  await page.setContent(`
    <main>
      <div data-testid="twitterArticleReadView">
        <div style="background-image: url(https://abs.twimg.com/media/COVER?format=jpg&name=medium)"></div>
        <div data-testid="twitter-article-title">Exact article</div>
        <div data-testid="twitterArticleRichTextView">
          <div data-testid="longformRichTextComponent">
            <div data-contents="true">
              <div><h2 data-block="true"><span data-text="true">Heading</span></h2></div>
              <div class="longform-unstyled" data-block="true"><div><span style="font-weight: bold"><span data-text="true">Bold</span></span><span data-text="true"> and </span><a href="https://example.com/source"><span style="font-style: italic"><span data-text="true">linked italic</span></span></a></div></div>
              <div class="longform-unstyled" data-block="true"><div><span role="link" data-expanded-url="https://example.com/role-link">role link</span></div></div>
              <blockquote data-block="true"><div><span data-text="true">Quoted line</span></div></blockquote>
              <ul><li data-block="true"><div>First</div></li><li data-block="true"><div>Second</div></li><li data-block="true"><div>Third</div></li></ul>
            </div>
          </div>
        </div>
      </div>
    </main>
  `);
  const extracted = JSON.parse(await page.evaluate(articleExtractionSource()));
  assert.equal(extracted.title, "Exact article");
  assert.deepEqual(extracted.blocks.map((block) => block.type), ["heading", "paragraph", "paragraph", "quote", "bullet_list"]);
  assert.equal(extracted.blocks[1].html, '<strong>Bold</strong> and <a href="https://example.com/source"><em>linked italic</em></a>');
  assert.equal(extracted.blocks[2].html, '<a href="https://example.com/role-link">role link</a>');
  assert.deepEqual(extracted.blocks[4].items, ["First", "Second", "Third"]);
  assert.match(extracted.cover_media.url, /abs\.twimg\.com\/media\/COVER/);
  assert.equal(extracted.cover_media.layout, "wide");
  assert.deepEqual(draftStructure({ blocks: [extracted.cover_media, ...extracted.blocks] }), {
    images: 1,
    links: 2,
    headings: 1,
    quotes: 1,
    list_items: 3,
    bold: 1,
    italic: 1,
  });
  console.log("X rich article DOM parser: ok");
} finally {
  await browser.close();
}
