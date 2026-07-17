export function htmlEscape(value) {
  return String(value || "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function inlineHtml(value) {
  return String(value || "");
}

export function draftBlocks(draft) {
  if (Array.isArray(draft.blocks) && draft.blocks.length) return draft.blocks;
  const blocks = String(draft.body || "")
    .split(/\n{2,}/)
    .filter(Boolean)
    .map((paragraph) => paragraph.startsWith("## ")
      ? { type: "heading", html: htmlEscape(paragraph.slice(3)) }
      : { type: "paragraph", html: htmlEscape(paragraph).replaceAll("\n", "<br>") });
  for (const item of draft.media || []) {
    if (item?.url) blocks.push(item.type === "image" ? { type: "image", ...item } : { type: "embed", ...item });
  }
  return blocks;
}

function stripHtml(value) {
  return String(value || "")
    .replaceAll(/<br\s*\/?>/gi, "\n")
    .replaceAll(/<[^>]+>/g, "")
    .replaceAll("&nbsp;", " ")
    .replaceAll("&amp;", "&")
    .replaceAll("&lt;", "<")
    .replaceAll("&gt;", ">");
}

export function blockPlainText(block) {
  if (["paragraph", "heading", "subheading", "quote", "code"].includes(block.type)) return stripHtml(block.html);
  if (["bullet_list", "numbered_list"].includes(block.type)) return (block.items || []).map(stripHtml).join("\n");
  if (block.type === "embed") return block.url || "";
  return block.caption || "";
}

export function draftPlainText(draft) {
  return draftBlocks(draft).map(blockPlainText).filter(Boolean).join("\n\n");
}

export function draftBodyHtml(draft) {
  return draftBlocks(draft).map((block) => {
    if (block.type === "paragraph") return `<p>${inlineHtml(block.html) || "<br>"}</p>`;
    if (block.type === "heading") return `<h2>${inlineHtml(block.html)}</h2>`;
    if (block.type === "subheading") return `<h3>${inlineHtml(block.html)}</h3>`;
    if (block.type === "quote") return `<blockquote><p>${inlineHtml(block.html)}</p></blockquote>`;
    if (block.type === "code") return `<pre><code>${htmlEscape(stripHtml(block.html))}</code></pre>`;
    if (["bullet_list", "numbered_list"].includes(block.type)) {
      const tag = block.type === "bullet_list" ? "ul" : "ol";
      return `<${tag}>${(block.items || []).map((item) => `<li>${inlineHtml(item)}</li>`).join("")}</${tag}>`;
    }
    if (block.type === "divider") return "<hr>";
    if (block.type === "image" && block.url) {
      const caption = block.caption ? `<figcaption>${htmlEscape(block.caption)}</figcaption>` : "";
      return `<figure data-layout="${htmlEscape(block.layout || "regular")}"><img src="${htmlEscape(block.url)}" alt="${htmlEscape(block.alt || "")}">${caption}</figure>`;
    }
    if (block.type === "embed" && block.url) {
      return `<p><a href="${htmlEscape(block.url)}">${htmlEscape(block.caption || block.url)}</a></p>`;
    }
    return "";
  }).join("");
}

export function imageCount(draft) {
  return draftBlocks(draft).filter((block) => block.type === "image" && block.url).length;
}

export function draftStructure(draft) {
  const blocks = draftBlocks(draft);
  const bodyHtml = draftBodyHtml(draft);
  const countTags = (pattern) => (bodyHtml.match(pattern) || []).length;
  return {
    images: blocks.filter((block) => block.type === "image" && block.url).length,
    links: countTags(/<a\s[^>]*href=/gi),
    headings: blocks.filter((block) => block.type === "heading" || block.type === "subheading").length,
    quotes: blocks.filter((block) => block.type === "quote").length,
    list_items: blocks
      .filter((block) => block.type === "bullet_list" || block.type === "numbered_list")
      .reduce((total, block) => total + (block.items || []).length, 0),
    bold: countTags(/<(strong|b)(\s|>)/gi),
    italic: countTags(/<(em|i)(\s|>)/gi),
  };
}
