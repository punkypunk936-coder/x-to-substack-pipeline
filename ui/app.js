const $ = (selector) => document.querySelector(selector);

const TEXT_BLOCK_TYPES = new Set(["paragraph", "heading", "subheading", "pull_quote", "quote", "code"]);
const LIST_BLOCK_TYPES = new Set(["bullet_list", "numbered_list"]);
const BLOCK_LABELS = {
  paragraph: "Paragraph",
  heading: "Heading",
  subheading: "Subheading",
  pull_quote: "Pull quote",
  quote: "Quote",
  bullet_list: "Bulleted list",
  numbered_list: "Numbered list",
  code: "Code",
};
const INSERT_BLOCK_LABELS = { ...BLOCK_LABELS, divider: "Divider" };

let currentDraft = null;
let currentPipeline = null;
let editorBlocks = [];
let editorDirty = false;
let editorRevision = 0;
let editorSession = 0;
let saveInFlight = null;
let autosaveTimer = null;
let activeBlockId = null;
let mediaSourceMode = "upload";
let mediaEditingId = null;
let draggedBlockId = null;
let pipelineRenderKey = "";
let pipelineRefreshTimer = null;
let pipelineRefreshInFlight = false;
let syncInFlight = false;
let ingestConfigured = false;
let publishConfigured = false;
let conflictRetryMode = "review";

function setStatus(message, mode = "idle") {
  const node = $("#statusLine");
  node.textContent = message;
  node.dataset.mode = mode;
}

function setSaveState(message, mode = "saved") {
  const node = $("#saveState");
  node.textContent = message;
  node.className = `save-state ${mode === "saved" ? "" : mode}`.trim();
}

function configureConnections(data) {
  ingestConfigured = Boolean(data.ingest?.configured);
  publishConfigured = Boolean(data.publish?.configured);

  const ingestMessage = data.ingest?.message || "Official X API connection is not configured.";
  const publishMessage = data.publish?.message || "No browser-free publishing connection is available.";
  const ingestButton = $("#ingestButton");
  const draftButton = $("#substackDraftButton");
  const publishButton = $("#publishButton");

  ingestButton.disabled = !ingestConfigured;
  ingestButton.title = ingestMessage;
  draftButton.disabled = !publishConfigured;
  draftButton.title = publishMessage;
  publishButton.disabled = !publishConfigured;
  publishButton.title = publishMessage;
}

function uid() {
  if (window.crypto?.randomUUID) return window.crypto.randomUUID().replaceAll("-", "").slice(0, 12);
  return `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`;
}

