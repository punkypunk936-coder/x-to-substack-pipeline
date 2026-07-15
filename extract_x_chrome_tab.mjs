import { execFile } from "node:child_process";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);
const url = process.argv[2] || "";
const browserApp = process.env.X_BROWSER_APP || "Google Chrome";

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

function statusId(value) {
  const match = String(value || "").match(/\/status(?:es)?\/(\d+)/);
  return match ? match[1] : "";
}

function sameXTarget(requestedUrl, currentUrl) {
  const requestedId = statusId(requestedUrl);
  if (requestedId) return String(currentUrl || "").includes(requestedId);
  return String(currentUrl || "").split("?")[0] === String(requestedUrl || "").split("?")[0];
}

function titleFromText(text) {
  const xDocumentTitle = cleanText(text).match(/on X:\s*["“](.+?)["”](?:\s*\/\s*X)?$/i);
  if (xDocumentTitle?.[1]) return xDocumentTitle[1].trim().slice(0, 140);
  const lines = cleanText(text)
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean)
    .filter((line) => !/^(@|Follow|Subscribe|Sign in|Log in|Post$|Reply$|Repost$|Like$)/i.test(line));
  const title = lines.find((line) => wordCount(line) >= 4 && !isBylineOnly(line)) || lines[0] || "Untitled X article";
  return title.slice(0, 140);
}

function removeChromeXNoise(value) {
  const lines = cleanText(value)
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
  if (!lines.length) return "";

  const handleIndex = lines.findIndex((line, index) => {
    return /^@[A-Za-z0-9_]{1,20}$/.test(line) && index > 0;
  });
  let start = 0;
  if (handleIndex >= 0 && handleIndex + 1 < lines.length) {
    start = handleIndex + 1;
  } else {
    const articleIndex = lines.findIndex((line) => /^Article$/i.test(line));
    if (articleIndex >= 0) start = articleIndex + 1;
  }

  let end = lines.length;
  const footerIndex = lines.findIndex((line, index) => {
    if (index <= start) return false;
    return (
      /^\d{1,2}:\d{2}\s*(AM|PM)\s*·/i.test(line) ||
      /^Relevant people$/i.test(line) ||
      /^Trending now$/i.test(line) ||
      /^What's happening$/i.test(line) ||
      /^Terms$/i.test(line)
    );
  });
  if (footerIndex >= 0) end = footerIndex;

  const noise = [
    /^To view keyboard shortcuts/i,
    /^View keyboard shortcuts$/i,
    /^Article$/i,
    /^See new posts$/i,
    /^Conversation$/i,
    /^Show more$/i,
    /^in the arena, doing things$/i,
  ];
  const cleaned = lines
    .slice(start, end)
    .filter((line) => !noise.some((pattern) => pattern.test(line)))
  while (cleaned.length && (
    /^[·•]$/.test(cleaned[0]) ||
    /^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2}$/i.test(cleaned[0]) ||
    /^\d+[smhdw]$/i.test(cleaned[0]) ||
    /^\d[\d,.KMB]*$/i.test(cleaned[0]) ||
    /^Views?$/i.test(cleaned[0])
  )) cleaned.shift();
  return cleaned.join("\n").trim();
}

function appleScript(appName) {
  return `
on run argv
  set targetUrl to item 1 of argv
  set jsPath to item 2 of argv
  set jsSource to read POSIX file jsPath as text
  tell application ${JSON.stringify(appName)}
    if (count of windows) = 0 then
      activate
      make new window
    end if
    set targetWindow to front window
    set targetTab to make new tab at end of tabs of targetWindow with properties {URL:targetUrl}
    repeat with i from 1 to 100
      delay 0.25
      if (loading of targetTab is false) then exit repeat
    end repeat
    delay 2
    try
      execute targetTab javascript "Array.from(document.querySelectorAll('span, div, button')).find((el) => /^(Show more|Read more)$/i.test((el.innerText || '').trim()))?.click();"
    end try
    delay 1
    set resultText to execute targetTab javascript jsSource
    close targetTab
    return resultText
  end tell
end run
`;
}

