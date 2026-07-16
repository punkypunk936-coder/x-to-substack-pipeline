import { execFile } from "node:child_process";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { promisify } from "node:util";
import { draftBodyHtml, draftPlainText, imageCount } from "./rich_draft.mjs";

const execFileAsync = promisify(execFile);
const payloadPath = process.argv[2];
const editorUrl = process.env.SUBSTACK_EDITOR_URL || process.env.SUBSTACK_PUBLISH_URL || "";
const browserApp = process.env.SUBSTACK_BROWSER_APP || "Google Chrome";
const confirmPublish = process.env.SUBSTACK_CONFIRM_PUBLISH === "1";
const allowAutopublish = process.env.SUBSTACK_AUTOPUBLISH === "1";

function finish(result, code = 0) {
  console.log(JSON.stringify(result));
  process.exit(code);
}

function injectionSource(draft) {
  const payload = JSON.stringify({
    title: draft.title || "Untitled X article",
    subtitle: draft.subtitle || "",
    bodyText: draftPlainText(draft),
    bodyHtml: draftBodyHtml(draft),
    expectedImages: imageCount(draft),
  });
  return `(() => {
    const draft = ${payload};
    const visible = (el) => {
      if (!el) return false;
      const style = getComputedStyle(el);
      const rect = el.getBoundingClientRect();
      return style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0;
    };
    const candidates = (selectors) => selectors.flatMap((selector) => Array.from(document.querySelectorAll(selector))).filter(visible);
    const setValue = (el, value) => {
      el.focus();
      const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
      if (setter) setter.call(el, value); else el.value = value;
      el.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: value }));
      el.dispatchEvent(new Event("change", { bubbles: true }));
    };
    const title = candidates([
      "textarea[placeholder='Title']", "textarea[placeholder*='title' i]",
      "input[placeholder='Title']", "input[placeholder*='title' i]",
      "[contenteditable='true'][data-placeholder='Title']"
    ])[0];
    const subtitle = candidates([
      "textarea[placeholder*='subtitle' i]", "input[placeholder*='subtitle' i]",
      "[contenteditable='true'][data-placeholder*='subtitle' i]"
    ])[0];
    const editables = candidates([".ProseMirror[contenteditable='true']", "[contenteditable='true']"])
      .filter((el) => el !== title && el !== subtitle)
      .filter((el) => !/title|subtitle/i.test((el.getAttribute("aria-label") || "") + " " + (el.getAttribute("data-placeholder") || "")))
      .sort((a, b) => b.getBoundingClientRect().height - a.getBoundingClientRect().height);
    const body = editables.find((el) => /start writing|body|post/i.test((el.getAttribute("aria-label") || "") + " " + (el.getAttribute("data-placeholder") || ""))) || editables[0];
    if (!title || !body) return JSON.stringify({ ok: false, status: "editor_not_found", title_found: !!title, body_found: !!body, current_url: location.href });
    if (title.isContentEditable) {
      title.textContent = draft.title;
      title.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: draft.title }));
    } else setValue(title, draft.title);
    if (subtitle && draft.subtitle) {
      if (subtitle.isContentEditable) {
        subtitle.textContent = draft.subtitle;
        subtitle.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: draft.subtitle }));
      } else setValue(subtitle, draft.subtitle);
    }
    body.focus();
    const selection = getSelection();
    const range = document.createRange();
    range.selectNodeContents(body);
    selection.removeAllRanges();
    selection.addRange(range);
    document.execCommand("delete", false);
    let pasteDispatched = false;
    try {
      const transfer = new DataTransfer();
      transfer.setData("text/html", draft.bodyHtml);
      transfer.setData("text/plain", draft.bodyText);
      body.dispatchEvent(new ClipboardEvent("paste", { bubbles: true, cancelable: true, clipboardData: transfer }));
      pasteDispatched = true;
    } catch {}
    setTimeout(() => {
      if (!(body.innerText || "").trim()) {
        body.focus();
        document.execCommand("insertHTML", false, draft.bodyHtml);
        body.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertFromPaste", data: draft.bodyText }));
      }
    }, pasteDispatched ? 1200 : 0);
    body.dispatchEvent(new Event("change", { bubbles: true }));
    body.setAttribute("data-x-substack-bridge-body", "1");
    return JSON.stringify({
      ok: true,
      status: "draft_populated_existing_chrome",
      current_url: location.href,
      title_found: !!title,
      subtitle_found: !!subtitle,
      body_found: !!body,
      body_chars: (body.innerText || "").trim().length,
      image_count: body.querySelectorAll("img").length,
      expected_images: draft.expectedImages
    });
  })()`;
}