function escapeHtml(value) {
  return String(value || "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function sanitizeInline(value) {
  const template = document.createElement("template");
  template.innerHTML = String(value || "");
  const allowed = new Set(["A", "B", "BR", "CODE", "EM", "I", "MARK", "S", "STRIKE", "STRONG", "SUB", "SUP", "U"]);
  const nodes = [...template.content.querySelectorAll("*")];
  for (const node of nodes) {
    if (!allowed.has(node.tagName)) {
      node.replaceWith(...node.childNodes);
      continue;
    }
    const rawHref = node.tagName === "A" ? node.getAttribute("href") || "" : "";
    for (const attribute of [...node.attributes]) node.removeAttribute(attribute.name);
    if (node.tagName === "A") {
      let safeHref = "";
      try {
        const parsed = new URL(rawHref, window.location.href);
        if (["http:", "https:", "mailto:"].includes(parsed.protocol)) safeHref = parsed.href;
      } catch {}
      if (safeHref) {
        node.setAttribute("href", safeHref);
        node.setAttribute("target", "_blank");
        node.setAttribute("rel", "noopener noreferrer");
      } else {
        node.replaceWith(...node.childNodes);
      }
    }
  }
  return template.innerHTML.trim();
}

function plainText(value) {
  const container = document.createElement("div");
  container.innerHTML = String(value || "");
  container.querySelectorAll("br").forEach((lineBreak) => lineBreak.replaceWith(" "));
  return (container.textContent || "").trim();
}

function normalizeBlock(raw = {}) {
  const type = raw.type || "paragraph";
  const block = { id: raw.id || uid(), type };
  if (TEXT_BLOCK_TYPES.has(type)) block.html = sanitizeInline(raw.html ?? raw.text ?? "");
  if (LIST_BLOCK_TYPES.has(type)) {
    const items = Array.isArray(raw.items) ? raw.items : [raw.html || raw.text || ""];
    block.items = items.map(sanitizeInline);
    if (!block.items.length) block.items = [""];
  }
  if (type === "image") {
    Object.assign(block, {
      url: String(raw.url || ""),
      alt: String(raw.alt || ""),
      caption: String(raw.caption || ""),
      layout: ["regular", "wide", "full"].includes(raw.layout) ? raw.layout : "regular",
    });
  }
  if (type === "embed") Object.assign(block, { url: String(raw.url || ""), caption: String(raw.caption || "") });
  return block;
}

function draftBlocks(draft) {
  if (Array.isArray(draft.blocks) && draft.blocks.length) return draft.blocks.map(normalizeBlock);
  const blocks = String(draft.body || "")
    .split(/\n{2,}/)
    .filter((part) => part.trim())
    .map((part) => {
      const text = part.trim();
      if (text.startsWith("## ")) return normalizeBlock({ type: "heading", html: escapeHtml(text.slice(3)) });
      return normalizeBlock({ type: "paragraph", html: escapeHtml(text).replaceAll("\n", "<br>") });
    });
  for (const media of draft.media || []) {
    if (media?.url) blocks.push(normalizeBlock({ type: media.type === "image" ? "image" : "embed", ...media }));
  }
  return blocks.length ? blocks : [normalizeBlock()];
}

function blockWords(block) {
  if (TEXT_BLOCK_TYPES.has(block.type)) return plainText(block.html);
  if (LIST_BLOCK_TYPES.has(block.type)) return (block.items || []).map(plainText).join(" ");
  return [block.alt, block.caption].filter(Boolean).join(" ");
}

function wordCount(blocks = editorBlocks) {
  return blocks
    .map(blockWords)
    .join(" ")
    .trim()
    .split(/\s+/)
    .filter(Boolean).length;
}

function mediaCount(blocks = editorBlocks) {
  return blocks.filter((block) => block.type === "image" || block.type === "embed").length;
}

function blocksHtml(blocks) {
  return blocks.map((block) => {
    if (block.type === "paragraph") return `<p>${sanitizeInline(block.html) || "<br>"}</p>`;
    if (block.type === "heading") return `<h2>${sanitizeInline(block.html)}</h2>`;
    if (block.type === "subheading") return `<h3>${sanitizeInline(block.html)}</h3>`;
    if (block.type === "pull_quote") return `<blockquote class="pull-quote"><p>${sanitizeInline(block.html)}</p></blockquote>`;
    if (block.type === "quote") return `<blockquote><p>${sanitizeInline(block.html)}</p></blockquote>`;
    if (block.type === "code") return `<pre><code>${escapeHtml(plainText(block.html))}</code></pre>`;
    if (LIST_BLOCK_TYPES.has(block.type)) {
      const tag = block.type === "bullet_list" ? "ul" : "ol";
      return `<${tag}>${(block.items || []).map((item) => `<li>${sanitizeInline(item)}</li>`).join("")}</${tag}>`;
    }
    if (block.type === "divider") return "<hr>";
    if (block.type === "image") {
      const caption = block.caption ? `<figcaption>${escapeHtml(block.caption)}</figcaption>` : "";
      return `<figure data-layout="${escapeHtml(block.layout || "regular")}"><img src="${escapeHtml(block.url)}" alt="${escapeHtml(block.alt)}">${caption}</figure>`;
    }
    if (block.type === "embed") {
      return `<p class="embed"><a href="${escapeHtml(block.url)}" target="_blank" rel="noopener">${escapeHtml(block.caption || block.url)}</a></p>`;
    }
    return "";
  }).join("");
}

function draftHtml(draft) {
  const title = escapeHtml(draft.title || "Untitled X article");
  const subtitle = draft.subtitle ? `<p>${escapeHtml(draft.subtitle)}</p>` : "";
  return `<h1>${title}</h1>${subtitle}${blocksHtml(draftBlocks(draft))}`;
}

function renderDraft(draft) {
  editorSession += 1;
  editorRevision = 0;
  currentDraft = draft;
  editorBlocks = draftBlocks(draft);
  $("#workspace").classList.remove("hidden");
  $("#draftTitle").textContent = draft.title || "Untitled X article";
  $("#draftSubtitle").textContent = draft.subtitle || "";
  $("#draftMeta").textContent = `${draft.source || "source"} / ${wordCount(editorBlocks)} words / ${mediaCount(editorBlocks)} media item(s)`;
  $("#editTitle").value = draft.title || "";
  $("#editSubtitle").value = draft.subtitle || "";
  editorDirty = false;
  activeBlockId = editorBlocks[0]?.id || null;
  renderBlocks();
  updateEditMeta();
  renderInlinePreview();
  setSaveState("Saved locally");
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

function pipelineKey(pipeline) {
  return JSON.stringify({
    selected_id: pipeline.selected_id || null,
    account: pipeline.account || "",
    items: (pipeline.items || []).map((item) => [
      item.id,
      item.title,
      item.status,
      item.word_count,
      item.media_count,
      item.updated_at,
      item.substack_url,
      item.substack_sync_state,
    ]),
    sync: [
      pipeline.sync?.status,
      pipeline.sync?.last_sync,
      pipeline.sync?.last_error,
    ],
  });
}

function renderPipeline(pipeline, { force = false } = {}) {
  currentPipeline = pipeline;
  const nextKey = pipelineKey(pipeline);
  if (!force && nextKey === pipelineRenderKey) return false;
  pipelineRenderKey = nextKey;
  $("#accountLabel").textContent = pipeline.account || "@0xgoodie";
  const draftList = $("#draftList");
  const publishedList = $("#publishedList");
  draftList.replaceChildren();
  publishedList.replaceChildren();
  const activeItems = (pipeline.items || []).filter((item) => item.status !== "published");
  const publishedItems = (pipeline.items || []).filter((item) => item.status === "published");

  const renderItem = (item, list) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "draft-item";
    button.classList.toggle("active", String(item.id) === String(pipeline.selected_id));
    button.dataset.draftId = item.id;

    const status = document.createElement("span");
    status.className = `draft-status ${item.status === "published" ? "published" : ""}`;
    status.textContent = item.status === "published"
      ? "Published"
      : ["draft_unverified", "publish_unverified"].includes(item.substack_sync_state)
        ? "Needs verification"
        : item.substack_sync_state === "draft_verified"
          ? "Substack draft"
          : "Draft";

    const title = document.createElement("strong");
    title.textContent = item.title || "Untitled X article";

    const meta = document.createElement("span");
    meta.className = "draft-item-meta";
    meta.textContent = `${item.word_count || 0} words · ${item.media_count || 0} media`;

    button.append(status, title, meta);
    button.addEventListener("click", () => {
      if (item.status === "published" && item.substack_url) {
        window.open(item.substack_url, "_blank", "noopener");
        return;
      }
      selectDraft(item.id);
    });
    list.appendChild(button);
  };

  activeItems.forEach((item) => renderItem(item, draftList));
  publishedItems.forEach((item) => renderItem(item, publishedList));
  $("#emptyDrafts").classList.toggle("hidden", activeItems.length > 0);
  $("#publishedHistory").classList.toggle("hidden", publishedItems.length === 0);
  $("#publishedCount").textContent = String(publishedItems.length);
  $("#workspace").classList.toggle("hidden", !(pipeline.items || []).length);
  const sync = pipeline.sync || {};
  $("#syncLine").textContent = sync.status === "syncing" ? "Checking X and Substack..." : formatSyncTime(sync.last_sync);
  return true;
}

function makeToolButton(label, title, handler) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "tool-button";
  button.textContent = label;
  button.title = title;
  button.setAttribute("aria-label", title);
  button.addEventListener("click", handler);
  return button;
}