function clipboardAppleScript(appName) {
  return `
on run argv
  set targetUrl to item 1 of argv
  tell application ${JSON.stringify(appName)}
    activate
    if (count of windows) = 0 then
      make new window
    end if
    set targetWindow to front window
    set targetTab to make new tab at end of tabs of targetWindow with properties {URL:targetUrl}
    set active tab index of targetWindow to (count of tabs of targetWindow)
    repeat with i from 1 to 120
      delay 0.25
      if (loading of targetTab is false) then exit repeat
    end repeat
    delay 2
  end tell
  tell application "System Events"
    tell process ${JSON.stringify(appName)}
      key code 53
      delay 0.2
      keystroke "a" using command down
      delay 0.2
      keystroke "c" using command down
    end tell
  end tell
  delay 0.8
  set copiedText to the clipboard as text
  tell application ${JSON.stringify(appName)}
    set currentUrl to URL of active tab of front window
  end tell
  return currentUrl & linefeed & "-----X_SUBSTACK_CLIPBOARD_BODY-----" & linefeed & copiedText
end run
`;
}

const extractionJs = String.raw`
(() => {
  const clean = (text) => String(text || "")
    .replace(/\r\n/g, "\n")
    .replace(/\r/g, "\n")
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean)
    .filter((line, index, lines) => lines.indexOf(line) === index)
    .filter((line) => ![
      /^Home$/, /^Explore$/, /^Notifications$/, /^Messages$/, /^Grok$/, /^Bookmarks$/, /^Communities$/,
      /^Premium$/, /^Profile$/, /^More$/, /^Post$/, /^Reply$/, /^Repost$/, /^Like$/, /^View post analytics$/,
      /^Share post$/, /^Follow$/, /^Subscribe$/, /^Sign in$/, /^Log in$/, /^Relevant people$/,
      /^What.s happening$/, /^Terms of Service$/, /^Privacy Policy$/, /^Cookie Policy$/,
    ].some((pattern) => pattern.test(line)))
    .join("\n");

  const visible = (node) => {
    const style = window.getComputedStyle(node);
    const rect = node.getBoundingClientRect();
    return style.visibility !== "hidden" && style.display !== "none" && rect.width > 0 && rect.height > 0;
  };

  const articleLink = Array.from(document.querySelectorAll("a[href]"))
    .find((anchor) => /\/i\/article\/|\/article\//.test(anchor.getAttribute("href") || ""));
  const candidates = Array.from(document.querySelectorAll("article, main, [data-testid='tweetText'], [data-testid='cellInnerDiv']"))
    .filter(visible)
    .map((node) => clean(node.innerText || node.textContent || ""))
    .filter(Boolean)
    .sort((a, b) => b.length - a.length);
  const body = candidates[0] || clean(document.body.innerText || "");
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

  return JSON.stringify({
    body,
    media,
    title: articleLink?.innerText || document.querySelector("h1")?.innerText || document.title.replace(/\s*\/\s*X$/, ""),
    current_url: location.href,
    article_url: articleLink ? new URL(articleLink.getAttribute("href"), location.href).toString() : "",
  });
})()
`;

if (!url || !/^https?:\/\/(www\.)?(x|twitter)\.com\//i.test(url)) {
  finish({ ok: false, status: "bad_url", message: "Pass a valid X/Twitter article URL." }, 2);
}

async function extractFromChromeTab(targetUrl, jsPath) {
  const { stdout } = await execFileAsync("osascript", ["-e", appleScript(browserApp), targetUrl, jsPath], {
    timeout: 45000,
    maxBuffer: 1024 * 1024 * 5,
  });
  return JSON.parse(stdout.trim());
}

async function extractFromChromeClipboard(targetUrl) {
  const { stdout } = await execFileAsync("osascript", ["-e", clipboardAppleScript(browserApp), targetUrl], {
    timeout: 45000,
    maxBuffer: 1024 * 1024 * 5,
  });
  const marker = "-----X_SUBSTACK_CLIPBOARD_BODY-----";
  const [currentUrl = "", ...bodyParts] = stdout.split(marker);
  return {
    body: bodyParts.join(marker).trim(),
    media: [],
    title: "",
    current_url: currentUrl.trim(),
    article_url: "",
  };
}

