const $ = (selector) => document.querySelector(selector);

let currentDraft = null;
let editorDirty = false;
let currentPipeline = null;

function setStatus(message, mode = "idle") {
  const node = $("#statusLine");
  node.textContent = message;
  node.dataset.mode = mode;
}

function escapeHtml(value) {
  return String(value || "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function draftHtml(draft) {
  const title = escapeHtml(draft.title || "Untitled X article");
  const subtitle = draft.subtitle ? `<p>${escapeHtml(draft.subtitle)}</p>` : "";
  const body = String(draft.body || "")
    .split(/\n{2,}/)
    .filter(Boolean)
    .map((paragraph) => paragraph.startsWith("## ")
      ? `<h2>${escapeHtml(paragraph.slice(3))}</h2>`
      : `<p>${escapeHtml(paragraph).replaceAll("\n", "<br>")}</p>`)
    .join("");
  const media = (draft.media || [])
    .map((item) => item.url ? `<figure><img src="${escapeHtml(item.url)}" alt="${escapeHtml(item.alt || "X media")}"></figure>` : "")
    .join("");
  return `<h1>${title}</h1>${subtitle}${body}${media}`;
}

function renderDraft(draft) {
  currentDraft = draft;
  $("#workspace").classList.remove("hidden");
  $("#draftTitle").textContent = draft.title || "Untitled X article";
  $("#draftSubtitle").textContent = draft.subtitle || "";
  $("#draftMeta").textContent = `${draft.source || "source"} / ${(draft.body || "").split(/\s+/).filter(Boolean).length} words / ${(draft.media || []).length} media item(s)`;
  $("#editTitle").value = draft.title || "";
  $("#editSubtitle").value = draft.subtitle || "";
  $("#editBody").value = draft.body || "";
  editorDirty = false;
  updateEditMeta();
  renderInlinePreview();
  const warnings = draft.warnings || [];
  $("#warningLine").textContent = warnings.length ? warnings.join(" ") : "";
  $("#publishLine").textContent = "";
}

function formatSyncTime(value) {
  if (!value) return "Not synced yet";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Synced recently";
  return `Last checked ${date.toLocaleString([], { dateStyle: "medium", timeStyle: "short" })}`;
}

function renderPipeline(pipeline) {
  currentPipeline = pipeline;
  $("#accountLabel").textContent = pipeline.account || "@0xgoodie";
  const list = $("#draftList");
  list.replaceChildren();
  for (const item of pipeline.items || []) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "draft-item";
    button.classList.toggle("active", String(item.id) === String(pipeline.selected_id));
    button.dataset.draftId = item.id;

    const status = document.createElement("span");
    status.className = `draft-status ${item.status === "published" ? "published" : ""}`;
    status.textContent = item.status === "published" ? "Published" : "Draft";

    const title = document.createElement("strong");
    title.textContent = item.title || "Untitled X article";

    const meta = document.createElement("span");
    meta.className = "draft-item-meta";
    meta.textContent = `${item.word_count || 0} words · ${item.media_count || 0} media`;

    button.append(status, title, meta);
    button.addEventListener("click", () => selectDraft(item.id));
    list.appendChild(button);
  }
  $("#workspace").classList.toggle("hidden", !(pipeline.items || []).length);
  const sync = pipeline.sync || {};
  $("#syncLine").textContent = sync.status === "syncing" ? "Checking X and Substack..." : formatSyncTime(sync.last_sync);
}

async function selectDraft(id) {
  if (editorDirty && !window.confirm("Discard unsaved changes and open another draft?")) return;
  setStatus("Opening draft...", "busy");
  try {
    const data = await api("/api/drafts/select", {
      method: "POST",
      body: JSON.stringify({ id }),
    });
    renderPipeline(data.pipeline);
    renderDraft(data.draft);
    setDraftView("edit");
    setStatus("Draft ready.", "ok");
  } catch (error) {
    setStatus(error.message, "error");
  }
}

async function syncDrafts({ silent = false } = {}) {
  const button = $("#syncButton");
  button.disabled = true;
  if (!silent) setStatus("Checking @0xgoodie and Substack...", "busy");
  $("#syncLine").textContent = "Checking X and Substack...";
  try {
    const data = await api("/api/drafts/sync", { method: "POST", body: "{}" });
    renderPipeline(data.pipeline);
    const added = (data.added || []).length;
    if (!silent || added) setStatus(added ? `${added} new article draft${added === 1 ? "" : "s"} added.` : "Draft pipeline is up to date.", "ok");
  } catch (error) {
    $("#syncLine").textContent = "Sync needs attention.";
    if (!silent) setStatus(error.message, "error");
  } finally {
    button.disabled = false;
  }
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "content-type": "application/json", ...(options.headers || {}) },
  });
  const data = await response.json();
  if (!response.ok || data.error) throw new Error(data.error || `Request failed: ${response.status}`);
  return data;
}

async function ingest() {
  const url = $("#xUrl").value.trim();
  if (!url) {
    setStatus("Paste an X article URL first.", "error");
    return;
  }
  $("#ingestButton").disabled = true;
  setStatus("Opening X in your logged-in Chrome session and building the Substack draft...", "busy");
  try {
    const data = await api("/api/ingest", {
      method: "POST",
      body: JSON.stringify({ url }),
    });
    renderPipeline(data.pipeline);
    renderDraft(data.draft);
    setStatus("Draft package ready. Rich HTML copied if your browser allowed clipboard access.", "ok");
    await copyRichDraft(false);
  } catch (error) {
    setStatus(error.message, "error");
  } finally {
    $("#ingestButton").disabled = false;
  }
}