function populateAppleScript(appName) {
  return `
on run argv
  set targetUrl to item 1 of argv
  set jsPath to item 2 of argv
  set jsSource to read POSIX file jsPath as «class utf8»
  tell application ${JSON.stringify(appName)}
    if (count of windows) = 0 then make new window
    set targetWindow to front window
    make new tab at end of tabs of targetWindow with properties {URL:targetUrl}
    set targetIndex to count of tabs of targetWindow
    set active tab index of targetWindow to targetIndex
    set targetTab to tab targetIndex of targetWindow
    repeat with i from 1 to 160
      delay 0.25
      if (loading of targetTab is false) then exit repeat
    end repeat
    delay 2
    execute targetTab javascript jsSource
    delay 7
    return execute targetTab javascript "(() => { const body = document.querySelector('[data-x-substack-bridge-body]'); const title = Array.from(document.querySelectorAll('textarea,input,[contenteditable]')).find((el) => /title/i.test((el.getAttribute('placeholder') || '') + ' ' + (el.getAttribute('data-placeholder') || ''))); return JSON.stringify({ ok: !!title && !!body && ((body.innerText || '').trim().length > 0 || body.querySelectorAll('img').length > 0), status: 'draft_populated_existing_chrome', current_url: location.href, title_found: !!title, subtitle_found: !!Array.from(document.querySelectorAll('textarea,input,[contenteditable]')).find((el) => /subtitle/i.test((el.getAttribute('placeholder') || '') + ' ' + (el.getAttribute('data-placeholder') || ''))), body_found: !!body, body_chars: body ? (body.innerText || '').trim().length : 0, image_count: body ? body.querySelectorAll('img').length : 0 }); })()"
  end tell
end run
`;
}

function publishAppleScript(appName) {
  return `
tell application ${JSON.stringify(appName)}
  set targetTab to active tab of front window
  set continueResult to execute targetTab javascript "(() => { const b = Array.from(document.querySelectorAll('button')).find((x) => x.offsetParent && /^(Continue|Next)$/i.test((x.innerText || '').trim())); if (b) { b.click(); return 'clicked'; } return 'missing'; })()"
  if continueResult is not "clicked" then return "continue_missing"
  delay 3
  set publishResult to execute targetTab javascript "(() => { const b = Array.from(document.querySelectorAll('button')).find((x) => x.offsetParent && /^(Send to everyone now|Publish now|Publish|Send)$/i.test((x.innerText || '').trim())); if (b) { b.click(); return 'clicked'; } return 'missing'; })()"
  if publishResult is not "clicked" then return publishResult
  delay 8
  return execute targetTab javascript "(() => { const finalButton = Array.from(document.querySelectorAll('button')).find((x) => x.offsetParent && /^(Send to everyone now|Publish now|Publish|Send)$/i.test((x.innerText || '').trim())); const text = document.body.innerText || ''; return JSON.stringify({ clicked: true, final_button_visible: !!finalButton, confirmation_visible: /published|post sent|sent to everyone|view post/i.test(text), current_url: location.href }); })()"
end tell
`;
}

if (!payloadPath) finish({ ok: false, status: "missing_payload", message: "Missing publish payload path." }, 2);
if (!editorUrl) finish({ ok: false, status: "setup_required", message: "Set SUBSTACK_EDITOR_URL before publishing." }, 2);

let tempDir = "";
try {
  const draft = JSON.parse(await readFile(payloadPath, "utf-8"));
  tempDir = await mkdtemp(path.join(tmpdir(), "substack-chrome-publish-"));
  const jsPath = path.join(tempDir, "populate.js");
  await writeFile(jsPath, injectionSource(draft), "utf-8");
  const { stdout } = await execFileAsync("osascript", ["-e", populateAppleScript(browserApp), editorUrl, jsPath], {
    timeout: 70000,
    maxBuffer: 1024 * 1024 * 5,
  });
  const result = JSON.parse(stdout.trim());
  if (!result.ok) finish({ ...result, message: "Substack opened, but the editor fields were not found." }, 4);
  const expectedImages = imageCount(draft);
  if (expectedImages > 0 && Number(result.image_count || 0) < expectedImages) {
    finish({
      ...result,
      ok: false,
      status: "media_transfer_incomplete",
      expected_images: expectedImages,
      message: "Substack did not accept every image, so publishing was stopped before anything went live.",
    }, 4);
  }
  if (!(confirmPublish && allowAutopublish)) {
    finish({
      ...result,
      message: "Rich Substack draft saved in the background. Keep editing or publish from the dashboard.",
    });
  }
  const publishResult = await execFileAsync("osascript", ["-e", publishAppleScript(browserApp)], {
    timeout: 30000,
    maxBuffer: 1024 * 1024,
  });
  let publishState = {};
  try {
    publishState = JSON.parse(publishResult.stdout.trim());
  } catch {
    publishState = { clicked: false, status: publishResult.stdout.trim() };
  }
  const published = publishState.clicked === true && publishState.final_button_visible !== true;
  finish({
    ok: published,
    status: published ? "published" : publishState.status || "publish_not_confirmed",
    message: published ? "Substack accepted the final publish action." : "Draft is ready, but Substack did not confirm the final publish action.",
    current_url: publishState.current_url || result.current_url,
    confirmation_visible: publishState.confirmation_visible === true,
  }, published ? 0 : 5);
} catch (error) {
  const detail = String(error?.stderr || error?.message || error);
  const blocked = detail.includes("-1723") || /access not allowed/i.test(detail);
  finish({
    ok: false,
    status: blocked ? "chrome_javascript_blocked" : "publish_failed",
    message: blocked
      ? "Chrome is logged into Substack, but 'Allow JavaScript from Apple Events' is still disabled in Chrome's View > Developer menu."
      : "Could not populate the Substack editor in Chrome.",
    detail,
  }, 6);
} finally {
  if (tempDir) await rm(tempDir, { recursive: true, force: true }).catch(() => {});
}
