import { execFile } from "node:child_process";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);
const accountUrl = process.env.X_ACCOUNT_URL || "https://x.com/0xgoodie/articles";
const browserApp = process.env.X_BROWSER_APP || "Google Chrome";
const maxArticles = Math.max(1, Number(process.env.X_SYNC_MAX_ARTICLES || 30));

function finish(result, code = 0) {
  console.log(JSON.stringify(result));
  process.exit(code);
}

const collectorJs = String.raw`
(() => {
  window.__xSubstackArticleInbox ||= {};
  const clean = (value) => String(value || "").replace(/\s+/g, " ").trim();
  for (const link of document.querySelectorAll("a[href*='/article/'], a[href*='/i/article/']")) {
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
      error "Chrome must already be running in the signed-in X profile."
    end if
    set originalWindowId to id of front window
    set originalTabIndex to active tab index of front window
    set workerWindow to make new window with properties {visible:false}
    set workerWindowId to id of workerWindow
    try
      set bounds of window id workerWindowId to {-20000, -20000, -18640, -19020}
      set minimized of window id workerWindowId to true
      set URL of active tab of window id workerWindowId to targetUrl
      repeat with i from 1 to 120
        set visible of window id workerWindowId to false
        set minimized of window id workerWindowId to true
        delay 0.25
        if (loading of active tab of window id workerWindowId is false) then exit repeat
      end repeat
      delay 2
      repeat with i from 1 to 14
        set visible of window id workerWindowId to false
        set minimized of window id workerWindowId to true
        execute active tab of window id workerWindowId javascript jsSource
        delay 0.8
      end repeat
      set resultText to execute active tab of window id workerWindowId javascript "JSON.stringify(Object.values(window.__xSubstackArticleInbox || {}))"
      close window id workerWindowId
      try
        set active tab index of window id originalWindowId to originalTabIndex
        set index of window id originalWindowId to 1
      end try
      return resultText
    on error errorMessage number errorNumber
      try
        close window id workerWindowId
      end try
      try
        set active tab index of window id originalWindowId to originalTabIndex
        set index of window id originalWindowId to 1
      end try
      error errorMessage number errorNumber
    end try
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
  const items = JSON.parse(stdout.trim() || "[]")
    .sort((left, right) => {
      try {
        const leftId = BigInt(left.id || "0");
        const rightId = BigInt(right.id || "0");
        return leftId === rightId ? 0 : leftId > rightId ? -1 : 1;
      } catch {
        return String(right.id || "").localeCompare(String(left.id || ""));
      }
    })
    .slice(0, maxArticles);
  finish({ ok: true, status: "discovered", account_url: accountUrl, items });
} catch (error) {
  finish({
    ok: false,
    status: "discovery_failed",
    message: "Could not check the X article feed in the hidden Chrome worker.",
    detail: String(error?.stderr || error?.message || error),
  }, 2);
} finally {
  if (tempDir) await rm(tempDir, { recursive: true, force: true }).catch(() => {});
}
