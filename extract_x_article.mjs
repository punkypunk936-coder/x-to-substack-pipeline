import path from "node:path";
import { chromium } from "playwright";

const url = process.argv[2] || "";
const root = process.cwd();
const profileDir = process.env.X_PROFILE_DIR || path.join(root, ".x-profile");

function finish(result, code = 0) {
  console.log(JSON.stringify(result));
  process.exit(code);
}

function cleanText(value) {
  return String(value || "")
    .replace(/\r\n/g, "\n")
    .replace(/\r/g, "\n")
    .replace(/https:\/\/t\.co\/\S+/g, "")
    .replace(/\s+pic\.twitter\.com\/\S+/g, "")
    .replace(/[ \t]+\n/g, "\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

function wordCount(value) {
  return (String(value || "").match(/\b[\w'-]+\b/g) || []).length;
}

function isBylineOnly(value) {
  return /^[\u2013\u2014-]?\s*[\w .'-]+\s+\(@[\w_]+\)\s+[A-Za-z]+\s+\d{1,2},\s+\d{4}$/.test(cleanText(value));
}

function isLoginWall(value) {
  const lowered = cleanText(value).toLowerCase();
  return [
    "continue with phone",
    "email or username",
    "by continuing, you agree",
    "sign in to x",
    "log in to x",
  ].some((marker) => lowered.includes(marker));
}

function isUsable(body, media) {
  if (isLoginWall(body)) return false;
  if (isBylineOnly(body)) return false;
  if (wordCount(body) >= 35) return true;
  return media.length > 0 && wordCount(body) >= 12;
}

function titleFromText(text) {
  const lines = cleanText(text)
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean)
    .filter((line) => !/^(@|Follow|Subscribe|Sign in|Log in|Post$|Reply$|Repost$|Like$)/i.test(line));
  const title = lines.find((line) => wordCount(line) >= 4 && !isBylineOnly(line)) || lines[0] || "Untitled X article";
  return title.slice(0, 140);
}

if (!url || !/^https?:\/\/(www\.)?(x|twitter)\.com\//i.test(url)) {
  finish({ ok: false, status: "bad_url", message: "Pass a valid X/Twitter article URL." }, 2);
}

const browser = await chromium.launchPersistentContext(profileDir, {
  headless: false,
  viewport: { width: 1360, height: 980 },
});

try {
  const page = browser.pages()[0] || await browser.newPage();
  await page.goto(url, { waitUntil: "domcontentloaded", timeout: 60000 });
  await page.waitForTimeout(4500);

  const firstText = await page.locator("body").innerText({ timeout: 8000 }).catch(() => "");
  const firstUrl = page.url();
  if (/login|signin/i.test(firstUrl) || isLoginWall(firstText) || /phone, email, or username/i.test(firstText)) {
    finish({
      ok: false,
      status: "login_required",
      message: `Log into X in the browser profile at ${profileDir}, then paste the article link again.`,
      profile_dir: profileDir,
      current_url: firstUrl,
    }, 3);
  }

  await page.getByText("Show more", { exact: false }).click({ timeout: 1500 }).catch(() => {});
  await page.getByText("Read more", { exact: false }).click({ timeout: 1500 }).catch(() => {});
  await page.waitForTimeout(1500);

  const linkedArticle = await page.evaluate(() => {
    const anchors = Array.from(document.querySelectorAll("a[href]"));
    const hit = anchors.find((anchor) => /\/i\/article\/|\/article\//.test(anchor.getAttribute("href") || ""));
    return hit ? new URL(hit.getAttribute("href"), location.href).toString() : "";
  });
  if (linkedArticle && linkedArticle !== page.url()) {
    await page.goto(linkedArticle, { waitUntil: "domcontentloaded", timeout: 60000 });
    await page.waitForTimeout(3500);
  }

  await page.getByText("Show more", { exact: false }).click({ timeout: 1500 }).catch(() => {});
  await page.getByText("Read more", { exact: false }).click({ timeout: 1500 }).catch(() => {});
  await page.waitForTimeout(1500);

  const extracted = await page.evaluate(() => {
    const noise = [
      /^Home$/, /^Explore$/, /^Notifications$/, /^Messages$/, /^Grok$/, /^Bookmarks$/, /^Communities$/,
      /^Premium$/, /^Profile$/, /^More$/, /^Post$/, /^Reply$/, /^Repost$/, /^Like$/, /^View post analytics$/,
      /^Share post$/, /^Follow$/, /^Subscribe$/, /^Sign in$/, /^Log in$/, /^Relevant people$/,
      /^What.s happening$/, /^Terms of Service$/, /^Privacy Policy$/, /^Cookie Policy$/,
    ];

    const visible = (node) => {
      const style = window.getComputedStyle(node);
      const rect = node.getBoundingClientRect();
      return style.visibility !== "hidden" && style.display !== "none" && rect.width > 0 && rect.height > 0;
    };

    const clean = (text) => String(text || "")
      .replace(/\r\n/g, "\n")
      .replace(/\r/g, "\n")
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean)
      .filter((line, index, lines) => lines.indexOf(line) === index)
      .filter((line) => !noise.some((pattern) => pattern.test(line)))
      .join("\n");

    const candidates = Array.from(document.querySelectorAll("article, main, [data-testid='tweetText'], [data-testid='cellInnerDiv']"))
      .filter(visible)
      .map((node) => ({
        text: clean(node.innerText || node.textContent || ""),
        selector: node.tagName.toLowerCase(),
      }))
      .filter((item) => item.text.length > 0)
      .sort((a, b) => b.text.length - a.text.length);

    const main = candidates[0] || { text: clean(document.body.innerText || ""), selector: "body" };
    const media = Array.from(document.querySelectorAll("article img[src], main img[src]"))
      .map((img) => ({
        type: "image",
        url: img.currentSrc || img.src,
        alt: img.alt || "X media",
        width: String(img.naturalWidth || ""),
        height: String(img.naturalHeight || ""),
      }))
      .filter((item) => /pbs\.twimg\.com\/media|ton\.twitter\.com|twimg\.com/.test(item.url))
      .filter((item, index, items) => items.findIndex((other) => other.url === item.url) === index);

    const title = document.querySelector("h1")?.innerText || document.title.replace(/\s*\/\s*X$/, "");
    return {
      body: main.text,
      media,
      title,
      current_url: location.href,
      candidate_count: candidates.length,
    };
  });

  const body = cleanText(extracted.body);
  const media = extracted.media || [];
  if (!isUsable(body, media)) {
    const loginMessage = isLoginWall(body)
      ? `Log into X in the browser profile at ${profileDir}, then paste the article link again.`
      : "X loaded, but the article body was not visible/capturable. Open the X browser window, log in if needed, expand the article, then try again.";
    finish({
      ok: false,
      status: isLoginWall(body) ? "login_required" : "weak_capture",
      message: loginMessage,
      profile_dir: profileDir,
      current_url: extracted.current_url,
      word_count: wordCount(body),
      preview: body.slice(0, 220),
    }, 4);
  }

  finish({
    ok: true,
    status: "captured",
    draft: {
      url,
      title: titleFromText(extracted.title && wordCount(extracted.title) >= 3 ? extracted.title : body),
      subtitle: "",
      date: new Date().toISOString().slice(0, 10),
      body,
      media,
      source: "x_logged_in_browser",
      warnings: [],
    },
  });
} catch (error) {
  finish({ ok: false, status: "extract_failed", message: String(error?.message || error), profile_dir: profileDir }, 5);
} finally {
  await browser.close().catch(() => {});
}