function blockTypeSelect(block) {
  const select = document.createElement("select");
  select.className = "block-type-control";
  select.setAttribute("aria-label", "Change block type");
  for (const [value, label] of Object.entries(BLOCK_LABELS)) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    option.selected = value === block.type;
    select.appendChild(option);
  }
  select.addEventListener("change", () => changeBlockType(block.id, select.value));
  return select;
}

function inlineBlockInserter(block) {
  const wrapper = document.createElement("div");
  wrapper.className = "inline-block-inserter";
  const select = document.createElement("select");
  select.className = "inline-block-select";
  select.title = "Insert a block directly below";
  select.setAttribute("aria-label", "Insert block below");
  const placeholder = document.createElement("option");
  placeholder.value = "";
  placeholder.textContent = "+ Add below";
  placeholder.selected = true;
  placeholder.disabled = true;
  select.appendChild(placeholder);
  for (const [value, label] of Object.entries(INSERT_BLOCK_LABELS)) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    select.appendChild(option);
  }
  select.addEventListener("change", () => {
    const type = select.value;
    if (!type) return;
    insertBlock({ id: uid(), type }, block.id);
  });
  wrapper.appendChild(select);
  return wrapper;
}

function blockControls(block, index) {
  const controls = document.createElement("div");
  controls.className = "block-controls";
  if (TEXT_BLOCK_TYPES.has(block.type) || LIST_BLOCK_TYPES.has(block.type)) controls.appendChild(blockTypeSelect(block));
  if (block.type === "image") {
    const editButton = makeToolButton("Edit", "Edit image", () => openMediaDialog(block.id));
    editButton.classList.add("media-edit-button");
    controls.appendChild(editButton);
  }
  controls.appendChild(makeToolButton("↑", "Move block up", () => moveBlock(block.id, -1)));
  controls.appendChild(makeToolButton("↓", "Move block down", () => moveBlock(block.id, 1)));
  controls.appendChild(makeToolButton("×", "Delete block", () => deleteBlock(block.id)));
  controls.querySelectorAll("button").forEach((button) => {
    if (button.title === "Move block up") button.disabled = index === 0;
    if (button.title === "Move block down") button.disabled = index === editorBlocks.length - 1;
  });
  return controls;
}

function textContentNode(block) {
  const content = document.createElement("div");
  content.className = "block-content";
  content.contentEditable = "true";
  content.spellcheck = block.type !== "code";
  content.dataset.placeholder = BLOCK_LABELS[block.type] || "Write something";
  content.innerHTML = sanitizeInline(block.html);
  content.addEventListener("input", () => {
    block.html = sanitizeInline(content.innerHTML);
    markDirty();
  });
  content.addEventListener("focus", () => { activeBlockId = block.id; });
  content.addEventListener("keydown", (event) => handleTextKeydown(event, block, content));
  return content;
}

function listContentNode(block) {
  const list = document.createElement(block.type === "bullet_list" ? "ul" : "ol");
  list.className = "block-list";
  const syncItems = () => {
    block.items = [...list.querySelectorAll(".list-item")].map((item) => sanitizeInline(item.innerHTML));
  };
  const makeItem = (value = "") => {
    const item = document.createElement("li");
    item.className = "list-item";
    item.contentEditable = "true";
    item.spellcheck = true;
    item.dataset.placeholder = "List item";
    item.innerHTML = sanitizeInline(value);
    item.addEventListener("keydown", (event) => {
      if (handleFormattingShortcut(event)) return;
      if (event.key === "Enter") {
        event.preventDefault();
        const selection = window.getSelection();
        let before = sanitizeInline(item.innerHTML);
        let after = "";
        if (selection?.rangeCount && item.contains(selection.anchorNode)) {
          const range = selection.getRangeAt(0);
          const beforeRange = document.createRange();
          beforeRange.selectNodeContents(item);
          beforeRange.setEnd(range.startContainer, range.startOffset);
          const afterRange = document.createRange();
          afterRange.selectNodeContents(item);
          afterRange.setStart(range.endContainer, range.endOffset);
          const beforeBox = document.createElement("div");
          const afterBox = document.createElement("div");
          beforeBox.appendChild(beforeRange.cloneContents());
          afterBox.appendChild(afterRange.cloneContents());
          before = sanitizeInline(beforeBox.innerHTML);
          after = sanitizeInline(afterBox.innerHTML);
        }
        item.innerHTML = before;
        const next = makeItem(after);
        item.after(next);
        syncItems();
        markDirty();
        next.focus();
      } else if (event.key === "Backspace" && !plainText(item.innerHTML) && list.children.length > 1) {
        event.preventDefault();
        const previous = item.previousElementSibling || item.nextElementSibling;
        item.remove();
        syncItems();
        markDirty();
        previous?.focus();
      }
    });
    return item;
  };
  for (const value of block.items || [""]) list.appendChild(makeItem(value));
  list.addEventListener("input", () => {
    syncItems();
    markDirty();
  });
  list.addEventListener("focusin", () => { activeBlockId = block.id; });
  return list;
}

function imageContentNode(block) {
  const figure = document.createElement("figure");
  figure.className = "image-block-editor";
  figure.dataset.layout = block.layout || "regular";
  const image = document.createElement("img");
  image.src = block.url;
  image.alt = block.alt || "";
  const caption = document.createElement("input");
  caption.className = "image-caption";
  caption.placeholder = "Add caption";
  caption.value = block.caption || "";
  caption.addEventListener("input", () => {
    block.caption = caption.value;
    markDirty();
  });
  caption.addEventListener("focus", () => { activeBlockId = block.id; });
  figure.append(image, caption);
  return figure;
}

