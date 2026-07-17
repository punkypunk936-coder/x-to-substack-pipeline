import { execFile } from "node:child_process";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { promisify } from "node:util";
import { draftBodyHtml, draftPlainText, draftStructure } from "./rich_draft.mjs";

const execFileAsync = promisify(execFile);
const payloadPath = process.argv[2];
const editorUrl = process.env.SUBSTACK_EDITOR_URL || process.env.SUBSTACK_PUBLISH_URL || "";
const browserApp = process.env.SUBSTACK_BROWSER_APP || "Google Chrome";
const confirmPublish = process.env.SUBSTACK_CONFIRM_PUBLISH === "1";
const allowAutopublish = process.env.SUBSTACK_AUTOPUBLISH === "1";
const draftUrlMarker = "-----X_SUBSTACK_DRAFT_URL-----";

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
    expectedStructure: draftStructure(draft),
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
    const normalizeText = (value) => String(value || "").replace(/\\s+/g, " ").trim();
    const expectedText = normalizeText(draft.bodyText);
    const richTextCount = (selector) => Array.from(body.querySelectorAll(selector))
      .filter((element) => normalizeText(element.innerText).length > 0).length;
    const structure = () => ({
      images: body.querySelectorAll("img").length,
      links: Array.from(body.querySelectorAll("a[href]")).filter((link) => !link.querySelector("img") && normalizeText(link.innerText)).length,
      headings: richTextCount("h1,h2,h3"),
      quotes: richTextCount("blockquote"),
      list_items: richTextCount("li"),
      bold: richTextCount("strong,b"),
      italic: richTextCount("em,i"),
    });
    const structureComplete = () => Object.keys(draft.expectedStructure).every(
      (key) => Number(structure()[key] || 0) === Number(draft.expectedStructure[key] || 0),
    );
    const textComplete = () => normalizeText(body.innerText) === expectedText;
    const selection = getSelection();
    const clearBody = () => {
      body.focus();
      const clearRange = document.createRange();
      clearRange.selectNodeContents(body);
      selection.removeAllRanges();
      selection.addRange(clearRange);
      document.execCommand("delete", false);
      if (normalizeText(body.innerText) || body.querySelector("img,video,audio,iframe")) {
        body.replaceChildren();
        body.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "deleteContent" }));
      }
    };
    const dataUrlFile = (value, index) => {
      const match = String(value || "").match(/^data:([^;,]+)(;base64)?,(.*)$/s);
      if (!match) return null;
      try {
        const mime = match[1] || "image/jpeg";
        const bytes = match[2]
          ? Uint8Array.from(atob(match[3]), (character) => character.charCodeAt(0))
          : new TextEncoder().encode(decodeURIComponent(match[3]));
        const extension = mime.split("/")[1]?.replace(/[^a-z0-9]/gi, "") || "jpg";
        return new File([bytes], "x-article-image-" + (index + 1) + "." + extension, { type: mime });
      } catch {
        return null;
      }
    };
    const selectBody = () => {
      const range = document.createRange();
      range.selectNodeContents(body);
      selection.removeAllRanges();
      selection.addRange(range);
    };
    clearBody();
    selectBody();
    let pasteDispatched = false;
    try {
      const transfer = new DataTransfer();
      transfer.setData("text/html", draft.bodyHtml);
      transfer.setData("text/plain", draft.bodyText);
      Array.from(new DOMParser().parseFromString(draft.bodyHtml, "text/html").querySelectorAll("img[src^='data:image/']"))
        .map((image, index) => dataUrlFile(image.getAttribute("src"), index))
        .filter(Boolean)
        .forEach((file) => transfer.items.add(file));
      body.dispatchEvent(new ClipboardEvent("paste", { bubbles: true, cancelable: true, clipboardData: transfer }));
      pasteDispatched = true;
    } catch {}
    setTimeout(() => {
      if (!textComplete() || !structureComplete()) {
        clearBody();
        selectBody();
        document.execCommand("insertHTML", false, draft.bodyHtml);
        body.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertFromPaste", data: draft.bodyText }));
      }
    }, pasteDispatched ? 1200 : 0);
    body.dispatchEvent(new Event("change", { bubbles: true }));
    body.setAttribute("data-x-substack-bridge-body", "1");
    body.setAttribute("data-x-substack-bridge-expected", encodeURIComponent(JSON.stringify(draft.expectedStructure)));
    body.setAttribute("data-x-substack-bridge-text", encodeURIComponent(expectedText));
    return JSON.stringify({
      ok: true,
      status: "draft_populated_existing_chrome",
      current_url: location.href,
      title_found: !!title,
      subtitle_found: !!subtitle,
      body_found: !!body,
      body_chars: (body.innerText || "").trim().length,
      image_count: body.querySelectorAll("img").length,
      expected_structure: draft.expectedStructure,
      expected_text_chars: expectedText.length
    });
  })()`;
}

function verificationSource() {
  return String.raw`(() => {
    const body = document.querySelector("[data-x-substack-bridge-body]");
    const title = Array.from(document.querySelectorAll("textarea,input,[contenteditable]"))
      .find((el) => /title/i.test((el.getAttribute("placeholder") || "") + " " + (el.getAttribute("data-placeholder") || "")));
    const expected = body ? JSON.parse(decodeURIComponent(body.getAttribute("data-x-substack-bridge-expected") || "%7B%7D")) : {};
    const expectedText = body ? decodeURIComponent(body.getAttribute("data-x-substack-bridge-text") || "") : "";
    const normalize = (value) => String(value || "").replace(/\s+/g, " ").trim();
    const actualText = body ? normalize(body.innerText) : "";
    const countText = (selector) => Array.from(body?.querySelectorAll(selector) || [])
      .filter((element) => normalize(element.innerText).length > 0).length;
    const actual = body ? {
      images: body.querySelectorAll("img").length,
      links: Array.from(body.querySelectorAll("a[href]")).filter((link) => !link.querySelector("img") && normalize(link.innerText)).length,
      headings: countText("h1,h2,h3"),
      quotes: countText("blockquote"),
      list_items: countText("li"),
      bold: countText("strong,b"),
      italic: countText("em,i"),
    } : {};
    const missing = Object.keys(expected).filter((key) => Number(actual[key] || 0) !== Number(expected[key] || 0));
    const textMatch = actualText === expectedText;
    if (!textMatch) missing.push("text");
    let mismatchIndex = -1;
    if (!textMatch) {
      const maxLength = Math.max(actualText.length, expectedText.length);
      mismatchIndex = Array.from({ length: maxLength }, (_, index) => index)
        .find((index) => actualText[index] !== expectedText[index]) ?? -1;
    }
    const contextStart = Math.max(0, mismatchIndex - 60);
    const contextEnd = mismatchIndex < 0 ? 0 : mismatchIndex + 80;
    const ok = !!title && !!body && textMatch && missing.length === 0;
    return JSON.stringify({
      ok,
      status: ok ? "draft_verified_background" : "fidelity_check_failed",
      current_url: location.href,
      title_found: !!title,
      body_found: !!body,
      body_chars: actualText.length,
      expected_text_chars: expectedText.length,
      text_match: textMatch,
      expected_structure: expected,
      actual_structure: actual,
      missing,
      mismatch_index: mismatchIndex,
      expected_context: mismatchIndex < 0 ? "" : expectedText.slice(contextStart, contextEnd),
      actual_context: mismatchIndex < 0 ? "" : actualText.slice(contextStart, contextEnd),
    });
  })()`;
}

function verificationOkaySource() {
  return String.raw`(() => {
    const body = document.querySelector("[data-x-substack-bridge-body]");
    if (!body) return "0";
    const expected = JSON.parse(decodeURIComponent(body.getAttribute("data-x-substack-bridge-expected") || "%7B%7D"));
    const expectedText = decodeURIComponent(body.getAttribute("data-x-substack-bridge-text") || "");
    const normalize = (value) => String(value || "").replace(/\s+/g, " ").trim();
    const countText = (selector) => Array.from(body.querySelectorAll(selector))
      .filter((element) => normalize(element.innerText).length > 0).length;
    const actual = {
      images: body.querySelectorAll("img").length,
      links: Array.from(body.querySelectorAll("a[href]")).filter((link) => !link.querySelector("img") && normalize(link.innerText)).length,
      headings: countText("h1,h2,h3"),
      quotes: countText("blockquote"),
      list_items: countText("li"),
      bold: countText("strong,b"),
      italic: countText("em,i"),
    };
    const structureMatches = Object.keys(expected)
      .every((key) => Number(actual[key] || 0) === Number(expected[key] || 0));
    return structureMatches && normalize(body.innerText) === expectedText ? "1" : "0";
  })()`;
}

function continueSource() {
  return `(() => { const button = Array.from(document.querySelectorAll("button")).find((item) => item.offsetParent && /^(Continue|Next)$/i.test((item.innerText || "").trim())); if (button) { button.click(); return "clicked"; } return "missing"; })()`;
}

function publishSource() {
  return `(() => { const button = Array.from(document.querySelectorAll("button")).find((item) => item.offsetParent && /^(Send to everyone now|Publish now|Publish|Send)$/i.test((item.innerText || "").trim())); if (button) { button.click(); return "clicked"; } return "missing"; })()`;
}

function finalStateSource() {
  return `(() => { const finalButton = Array.from(document.querySelectorAll("button")).find((item) => item.offsetParent && /^(Send to everyone now|Publish now|Publish|Send)$/i.test((item.innerText || "").trim())); const text = document.body.innerText || ""; return JSON.stringify({ ok: !finalButton, clicked: true, status: !finalButton ? "published" : "publish_not_confirmed", final_button_visible: !!finalButton, confirmation_visible: /published|post sent|sent to everyone|view post/i.test(text), current_url: location.href }); })()`;
}

function draftLookupSource(draft) {
  const title = JSON.stringify(String(draft.title || "Untitled X article").trim());
  return `(() => {
    const expectedTitle = ${title};
    const normalize = (value) => String(value || "").replace(/\\s+/g, " ").trim();
    const candidates = Array.from(document.querySelectorAll("a[href*='/publish/post/']"));
    const exact = candidates.find((anchor) => normalize(anchor.innerText).startsWith(normalize(expectedTitle)));
    return exact ? exact.href.split("?")[0] : "";
  })()`;
}

function substackWorkerAppleScript(appName) {
  return `
