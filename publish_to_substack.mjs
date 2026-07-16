import { readFile } from "node:fs/promises";
import path from "node:path";
import { chromium } from "playwright";
import { draftBodyHtml, draftPlainText, imageCount } from "./rich_draft.mjs";

const payloadPath = process.argv[2];
const editorUrl = process.env.SUBSTACK_EDITOR_URL || process.env.SUBSTACK_PUBLISH_URL || "";
const profileDir = process.env.SUBSTACK_PROFILE_DIR || path.join(process.cwd(), ".substack-profile");
const confirmPublish = process.env.SUBSTACK_CONFIRM_PUBLISH === "1";
const allowAutopublish = process.env.SUBSTACK_AUTOPUBLISH === "1";

function finish(result, code = 0) {
  console.log(JSON.stringify(result));
  process.exit(code);
}

async function count(locator) {
  try {
    return await locator.count();
  } catch {
    return 0;
  }
}

async function firstVisible(page, selectors) {
  for (const selector of selectors) {
    const locator = page.locator(selector);
    const n = await count(locator);
    for (let i = 0; i < Math.min(n, 6); i += 1) {
      const item = locator.nth(i);
      try {
        if (await item.isVisible({ timeout: 400 })) return item;
      } catch {}
    }
  }
  return null;
}

async function clickText(page, texts) {
  for (const text of texts) {
    const button = page.getByText(text, { exact: false });
    const n = await count(button);
    for (let i = 0; i < Math.min(n, 6); i += 1) {
      const item = button.nth(i);
      try {
        if (await item.isVisible({ timeout: 500 }) && await item.isEnabled({ timeout: 500 })) {
          await item.click({ timeout: 3000 });
          return true;
        }
      } catch {}
    }
  }
  return false;
}

if (!payloadPath) finish({ ok: false, status: "missing_payload", message: "Missing publish payload path." }, 2);
if (!editorUrl) finish({ ok: false, status: "setup_required", message: "Set SUBSTACK_EDITOR_URL before publishing." }, 2);

const draft = JSON.parse(await readFile(payloadPath, "utf-8"));
const browser = await chromium.launchPersistentContext(profileDir, {
  headless: false,
  viewport: { width: 1440, height: 1000 },
});

try {
  const page = browser.pages()[0] || await browser.newPage();
  await page.goto(editorUrl, { waitUntil: "domcontentloaded", timeout: 60000 });
  await page.waitForTimeout(2500);

  const url = page.url();
  const bodyText = (await page.locator("body").innerText({ timeout: 5000 }).catch(() => "")).toLowerCase();
  if (/sign.?in|login/.test(url) || bodyText.includes("sign in") || bodyText.includes("log in")) {
    finish({
      ok: false,
      status: "login_required",
      message: `Log into Substack in the Playwright profile at ${profileDir}, then click Publish again.`,
      profile_dir: profileDir,
      current_url: url,
    }, 3);
  }

  await clickText(page, ["Create", "New post", "New Posts", "Article", "Text post"]);
  await page.waitForTimeout(1500);

  const titleBox = await firstVisible(page, [
    "textarea[placeholder*='Title' i]",
    "input[placeholder*='Title' i]",
    "[contenteditable='true'][aria-label*='Title' i]",
    "[data-testid*='title' i]",
  ]);
  if (titleBox) {
    await titleBox.click({ timeout: 3000 });
    await page.keyboard.press(process.platform === "darwin" ? "Meta+A" : "Control+A");
    await page.keyboard.type(draft.title || "Untitled X article", { delay: 1 });
  }

  const subtitleBox = await firstVisible(page, [
    "textarea[placeholder*='Subtitle' i]",
    "input[placeholder*='Subtitle' i]",
    "[contenteditable='true'][aria-label*='Subtitle' i]",
    "[data-testid*='subtitle' i]",
  ]);
  if (subtitleBox && draft.subtitle) {
    await subtitleBox.click({ timeout: 3000 });
    await page.keyboard.type(draft.subtitle, { delay: 1 });
  }

  await page.evaluate(async ({ html, text }) => {
    const item = new ClipboardItem({
      "text/html": new Blob([html], { type: "text/html" }),
      "text/plain": new Blob([text], { type: "text/plain" }),
    });
    await navigator.clipboard.write([item]);
  }, { html: draftBodyHtml(draft), text: draftPlainText(draft) }).catch(async () => {
    await page.context().grantPermissions(["clipboard-read", "clipboard-write"]).catch(() => {});
    await page.evaluate(async (text) => navigator.clipboard.writeText(text), draftPlainText(draft));
  });

  const bodyBox = await firstVisible(page, [
    "[contenteditable='true'][aria-label*='Body' i]",
    "[contenteditable='true'][aria-label*='post' i]",
    ".ProseMirror",
    "[contenteditable='true']",
  ]);
  if (!bodyBox) {
    finish({
      ok: false,
      status: "editor_not_found",
      message: "Opened Substack, but could not find the editor body. The rich draft is on the clipboard.",
      current_url: page.url(),
    }, 4);
  }
  await bodyBox.click({ timeout: 5000 });
  await page.keyboard.press(process.platform === "darwin" ? "Meta+V" : "Control+V");
  await page.waitForTimeout(7000);

  const expectedImages = imageCount(draft);
  const transferredImages = await bodyBox.locator("img").count().catch(() => 0);
  if (expectedImages > 0 && transferredImages < expectedImages) {
    finish({
      ok: false,
      status: "media_transfer_incomplete",
      message: "Substack did not accept every image, so publishing was stopped before anything went live.",
      expected_images: expectedImages,
      image_count: transferredImages,
      current_url: page.url(),
    }, 4);
  }

  if (!(confirmPublish && allowAutopublish)) {
    finish({
      ok: true,
      status: "draft_populated",
      message: "Rich Substack draft saved in the background. Keep editing or publish from the dashboard.",
      current_url: page.url(),
      profile_dir: profileDir,
    });
  }

  const continued = await clickText(page, ["Continue", "Next"]);
  if (continued) await page.waitForTimeout(2000);
  const published = await clickText(page, ["Publish now", "Publish", "Send"]);
  await page.waitForTimeout(3000);

  finish({
    ok: published,
    status: published ? "published" : "publish_button_not_found",
    message: published ? "Publish click sent to Substack." : "Draft populated, but final publish button was not found.",
    current_url: page.url(),
  }, published ? 0 : 5);
} catch (error) {
  finish({ ok: false, status: "publish_failed", message: String(error?.message || error) }, 6);
} finally {
  await browser.close().catch(() => {});
}