function embedContentNode(block) {
  const wrapper = document.createElement("div");
  wrapper.className = "embed-block-editor";
  const link = document.createElement("a");
  link.href = block.url;
  link.target = "_blank";
  link.rel = "noopener";
  link.textContent = block.url;
  const caption = document.createElement("input");
  caption.className = "embed-caption";
  caption.placeholder = "Add caption";
  caption.value = block.caption || "";
  caption.addEventListener("input", () => {
    block.caption = caption.value;
    markDirty();
  });
  wrapper.append(link, caption);
  return wrapper;
}

function renderBlocks({ focusId = null } = {}) {
  const editor = $("#blockEditor");
  editor.replaceChildren();
  editorBlocks.forEach((block, index) => {
    const row = document.createElement("section");
    row.className = "editor-block";
    row.dataset.blockId = block.id;
    row.dataset.type = block.type;

    const grip = makeToolButton("⋮⋮", "Drag to reorder", () => {});
    grip.classList.add("block-grip");
    grip.draggable = true;
    grip.addEventListener("dragstart", (event) => {
      draggedBlockId = block.id;
      event.dataTransfer.effectAllowed = "move";
      event.dataTransfer.setData("text/plain", block.id);
    });
    grip.addEventListener("dragend", () => { draggedBlockId = null; });

    let content;
    if (TEXT_BLOCK_TYPES.has(block.type)) content = textContentNode(block);
    else if (LIST_BLOCK_TYPES.has(block.type)) content = listContentNode(block);
    else if (block.type === "image") content = imageContentNode(block);
    else if (block.type === "embed") content = embedContentNode(block);
    else {
      content = document.createElement("hr");
      content.className = "block-divider";
    }
    row.append(grip, content, blockControls(block, index), inlineBlockInserter(block));
    editor.appendChild(row);
  });
  if (focusId) focusBlock(focusId);
}

function focusBlock(id, atEnd = false) {
  window.requestAnimationFrame(() => {
    const row = document.querySelector(`[data-block-id="${CSS.escape(id)}"]`);
    const target = row?.querySelector("[contenteditable='true'], input");
    if (!target) return;
    target.focus();
    if (atEnd && target.isContentEditable) {
      const selection = window.getSelection();
      const range = document.createRange();
      range.selectNodeContents(target);
      range.collapse(false);
      selection.removeAllRanges();
      selection.addRange(range);
    }
  });
}

function handleTextKeydown(event, block, content) {
  if (handleFormattingShortcut(event)) return;
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    const selection = window.getSelection();
    let before = sanitizeInline(content.innerHTML);
    let after = "";
    if (selection?.rangeCount && content.contains(selection.anchorNode)) {
      const range = selection.getRangeAt(0);
      const beforeRange = document.createRange();
      beforeRange.selectNodeContents(content);
      beforeRange.setEnd(range.startContainer, range.startOffset);
      const afterRange = document.createRange();
      afterRange.selectNodeContents(content);
      afterRange.setStart(range.endContainer, range.endOffset);
      const beforeBox = document.createElement("div");
      const afterBox = document.createElement("div");
      beforeBox.appendChild(beforeRange.cloneContents());
      afterBox.appendChild(afterRange.cloneContents());
      before = sanitizeInline(beforeBox.innerHTML);
      after = sanitizeInline(afterBox.innerHTML);
    }
    block.html = before;
    const next = normalizeBlock({ type: "paragraph", html: after });
    insertBlock(next, block.id);
    return;
  }
  if (event.key === "Backspace" && !plainText(content.innerHTML) && editorBlocks.length > 1) {
    event.preventDefault();
    const index = editorBlocks.findIndex((item) => item.id === block.id);
    const previous = editorBlocks[index - 1];
    editorBlocks.splice(index, 1);
    markDirty({ render: false });
    renderBlocks({ focusId: previous?.id || editorBlocks[0].id });
  }
}

function insertBlock(block, afterId = activeBlockId) {
  const index = editorBlocks.findIndex((item) => item.id === afterId);
  editorBlocks.splice(index >= 0 ? index + 1 : editorBlocks.length, 0, normalizeBlock(block));
  activeBlockId = block.id;
  markDirty({ render: false });
  renderBlocks({ focusId: block.id });
}

function moveBlock(id, direction) {
  const index = editorBlocks.findIndex((block) => block.id === id);
  const nextIndex = index + direction;
  if (index < 0 || nextIndex < 0 || nextIndex >= editorBlocks.length) return;
  const [block] = editorBlocks.splice(index, 1);
  editorBlocks.splice(nextIndex, 0, block);
  activeBlockId = id;
  markDirty({ render: false });
  renderBlocks({ focusId: id });
}

function deleteBlock(id) {
  const index = editorBlocks.findIndex((block) => block.id === id);
  if (index < 0) return;
  editorBlocks.splice(index, 1);
  if (!editorBlocks.length) editorBlocks.push(normalizeBlock());
  activeBlockId = editorBlocks[Math.max(0, index - 1)]?.id || editorBlocks[0].id;
  markDirty({ render: false });
  renderBlocks({ focusId: activeBlockId });
}

function changeBlockType(id, nextType) {
  const block = editorBlocks.find((item) => item.id === id);
  if (!block || block.type === nextType) return;
  const text = TEXT_BLOCK_TYPES.has(block.type) ? plainText(block.html) : (block.items || []).map(plainText).join("\n");
  block.type = nextType;
  if (TEXT_BLOCK_TYPES.has(nextType)) {
    block.html = escapeHtml(text).replaceAll("\n", "<br>");
    delete block.items;
  } else {
    block.items = text.split(/\n+/).filter(Boolean).map(escapeHtml);
    if (!block.items.length) block.items = [""];
    delete block.html;
  }
  markDirty({ render: false });
  renderBlocks({ focusId: id });
}

function markDirty({ render = true } = {}) {
  editorDirty = true;
  editorRevision += 1;
  setSaveState("Unsaved changes", "saving");
  if (render) {
    updateEditMeta();
    renderInlinePreview();
  } else {
    updateEditMeta();
  }
  window.clearTimeout(autosaveTimer);
  autosaveTimer = window.setTimeout(() => {
    saveDraft({ quiet: true, autosave: true }).catch((error) => {
      editorDirty = true;
      setSaveState("Autosave failed", "error");
      setStatus(`Autosave failed: ${error.message}`, "error");
    });
  }, 1200);
}