on run argv
  set targetUrl to item 1 of argv
  set jsPath to item 2 of argv
  set verifyPath to item 3 of argv
  set verifyOkPath to item 4 of argv
  set continuePath to item 5 of argv
  set publishPath to item 6 of argv
  set finalStatePath to item 7 of argv
  set draftsUrl to item 8 of argv
  set draftLookupPath to item 9 of argv
  set shouldPublish to item 10 of argv
  set jsSource to read POSIX file jsPath as «class utf8»
  set verifySource to read POSIX file verifyPath as «class utf8»
  set verifyOkSource to read POSIX file verifyOkPath as «class utf8»
  set continueSource to read POSIX file continuePath as «class utf8»
  set publishSource to read POSIX file publishPath as «class utf8»
  set finalStateSource to read POSIX file finalStatePath as «class utf8»
  set draftLookupSource to read POSIX file draftLookupPath as «class utf8»
  tell application ${JSON.stringify(appName)}
    if (count of windows) = 0 then error "Chrome must already be running in the signed-in Substack profile."
    set originalWindowId to id of front window
    set originalTabIndex to active tab index of front window
    set workerWindow to make new window with properties {visible:false}
    set workerWindowId to id of workerWindow
    try
      set bounds of window id workerWindowId to {-20000, -20000, -18640, -19020}
      set visible of window id workerWindowId to true
      set index of window id originalWindowId to 1
      set URL of active tab of window id workerWindowId to targetUrl
      repeat with i from 1 to 160
        set bounds of window id workerWindowId to {-20000, -20000, -18640, -19020}
        set index of window id originalWindowId to 1
        delay 0.25
        if (loading of active tab of window id workerWindowId is false) then exit repeat
      end repeat
      delay 2
      execute active tab of window id workerWindowId javascript jsSource
      delay 12
      set verifyText to execute active tab of window id workerWindowId javascript verifySource
      set verifyOk to execute active tab of window id workerWindowId javascript verifyOkSource
      if verifyOk is not "1" then
        close window id workerWindowId
        try
          set active tab index of window id originalWindowId to originalTabIndex
          set index of window id originalWindowId to 1
        end try
        return verifyText
      end if
      if shouldPublish is "1" then
        set continueResult to execute active tab of window id workerWindowId javascript continueSource
        if continueResult is not "clicked" then
          set resultText to execute active tab of window id workerWindowId javascript "JSON.stringify({ ok: false, status: 'continue_missing', current_url: location.href })"
        else
          delay 3
          set publishResult to execute active tab of window id workerWindowId javascript publishSource
          if publishResult is not "clicked" then
            set resultText to execute active tab of window id workerWindowId javascript "JSON.stringify({ ok: false, status: 'publish_button_missing', current_url: location.href })"
          else
            delay 8
            set resultText to execute active tab of window id workerWindowId javascript finalStateSource
          end if
        end if
      else
        set resultText to verifyText
        set URL of active tab of window id workerWindowId to draftsUrl
        repeat with i from 1 to 120
          set bounds of window id workerWindowId to {-20000, -20000, -18640, -19020}
          set index of window id originalWindowId to 1
          delay 0.25
          if (loading of active tab of window id workerWindowId is false) then exit repeat
        end repeat
        delay 3
        set resolvedDraftUrl to execute active tab of window id workerWindowId javascript draftLookupSource
        if resolvedDraftUrl starts with "http" then
          set resultText to resultText & ${JSON.stringify(draftUrlMarker)} & resolvedDraftUrl
        end if
      end if
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