function updateEditMeta() {
  const words = ($("#editBody").value || "").trim().split(/\s+/).filter(Boolean).length;
  $("#editMeta").textContent = `${words} words`;
}

function renderInlinePreview() {
  $("#previewTitle").textContent = $("#editTitle").value || "Untitled";
  const subtitle = $("#editSubtitle").value.trim();
  $("#previewSubtitle").textContent = subtitle;
  $("#previewSubtitle").classList.toggle("hidden", !subtitle);
  const body = $("#previewBody");
  body.replaceChildren();
  for (const block of $("#editBody").value.split(/\n{2,}/).filter((part) => part.trim())) {
    const node = document.createElement(block.startsWith("## ") ? "h2" : "p");
    node.textContent = block.startsWith("## ") ? block.slice(3).trim() : block.trim();
    body.appendChild(node);
  }
}

function setDraftView(mode) {
  const previewing = mode === "preview";
  $("#editor").classList.toggle("hidden", previewing);
  $("#inlinePreview").classList.toggle("hidden", !previewing);
  $("#editTab").classList.toggle("active", !previewing);
  $("#previewTab").classList.toggle("active", previewing);
  $("#editTab").setAttribute("aria-selected", String(!previewing));
  $("#previewTab").setAttribute("aria-selected", String(previewing));
  if (previewing) renderInlinePreview();
}

async function saveDraft({ quiet = false } = {}) {
  if (!currentDraft) throw new Error("Create a draft first.");
  const data = await api("/api/draft", {
    method: "POST",
    body: JSON.stringify({
      title: $("#editTitle").value,
      subtitle: $("#editSubtitle").value,
      body: $("#editBody").value,
    }),
  });
  renderPipeline(data.pipeline);
  renderDraft(data.draft);
  if (!quiet) setStatus("Draft changes saved.", "ok");
  return data.draft;
}

async function prepareSavedDraft() {
  if (editorDirty) await saveDraft({ quiet: true });
}

async function sendToSubstack(mode) {
  if (!currentDraft) {
    setStatus("Create a draft first.", "error");
    return;
  }
  const button = mode === "publish" ? $("#publishButton") : $("#substackDraftButton");
  button.disabled = true;
  try {
    await prepareSavedDraft();
    if (mode === "publish") {
      const confirmed = window.confirm(
        `Publish “${currentDraft.title}” to Everyone now? Substack will also send it by email and in the Substack app.`
      );
      if (!confirmed) return;
    }
    $("#publishLine").textContent = mode === "publish" ? "Publishing to Substack..." : "Preparing Substack editor...";
    setStatus(mode === "publish" ? "Sending the saved draft to Substack..." : "Opening the saved draft in Substack...", "busy");
    const result = await api("/api/publish", {
      method: "POST",
      body: JSON.stringify({ mode, confirm_publish: mode === "publish" }),
    });
    if (result.pipeline) renderPipeline(result.pipeline);
    $("#publishLine").textContent = result.message || result.status || "Publish step finished.";
    if (result.ok) {
      setStatus(result.message || "Substack step finished.", result.status === "published" ? "ok" : "busy");
    } else {
      setStatus(result.message || "Publish setup required.", "error");
    }
  } catch (error) {
    $("#publishLine").textContent = error.message;
    setStatus(error.message, "error");
  } finally {
    button.disabled = false;
  }
}

async function copyRichDraft(showStatus = true) {
  if (!currentDraft) return;
  const html = draftHtml(currentDraft);
  const text = `# ${currentDraft.title || "Untitled X article"}\n\n${currentDraft.subtitle ? `${currentDraft.subtitle}\n\n` : ""}${currentDraft.body || ""}`;
  try {
    if (window.ClipboardItem) {
      await navigator.clipboard.write([
        new ClipboardItem({
          "text/html": new Blob([html], { type: "text/html" }),
          "text/plain": new Blob([text], { type: "text/plain" }),
        }),
      ]);
    } else {
      await navigator.clipboard.writeText(text);
    }
    if (showStatus) setStatus("Rich draft copied.", "ok");
  } catch {
    await navigator.clipboard.writeText(text);
    if (showStatus) setStatus("Plain text draft copied.", "ok");
  }
}

async function loadCurrent() {
  const [data, pipeline] = await Promise.all([api("/api/current"), api("/api/drafts")]);
  renderPipeline(pipeline);
  if (data.draft) renderDraft(data.draft);
  window.setTimeout(() => syncDrafts({ silent: true }), 800);
}

$("#ingestButton").addEventListener("click", ingest);
$("#xUrl").addEventListener("keydown", (event) => {
  if (event.key === "Enter") ingest();
});
$("#editTab").addEventListener("click", () => setDraftView("edit"));
$("#previewTab").addEventListener("click", () => setDraftView("preview"));
$("#cancelEdit").addEventListener("click", () => {
  if (currentDraft) renderDraft(currentDraft);
});
$("#editor").addEventListener("input", () => {
  editorDirty = true;
  updateEditMeta();
  renderInlinePreview();
});
$("#editor").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("#saveDraft").disabled = true;
  try {
    await saveDraft();
  } catch (error) {
    setStatus(error.message, "error");
  } finally {
    $("#saveDraft").disabled = false;
  }
});
$("#substackDraftButton").addEventListener("click", () => sendToSubstack("review"));
$("#publishButton").addEventListener("click", () => sendToSubstack("publish"));
$("#syncButton").addEventListener("click", () => syncDrafts());
loadCurrent().catch(() => {});