function updateEditMeta() {
  $("#editMeta").textContent = `${wordCount()} words · ${editorBlocks.length} blocks · ${mediaCount()} media`;
}

function captureVisibleEditorState() {
  let changed = false;
  for (const block of editorBlocks) {
    const row = document.querySelector(`[data-block-id="${CSS.escape(block.id)}"]`);
    if (!row) continue;
    if (TEXT_BLOCK_TYPES.has(block.type)) {
      const content = row.querySelector(".block-content[contenteditable='true']");
      const next = sanitizeInline(content?.innerHTML || "");
      if (next !== block.html) {
        block.html = next;
        changed = true;
      }
    } else if (LIST_BLOCK_TYPES.has(block.type)) {
      const next = [...row.querySelectorAll(".list-item")].map((item) => sanitizeInline(item.innerHTML));
      if (JSON.stringify(next) !== JSON.stringify(block.items || [])) {
        block.items = next;
        changed = true;
      }
    } else if (block.type === "image") {
      const caption = row.querySelector(".image-caption")?.value || "";
      if (caption !== block.caption) {
        block.caption = caption;
        changed = true;
      }
    } else if (block.type === "embed") {
      const caption = row.querySelector(".embed-caption")?.value || "";
      if (caption !== block.caption) {
        block.caption = caption;
        changed = true;
      }
    }
  }
  if (changed) {
    editorDirty = true;
    editorRevision += 1;
    setSaveState("Unsaved changes", "saving");
    updateEditMeta();
    renderInlinePreview();
  }
  return editorBlocks;
}

function renderInlinePreview() {
  $("#previewTitle").textContent = $("#editTitle").value || "Untitled";
  const subtitle = $("#editSubtitle").value.trim();
  $("#previewSubtitle").textContent = subtitle;
  $("#previewSubtitle").classList.toggle("hidden", !subtitle);
  $("#previewBody").innerHTML = blocksHtml(editorBlocks);
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

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "content-type": "application/json", ...(options.headers || {}) },
  });
  let data;
  try {
    data = await response.json();
  } catch {
    throw new Error(`The dashboard returned an unreadable response (${response.status}). Your local draft was not marked saved.`);
  }
  if (!response.ok || data.error) {
    const error = new Error(data.error || `Request failed: ${response.status}`);
    error.code = data.code || "request_failed";
    throw error;
  }
  return data;
}

async function saveDraft({ quiet = false, autosave = false } = {}) {
  if (!currentDraft) throw new Error("Create a draft first.");
  if (saveInFlight) {
    await saveInFlight;
    return editorDirty ? saveDraft({ quiet, autosave }) : currentDraft;
  }
  const operation = (async () => {
    captureVisibleEditorState();
    window.clearTimeout(autosaveTimer);
    const savingRevision = editorRevision;
    const savingSession = editorSession;
    const snapshot = {
      title: $("#editTitle").value,
      subtitle: $("#editSubtitle").value,
      blocks: JSON.parse(JSON.stringify(editorBlocks)),
    };
    setSaveState(autosave ? "Autosaving..." : "Saving...", "saving");
    const data = await api("/api/draft", {
      method: "POST",
      body: JSON.stringify(snapshot),
    });
    if (savingSession !== editorSession) return data.draft;
    currentDraft = data.draft;
    renderPipeline(data.pipeline);
    if (savingRevision === editorRevision) {
      editorBlocks = draftBlocks(data.draft);
      editorDirty = false;
      $("#draftTitle").textContent = data.draft.title;
      $("#draftSubtitle").textContent = data.draft.subtitle || "";
      $("#draftMeta").textContent = `${data.draft.source || "source"} / ${wordCount()} words / ${mediaCount()} media item(s)`;
      setSaveState(autosave ? "Autosaved" : "Saved locally");
      if (!quiet) setStatus("Draft changes saved.", "ok");
    } else {
      editorDirty = true;
      setSaveState("Newer changes not saved yet", "saving");
      window.clearTimeout(autosaveTimer);
      autosaveTimer = window.setTimeout(() => {
        saveDraft({ quiet: true, autosave: true }).catch((error) => {
          setSaveState("Autosave failed", "error");
          setStatus(`Autosave failed: ${error.message}`, "error");
        });
      }, 250);
    }
    return data.draft;
  })();
  saveInFlight = operation;
  try {
    return await operation;
  } finally {
    if (saveInFlight === operation) saveInFlight = null;
  }
}

async function prepareSavedDraft() {
  window.clearTimeout(autosaveTimer);
  while (editorDirty || saveInFlight) await saveDraft({ quiet: true });
}

async function selectDraft(id) {
  if (editorDirty && !window.confirm("Discard unsaved changes and open another draft?")) return;
  setStatus("Opening draft...", "busy");
  try {
    await openDraft(id);
    setStatus("Draft ready.", "ok");
  } catch (error) {
    setStatus(error.message, "error");
  }
}

async function openDraft(id) {
  const data = await api("/api/drafts/select", { method: "POST", body: JSON.stringify({ id }) });
  renderPipeline(data.pipeline);
  renderDraft(data.draft);
  setDraftView("edit");
  return data.draft;
}