function resultFromParsed(parsed, source) {
  const body = removeChromeXNoise(parsed.body);
  const media = parsed.media || [];
  if (!isUsable(body, media)) {
    return {
      ok: false,
      status: isLoginWall(body) ? "login_required" : "weak_capture",
      message: isLoginWall(body)
        ? "The Chrome tab is not logged into X in the active profile/window. Bring the logged-in Chrome profile window to front, then paste the link again."
        : "Chrome opened X, but the article body was not visible/capturable. Expand the article in that Chrome tab, then paste the final article URL.",
      current_url: parsed.current_url,
      word_count: wordCount(body),
      preview: body.slice(0, 220),
    };
  }
  return {
    ok: true,
    status: source === "existing_chrome_clipboard" ? "captured_existing_chrome_clipboard" : "captured_existing_chrome",
    draft: {
      url,
      title: titleFromText(parsed.title && wordCount(parsed.title) >= 3 ? parsed.title : body),
      subtitle: "",
      date: new Date().toISOString().slice(0, 10),
      body,
      media,
      source,
      warnings: source === "existing_chrome_clipboard"
        ? ["Captured from the visible Chrome tab text because Chrome blocked direct page extraction."]
        : [],
    },
  };
}

let tempDir = "";
try {
  tempDir = await mkdtemp(path.join(tmpdir(), "x-chrome-extract-"));
  const jsPath = path.join(tempDir, "extract.js");
  await writeFile(jsPath, extractionJs, "utf-8");
  let parsed = await extractFromChromeTab(url, jsPath);
  let body = cleanText(parsed.body);
  let media = parsed.media || [];

  if (parsed.article_url && parsed.article_url !== parsed.current_url) {
    const originalTitle = parsed.title;
    const articleParsed = await extractFromChromeTab(parsed.article_url, jsPath);
    const articleBody = removeChromeXNoise(articleParsed.body);
    const articleMedia = articleParsed.media || [];
    if (isUsable(articleBody, articleMedia)) {
      articleParsed.title = originalTitle || articleParsed.title;
      parsed = articleParsed;
      body = articleBody;
      media = articleMedia;
    }
  }

  const result = resultFromParsed(parsed, "existing_chrome_tab");
  finish(result, result.ok ? 0 : 5);
} catch (error) {
  const detail = String(error?.stderr || error?.message || error);
  const lower = detail.toLowerCase();
  if (detail.includes("-1723") || lower.includes("access not allowed") || (lower.includes("javascript") && lower.includes("apple"))) {
    try {
      const parsed = await extractFromChromeClipboard(url);
      if (!sameXTarget(url, parsed.current_url)) {
        finish({
          ok: false,
          status: "chrome_wrong_tab",
          message: "Chrome opened, but the active tab did not match the requested X article. Bring the logged-in Chrome profile window to front and retry.",
          requested_url: url,
          current_url: parsed.current_url,
        }, 6);
      }
      const result = resultFromParsed(parsed, "existing_chrome_clipboard");
      finish(result, result.ok ? 0 : 6);
    } catch (fallbackError) {
      finish({
        ok: false,
        status: "chrome_automation_blocked",
        message: "Chrome blocked direct extraction, and macOS blocked the clipboard fallback. Enable Chrome View > Developer > Allow JavaScript from Apple Events, or allow Codex/Terminal/osascript under System Settings > Privacy & Security > Accessibility.",
        detail: String(fallbackError?.stderr || fallbackError?.message || fallbackError),
      }, 6);
    }
  }
  if (detail.includes("-1743") || lower.includes("not authorized") || lower.includes("not permitted")) {
    finish({
      ok: false,
      status: "chrome_automation_blocked",
      message: "macOS blocked automation control of Chrome. Allow this app/terminal to control Google Chrome in System Settings, then try again.",
      detail,
    }, 7);
  }
  finish({
    ok: false,
    status: "existing_chrome_failed",
    message: `Could not extract from the existing Chrome profile: ${detail}`,
  }, 8);
} finally {
  if (tempDir) await rm(tempDir, { recursive: true, force: true }).catch(() => {});
}