if (!payloadPath) finish({ ok: false, status: "missing_payload", message: "Missing publish payload path." }, 2);
if (!editorUrl) finish({ ok: false, status: "setup_required", message: "Set SUBSTACK_EDITOR_URL before publishing." }, 2);

let tempDir = "";
try {
  const draft = JSON.parse(await readFile(payloadPath, "utf-8"));
  tempDir = await mkdtemp(path.join(tmpdir(), "substack-chrome-publish-"));
  const jsPath = path.join(tempDir, "populate.js");
  const verifyPath = path.join(tempDir, "verify.js");
  const verifyOkPath = path.join(tempDir, "verify-ok.js");
  const continuePath = path.join(tempDir, "continue.js");
  const publishPath = path.join(tempDir, "publish.js");
  const finalStatePath = path.join(tempDir, "final-state.js");
  const draftLookupPath = path.join(tempDir, "draft-lookup.js");
  await writeFile(jsPath, injectionSource(draft), "utf-8");
  await Promise.all([
    writeFile(verifyPath, verificationSource(), "utf-8"),
    writeFile(verifyOkPath, verificationOkaySource(), "utf-8"),
    writeFile(continuePath, continueSource(), "utf-8"),
    writeFile(publishPath, publishSource(), "utf-8"),
    writeFile(finalStatePath, finalStateSource(), "utf-8"),
    writeFile(draftLookupPath, draftLookupSource(draft), "utf-8"),
  ]);
  const shouldPublish = confirmPublish && allowAutopublish;
  const draftsUrl = new URL("/publish/posts/drafts", editorUrl).href;
  const { stdout } = await execFileAsync("osascript", [
    "-e",
    substackWorkerAppleScript(browserApp),
    editorUrl,
    jsPath,
    verifyPath,
    verifyOkPath,
    continuePath,
    publishPath,
    finalStatePath,
    draftsUrl,
    draftLookupPath,
    shouldPublish ? "1" : "0",
  ], {
    timeout: shouldPublish ? 100000 : 80000,
    maxBuffer: 1024 * 1024 * 5,
  });
  const [resultText, resolvedDraftUrl = ""] = stdout.trim().split(draftUrlMarker);
  const result = JSON.parse(resultText);
  if (/^https?:\/\//i.test(resolvedDraftUrl)) result.current_url = resolvedDraftUrl;
  if (!result.ok) {
    const message = result.status === "fidelity_check_failed"
      ? `Substack did not preserve: ${(result.missing || []).join(", ")}. Nothing was published.`
      : "The background Substack editor fields were not found. Nothing was published.";
    finish({ ...result, message }, 4);
  }
  if (!shouldPublish) {
    finish({
      ...result,
      message: "Rich Substack draft saved and verified in the background. Keep editing or publish from the dashboard.",
    });
  }
  const published = result.clicked === true && result.final_button_visible !== true && result.ok === true;
  finish({
    ok: published,
    status: published ? "published" : result.status || "publish_not_confirmed",
    message: published ? "Substack accepted the final publish action." : "Draft is ready, but Substack did not confirm the final publish action.",
    current_url: result.current_url,
    confirmation_visible: result.confirmation_visible === true,
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