async function syncDrafts({ silent = false } = {}) {
  if (syncInFlight) return;
  syncInFlight = true;
  window.clearTimeout(pipelineRefreshTimer);
  const button = $("#syncButton");
  button.disabled = true;
  if (!silent) setStatus("Checking @0xgoodie and Substack...", "busy");
  $("#syncLine").textContent = "Checking X and Substack...";
  try {
    const data = await api("/api/drafts/sync", { method: "POST", body: "{}" });
    renderPipeline(data.pipeline);
    const addedIds = data.added || [];
    const upgradedIds = data.upgraded || [];
    const newlyPublished = data.newly_published || [];
    const targetId = addedIds[0] || data.pipeline.selected_id;
    if (!editorDirty && upgradedIds.includes(String(currentDraft?.id || ""))) {
      await openDraft(currentDraft.id);
    } else if (
      !editorDirty
      && targetId
      && String(currentDraft?.id || "") !== String(targetId)
    ) {
      await openDraft(targetId);
    }
    const updates = [];
    if (addedIds.length) updates.push(`${addedIds.length} new X article draft${addedIds.length === 1 ? "" : "s"} added`);
    if (upgradedIds.length) updates.push(`${upgradedIds.length} existing draft${upgradedIds.length === 1 ? "" : "s"} restored to the X layout`);
    if (newlyPublished.length) updates.push(`${newlyPublished.length} Substack post${newlyPublished.length === 1 ? "" : "s"} marked published`);
    if (!silent || updates.length) {
      setStatus(updates.length ? `${updates.join(". ")}.` : "X and Substack are fully reconciled.", "ok");
    }
  } catch (error) {
    $("#syncLine").textContent = "Sync needs attention.";
    if (!silent) setStatus(error.message, "error");
  } finally {
    button.disabled = false;
    syncInFlight = false;
    schedulePipelineRefresh();
  }
}

async function ingest() {
  if (!ingestConfigured) {
    setStatus("The rich X Article read API is unavailable right now. Try again shortly.", "error");
    return;
  }
  const url = $("#xUrl").value.trim();
  if (!url) {
    setStatus("Paste an X article URL first.", "error");
    return;
  }
  $("#ingestButton").disabled = true;
  setStatus("Capturing the X article and building its rich draft...", "busy");
  try {
    const data = await api("/api/ingest", { method: "POST", body: JSON.stringify({ url }) });
    renderPipeline(data.pipeline);
    renderDraft(data.draft);
    setStatus("Draft ready to edit or publish.", "ok");
  } catch (error) {
    setStatus(error.message, "error");
  } finally {
    $("#ingestButton").disabled = !ingestConfigured;
  }
}

async function createBlankDraft() {
  if (editorDirty && !window.confirm("Start a blank draft and leave the current unsaved changes?")) return;
  const button = $("#newDraftButton");
  button.disabled = true;
  setStatus("Opening a blank draft...", "busy");
  try {
    const data = await api("/api/drafts/new", { method: "POST", body: "{}" });
    renderPipeline(data.pipeline);
    renderDraft(data.draft);
    setDraftView("edit");
    $("#editTitle").focus();
    $("#editTitle").select();
    setStatus("Blank draft ready.", "ok");
  } catch (error) {
    setStatus(error.message, "error");
  } finally {
    button.disabled = false;
  }
}

async function sendToSubstack(mode) {
  if (!publishConfigured) {
    setStatus("Substack has no browser-free write API. This dashboard will not open Chrome or fake a publish.", "error");
    return;
  }
  if (!currentDraft) {
    setStatus("Create a draft first.", "error");
    return;
  }
  const button = mode === "publish" ? $("#publishButton") : $("#substackDraftButton");
  $("#publishButton").disabled = true;
  $("#substackDraftButton").disabled = true;
  try {
    captureVisibleEditorState();
    await prepareSavedDraft();
    if (mode === "publish") {
      const confirmed = window.confirm(`Publish “${currentDraft.title}” to Everyone now? Substack will also send it by email and in the Substack app.`);
      if (!confirmed) return;
    }
    captureVisibleEditorState();
    await prepareSavedDraft();
    const publishSnapshot = {
      title: $("#editTitle").value,
      subtitle: $("#editSubtitle").value,
      blocks: JSON.parse(JSON.stringify(editorBlocks)),
    };
    $("#editor").inert = true;
    $("#publishLine").textContent = mode === "publish" ? "Publishing to Substack..." : "Saving a Substack draft in the background...";
    setStatus(mode === "publish" ? "Publishing the saved rich draft..." : "Sending the saved rich draft to Substack...", "busy");
    const result = await api("/api/publish", {
      method: "POST",
      body: JSON.stringify({
        mode,
        confirm_publish: mode === "publish",
        draft: publishSnapshot,
      }),
    });
    if (result.pipeline) renderPipeline(result.pipeline);
    $("#publishLine").textContent = result.message || result.status || "Substack step finished.";
    setStatus(result.message || "Substack step finished.", result.ok ? "ok" : "error");
  } catch (error) {
    $("#publishLine").textContent = error.message;
    setStatus(error.message, "error");
    if (error.code === "substack_conflict") {
      conflictRetryMode = mode;
      $("#conflictMessage").textContent = error.message;
      $("#conflictDialog").showModal();
    }
  } finally {
    $("#editor").inert = false;
    $("#publishButton").disabled = !publishConfigured;
    $("#substackDraftButton").disabled = !publishConfigured;
  }
}

async function resolveSubstackConflict(action) {
  const keepButton = $("#keepDashboardCopy");
  const useButton = $("#useSubstackCopy");
  keepButton.disabled = true;
  useButton.disabled = true;
  try {
    const data = await api("/api/substack/conflict", {
      method: "POST",
      body: JSON.stringify({ action }),
    });
    renderPipeline(data.pipeline);
    renderDraft(data.draft);
    $("#conflictDialog").close();
    if (action === "keep_dashboard") {
      setStatus("Dashboard copy kept. Rechecking it before Substack save.", "busy");
      await sendToSubstack(conflictRetryMode);
    } else {
      setStatus("Loaded the Substack copy. The previous dashboard revision is backed up.", "ok");
    }
  } catch (error) {
    setStatus(error.message, "error");
  } finally {
    keepButton.disabled = false;
    useButton.disabled = false;
  }
}

function setMediaSourceMode(mode) {
  mediaSourceMode = mode;
  const uploading = mode === "upload";
  $("#uploadMediaTab").classList.toggle("active", uploading);
  $("#urlMediaTab").classList.toggle("active", !uploading);
  $("#uploadMediaTab").setAttribute("aria-selected", String(uploading));
  $("#urlMediaTab").setAttribute("aria-selected", String(!uploading));
  $("#uploadMediaPanel").classList.toggle("hidden", !uploading);
  $("#urlMediaPanel").classList.toggle("hidden", uploading);
}

