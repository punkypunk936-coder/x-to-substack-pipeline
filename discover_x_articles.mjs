import { execFile } from "node:child_process";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);
const accountUrl = process.env.X_ACCOUNT_URL || "https://x.com/0xgoodie/articles";
const browserApp = process.env.X_BROWSER_APP || "Google Chrome";
const maxArticles = Math.max(1, Number(process.env.X_SYNC_MAX_ARTICLES || 8));

function finish(result, code = 0) {
  console.log(JSON.stringify(result));
  process.exit(code);
}

const collectorJs = String.raw`
(() => {
  window.__xSubstackArticleInbox ||= {};
  const clean = (value) => String(value || "").replace(/\s+/g, " ").trim();
  for (const link of document.querySelectorAll("a[href*='/article/']")) {
    const article = link.closest("article") || link.closest("[data-testid='cellInnerDiv']");
    if (!article) continue;
    const statusLinks = Array.from(article.querySelectorAll("a[href*='/status/']"));
    const statusLink = statusLinks.find((item) => /\/status\/\d+/.test(item.getAttribute("href") || ""));
    if (!statusLink) continue;
    const href = new URL(statusLink.getAttribute("href"), location.href).toString().split("?")[0];
    const id = href.match(/\/status\/(\d+)/)?.[1];
    if (!id) continue;
    const title = clean(link.innerText || link.getAttribute("aria-label") || "");
    window.__xSubstackArticleInbox[id] = { id, url: href, title };
  }
  const account = location.pathname.split("/").filter(Boolean)[0] || "0xgoodie";
  for (const article of document.querySelectorAll("article")) {
    const lines = String(article.innerText || "").split("\n").map((line) => line.trim()).filter(Boolean);
    const articleIndex = lines.findIndex((line) => line === "Article");
    if (articleIndex < 0 || !lines[articleIndex + 1]) continue;
    const statusLink = Array.from(article.querySelectorAll("a[href*='/status/']"))
      .find((item) => new URL(item.getAttribute("href"), location.href).pathname.startsWith("/" + account + "/status/"));
    if (!statusLink) continue;
    const href = new URL(statusLink.getAttribute("href"), location.href).toString().split("?")[0];
    const id = href.match(/\/status\/(\d+)/)?.[1];
    if (!id) continue;
    window.__xSubstackArticleInbox[id] = { id, url: href, title: clean(lines[articleIndex + 1]) };
  }
  window.scrollBy(0, Math.max(window.innerHeight * 1.4, 900));
  return Object.keys(window.__xSubstackArticleInbox).length;
})()
`;

function discoveryAppleScript(appName) {
  return `
on run argv
  set targetUrl to item 1 of argv
  set jsPath to item 2 of argv
  set jsSource to read POSIX file jsPath as «class utf8»
  tell application ${JSON.stringify(appName)}
    if (count of windows) = 0 then
      activate
      make new window
    end if
    set targetWindow to front window
    set targetTab to make new tab at end of tabs of targetWindow with properties {URL:targetUrl}
    repeat with i from 1 to 120
      delay 0.25
      if (loading of targetTab is false) then exit repeat
    end repeat
    delay 2
    repeat with i from 1 to 7
      execute targetTab javascript jsSource
      delay 0.8
    end repeat
    set resultText to execute targetTab javascript "JSON.stringify(Object.values(window.__xSubstackArticleInbox || {}))"
    close targetTab
    return resultText
  end tell
end run
`;
}

let tempDir = "";
try {
  tempDir = await mkdtemp(path.join(tmpdir(), "x-article-discovery-"));
  const jsPath = path.join(tempDir, "collect.js");
  await writeFile(jsPath, collectorJs, "utf-8");
  const { stdout } = await execFileAsync("osascript", ["-e", discoveryAppleScript(browserApp), accountUrl, jsPath], {
    timeout: 60000,
    maxBuffer: 1024 * 1024 * 3,
  });
  const items = JSON.parse(stdout.trim() || "[]").slice(0, maxArticles);
  finish({ ok: true, status: "discovered", account_url: accountUrl, items });
} catch (error) {
  finish({
    ok: false,
    status: "discovery_failed",
    message: "Could not check the X article feed in Chrome.",
    detail: String(error?.stderr || error?.message || error),
  }, 2);
} finally {
  if (tempDir) await rm(tempDir, { recursive: true, force: true }).catch(() => {});
}