function openMediaDialog(blockId = null) {
  mediaEditingId = blockId;
  const block = editorBlocks.find((item) => item.id === blockId);
  $("#mediaForm").reset();
  $("#mediaFileName").textContent = "PNG, JPEG, WebP, or GIF";
  $("#mediaAlt").value = block?.alt || "";
  $("#mediaCaption").value = block?.caption || "";
  $("#mediaLayout").value = block?.layout || "regular";
  $("#mediaUrl").value = block?.url?.startsWith("http") ? block.url : "";
  setMediaSourceMode(block?.url?.startsWith("http") ? "url" : "upload");
  $("#mediaDialog").showModal();
}

function closeMediaDialog() {
  mediaEditingId = null;
  $("#mediaDialog").close();
}

function fileAsDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(new Error("That image could not be read."));
    reader.readAsDataURL(file);
  });
}

async function submitMedia(event) {
  event.preventDefault();
  const button = $("#insertMedia");
  button.disabled = true;
  try {
    const existing = editorBlocks.find((block) => block.id === mediaEditingId);
    let url = existing?.url || "";
    if (mediaSourceMode === "upload") {
      const file = $("#mediaFile").files[0];
      if (file) {
        const data = await api("/api/media", {
          method: "POST",
          body: JSON.stringify({ name: file.name, type: file.type, data: await fileAsDataUrl(file) }),
        });
        url = data.media.url;
      } else if (!url) {
        throw new Error("Choose an image first.");
      }
    } else {
      url = $("#mediaUrl").value.trim();
      if (!/^https?:\/\//i.test(url)) throw new Error("Enter a complete image URL.");
    }
    const imageBlock = normalizeBlock({
      id: existing?.id || uid(),
      type: "image",
      url,
      alt: $("#mediaAlt").value.trim(),
      caption: $("#mediaCaption").value.trim(),
      layout: $("#mediaLayout").value,
    });
    if (existing) Object.assign(existing, imageBlock);
    else insertBlock(imageBlock);
    markDirty({ render: false });
    renderBlocks({ focusId: imageBlock.id });
    renderInlinePreview();
    closeMediaDialog();
  } catch (error) {
    setStatus(error.message, "error");
  } finally {
    button.disabled = false;
  }
}

function addEmbed() {
  const url = window.prompt("Paste the media or embed URL:", "https://");
  if (!url) return;
  if (!/^https?:\/\//i.test(url)) {
    setStatus("Enter a complete media URL.", "error");
    return;
  }
  const caption = window.prompt("Caption (optional):", "") || "";
  const block = normalizeBlock({ id: uid(), type: "embed", url, caption });
  insertBlock(block);
}

function applyInlineCommand(command) {
  const selection = window.getSelection();
  const anchor = selection?.anchorNode;
  const anchorElement = anchor?.nodeType === Node.ELEMENT_NODE ? anchor : anchor?.parentElement;
  const editable = anchorElement?.closest?.("[contenteditable='true']");
  if (!editable || !$("#blockEditor").contains(editable)) return;
  if (command === "createLink") {
    const url = window.prompt("Link URL:", "https://");
    if (!url) return;
    document.execCommand(command, false, url);
  } else {
    document.execCommand(command, false);
  }
  const inputTypes = {
    bold: "formatBold",
    italic: "formatItalic",
    underline: "formatUnderline",
    strikeThrough: "formatStrikeThrough",
    createLink: "insertLink",
  };
  editable.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: inputTypes[command] || "formatSetBlockTextDirection" }));
  updateFormattingState();
}

function handleFormattingShortcut(event) {
  if (!(event.metaKey || event.ctrlKey) || event.altKey) return false;
  const key = event.key.toLowerCase();
  let command = null;
  if (!event.shiftKey && key === "b") command = "bold";
  else if (!event.shiftKey && key === "i") command = "italic";
  else if (!event.shiftKey && key === "u") command = "underline";
  else if (!event.shiftKey && key === "k") command = "createLink";
  else if (event.shiftKey && key === "x") command = "strikeThrough";
  if (!command) return false;
  event.preventDefault();
  applyInlineCommand(command);
  return true;
}

function updateFormattingState() {
  const selection = window.getSelection();
  const anchor = selection?.anchorNode;
  const anchorElement = anchor?.nodeType === Node.ELEMENT_NODE ? anchor : anchor?.parentElement;
  const editable = anchorElement?.closest?.("[contenteditable='true']");
  const insideEditor = Boolean(editable && $("#blockEditor").contains(editable));
  document.querySelectorAll(".format-button").forEach((button) => {
    let active = false;
    if (insideEditor) {
      try {
        active = document.queryCommandState(button.dataset.command);
      } catch {}
    }
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });
}

async function copyRichDraft(showStatus = true) {
  if (!currentDraft) return;
  const richDraft = { ...currentDraft, title: $("#editTitle").value, subtitle: $("#editSubtitle").value, blocks: editorBlocks };
  const richHtml = draftHtml(richDraft);
  const text = `${richDraft.title}\n\n${richDraft.subtitle ? `${richDraft.subtitle}\n\n` : ""}${editorBlocks.map(blockWords).join("\n\n")}`;
  try {
    await navigator.clipboard.write([new ClipboardItem({
      "text/html": new Blob([richHtml], { type: "text/html" }),
      "text/plain": new Blob([text], { type: "text/plain" }),
    })]);
    if (showStatus) setStatus("Rich draft copied.", "ok");
  } catch {
    await navigator.clipboard.writeText(text);
    if (showStatus) setStatus("Plain text draft copied.", "ok");
  }
}

async function loadCurrent() {
  const data = await api("/api/bootstrap");
  const pipeline = data.pipeline;
  renderPipeline(pipeline, { force: true });
  if (data.draft) renderDraft(data.draft);
  configureConnections(data);
  $("#loadingWorkspace").classList.add("hidden");
  $("#workspace").classList.add("workspace-ready");
  setStatus(
    ingestConfigured ? "Dashboard ready. API-only capture is active." : data.ingest?.message || "Dashboard ready.",
    ingestConfigured ? "ok" : "error",
  );
  return pipeline;
}

async function refreshPipelineState() {
  if (editorDirty || pipelineRefreshInFlight || syncInFlight) return currentPipeline;
  pipelineRefreshInFlight = true;
  try {
    const pipeline = await api("/api/drafts");
    renderPipeline(pipeline);
    const currentItem = (pipeline.items || []).find(
      (item) => String(item.id || "") === String(currentDraft?.id || ""),
    );
    if (
      currentItem?.updated_at
      && currentDraft?.updated_at
      && currentItem.updated_at !== currentDraft.updated_at
    ) {
      await openDraft(currentItem.id);
      return pipeline;
    }
    if (
      pipeline.selected_id
      && String(currentDraft?.id || "") !== String(pipeline.selected_id)
      && (!currentDraft || currentItem?.status === "published")
    ) {
      await openDraft(pipeline.selected_id);
    }
    return pipeline;
  } catch {
    return currentPipeline;
  } finally {
    pipelineRefreshInFlight = false;
  }
}

function refreshDelay(pipeline = currentPipeline) {
  if (document.hidden) return 60000;
  return pipeline?.sync?.status === "syncing" ? 4000 : 20000;
}

function schedulePipelineRefresh(delay = refreshDelay()) {
  window.clearTimeout(pipelineRefreshTimer);
  pipelineRefreshTimer = window.setTimeout(async () => {
    const pipeline = await refreshPipelineState();
    schedulePipelineRefresh(refreshDelay(pipeline));
  }, delay);
}

$("#blockEditor").addEventListener("dragover", (event) => {
  if (!draggedBlockId) return;
  event.preventDefault();
});
$("#blockEditor").addEventListener("drop", (event) => {
  if (!draggedBlockId) return;
  event.preventDefault();
  const target = event.target.closest(".editor-block");
  if (!target || target.dataset.blockId === draggedBlockId) return;
  const from = editorBlocks.findIndex((block) => block.id === draggedBlockId);
  const to = editorBlocks.findIndex((block) => block.id === target.dataset.blockId);
  if (from < 0 || to < 0) return;
  const [block] = editorBlocks.splice(from, 1);
  editorBlocks.splice(to, 0, block);
  markDirty({ render: false });
  renderBlocks({ focusId: draggedBlockId });
});

$("#ingestButton").addEventListener("click", ingest);
$("#newDraftButton").addEventListener("click", createBlankDraft);
$("#xUrl").addEventListener("keydown", (event) => { if (event.key === "Enter") ingest(); });
$("#editTab").addEventListener("click", () => setDraftView("edit"));
$("#previewTab").addEventListener("click", () => setDraftView("preview"));
$("#cancelEdit").addEventListener("click", () => { if (currentDraft) renderDraft(currentDraft); });
$("#editTitle").addEventListener("input", () => markDirty());
$("#editSubtitle").addEventListener("input", () => markDirty());
$("#editor").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("#saveDraft").disabled = true;
  try {
    await saveDraft();
  } catch (error) {
    setSaveState("Save failed", "error");
    setStatus(error.message, "error");
  } finally {
    $("#saveDraft").disabled = false;
  }
});
$("#addBlockButton").addEventListener("click", () => insertBlock(normalizeBlock({ id: uid(), type: $("#blockTypeSelect").value })));
$("#pullQuoteButton").addEventListener("click", () => insertBlock({ id: uid(), type: "pull_quote" }));
$("#quoteButton").addEventListener("click", () => insertBlock({ id: uid(), type: "quote" }));
$("#dividerButton").addEventListener("click", () => insertBlock({ id: uid(), type: "divider" }));
$("#imageButton").addEventListener("click", () => openMediaDialog());
$("#embedButton").addEventListener("click", addEmbed);
$("#linkButton").addEventListener("mousedown", (event) => { event.preventDefault(); applyInlineCommand("createLink"); });
document.querySelectorAll(".format-button").forEach((button) => {
  button.addEventListener("mousedown", (event) => {
    event.preventDefault();
    applyInlineCommand(button.dataset.command);
  });
});
document.addEventListener("selectionchange", updateFormattingState);
$("#mediaForm").addEventListener("submit", submitMedia);
$("#mediaFile").addEventListener("change", () => {
  const file = $("#mediaFile").files[0];
  $("#mediaFileName").textContent = file ? file.name : "PNG, JPEG, WebP, or GIF";
  if (file && !$("#mediaAlt").value) $("#mediaAlt").value = file.name.replace(/\.[^.]+$/, "").replaceAll(/[-_]/g, " ");
});
$("#uploadMediaTab").addEventListener("click", () => setMediaSourceMode("upload"));
$("#urlMediaTab").addEventListener("click", () => setMediaSourceMode("url"));
$("#closeMediaDialog").addEventListener("click", closeMediaDialog);
$("#cancelMedia").addEventListener("click", closeMediaDialog);
$("#closeConflictDialog").addEventListener("click", () => $("#conflictDialog").close());
$("#keepDashboardCopy").addEventListener("click", () => resolveSubstackConflict("keep_dashboard"));
$("#useSubstackCopy").addEventListener("click", () => resolveSubstackConflict("use_substack"));
$("#substackDraftButton").addEventListener("click", () => sendToSubstack("review"));
$("#publishButton").addEventListener("click", () => sendToSubstack("publish"));
$("#syncButton").addEventListener("click", () => syncDrafts());
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) schedulePipelineRefresh(500);
});
window.addEventListener("beforeunload", (event) => {
  if (!editorDirty) return;
  event.preventDefault();
});

loadCurrent()
  .then(() => schedulePipelineRefresh())
  .catch((error) => {
    $("#loadingWorkspace").classList.add("hidden");
    setStatus(error.message, "error");
  });
