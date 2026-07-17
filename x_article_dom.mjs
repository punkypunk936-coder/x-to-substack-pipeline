export function articleExtractionSource() {
  return String.raw`
(() => {
  const clean = (value) => String(value || "")
    .replace(/\r\n/g, "\n")
    .replace(/\r/g, "\n")
    .replace(/[ \t]+\n/g, "\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
  const escapeHtml = (value) => String(value || "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
  const safeUrl = (value) => {
    try {
      const parsed = new URL(String(value || ""), location.href);
      return ["http:", "https:", "mailto:"].includes(parsed.protocol) ? parsed.href : "";
    } catch {
      return "";
    }
  };
  const linkUrl = (element) => safeUrl(
    element.getAttribute("href")
      || element.getAttribute("data-expanded-url")
      || element.getAttribute("data-url")
      || element.getAttribute("data-href")
      || "",
  );
  const mediaUrl = (value) => {
    const url = safeUrl(value);
    if (!url) return "";
    try {
      const parsed = new URL(url);
      if (/pbs\.twimg\.com$/i.test(parsed.hostname) && /\/media\//i.test(parsed.pathname)) {
        parsed.searchParams.set("name", "large");
      }
      return parsed.href;
    } catch {
      return url;
    }
  };
  const isArticleMediaUrl = (value) => {
    try {
      const parsed = new URL(String(value || ""), location.href);
      return ["http:", "https:"].includes(parsed.protocol)
        && (/\.twimg\.com$/i.test(parsed.hostname) || /\.twitter\.com$/i.test(parsed.hostname))
        && !/(?:profile_images|profile_banners|emoji|hashflags)/i.test(parsed.pathname);
    } catch {
      return false;
    }
  };
  const inlineHtml = (root) => {
    const render = (node) => {
      if (node.nodeType === Node.TEXT_NODE) return escapeHtml(node.nodeValue || "");
      if (node.nodeType !== Node.ELEMENT_NODE) return "";
      const element = node;
      if (element.tagName === "BR") return "<br>";
      let content = Array.from(element.childNodes).map(render).join("");
      if (!content) return "";
      const style = element.style || {};
      const weight = String(style.fontWeight || "").toLowerCase();
      const decoration = String(style.textDecoration || style.textDecorationLine || "").toLowerCase();
      if (["STRONG", "B"].includes(element.tagName) || weight === "bold" || /^[6-9]00$/.test(weight)) {
        content = "<strong>" + content + "</strong>";
      }
      if (["EM", "I"].includes(element.tagName) || String(style.fontStyle || "").toLowerCase() === "italic") {
        content = "<em>" + content + "</em>";
      }
      if (element.tagName === "U" || decoration.includes("underline")) content = "<u>" + content + "</u>";
      if (["S", "STRIKE"].includes(element.tagName) || decoration.includes("line-through")) content = "<s>" + content + "</s>";
      if (element.tagName === "CODE") content = "<code>" + content + "</code>";
      const href = element.tagName === "A" || element.getAttribute("role") === "link"
        ? linkUrl(element) || safeUrl(element.href || "")
        : "";
      if (href) content = '<a href="' + escapeHtml(href) + '">' + content + "</a>";
      return content;
    };
    return render(root).trim();
  };
  const imageRecord = (image, layout = "regular") => {
    const url = mediaUrl(image.currentSrc || image.src);
    if (!url || /\/(profile_images|profile_banners|emoji)\//i.test(url)) return null;
    if (!isArticleMediaUrl(url)) return null;
    return {
      type: "image",
      url,
      alt: clean(image.alt || "Image"),
      caption: "",
      layout,
      width: String(image.naturalWidth || ""),
      height: String(image.naturalHeight || ""),
    };
  };
  const imageRecordFromUrl = (value, layout = "regular") => {
    const url = mediaUrl(value);
    if (!url || !isArticleMediaUrl(url)) return null;
    return { type: "image", url, alt: "Image", caption: "", layout, width: "", height: "" };
  };
  const uniqueImages = (images) => images.filter(Boolean).filter(
    (item, index, items) => items.findIndex((other) => other.url === item.url) === index,
  );
  const imagesIn = (node, layout = "regular") => {
    if (!node) return [];
    const records = Array.from(node.querySelectorAll("img[src], img[srcset], source[srcset]"), (image) => {
      const direct = imageRecord(image, layout);
      if (direct) return direct;
      const candidates = String(image.getAttribute("srcset") || image.getAttribute("data-srcset") || "")
        .split(",")
        .map((candidate) => candidate.trim().split(/\s+/)[0])
        .filter(Boolean);
      return candidates
        .map((candidate) => imageRecordFromUrl(candidate, layout))
        .find(Boolean) || null;
    });
    for (const element of [node, ...node.querySelectorAll("*")]) {
      const background = getComputedStyle(element).backgroundImage || "";
      const matches = [...background.matchAll(/url\(["']?([^"')]+)["']?\)/gi)];
      for (const match of matches) {
        const url = mediaUrl(match[1]);
        if (url && isArticleMediaUrl(url)) {
          records.push({ type: "image", url, alt: "Image", caption: "", layout, width: "", height: "" });
        }
      }
    }
    return uniqueImages(records);
  };
  const articleRoot = document.querySelector("[data-testid='twitterArticleReadView']");
  const richRoot = articleRoot?.querySelector("[data-testid='longformRichTextComponent']") || null;
  const contents = richRoot?.querySelector("[data-contents='true']") || null;
  const blocks = [];
  for (const node of Array.from(contents?.children || [])) {
    const list = node.matches("ul,ol") ? node : node.querySelector(":scope > ul, :scope > ol");
    if (list) {
      const items = Array.from(list.querySelectorAll(":scope > li"), (item) => inlineHtml(item)).filter(Boolean);
      if (items.length) blocks.push({ type: list.tagName === "OL" ? "numbered_list" : "bullet_list", items });
      continue;
    }
    const heading = node.matches("h1,h2,h3") ? node : node.querySelector("h1[data-block], h2[data-block], h3[data-block]");
    if (heading) {
      blocks.push({ type: heading.tagName === "H3" ? "subheading" : "heading", html: inlineHtml(heading) });
      continue;
    }
    const quote = node.matches("blockquote") ? node : node.querySelector(":scope > blockquote");
    if (quote) {
      blocks.push({ type: "quote", html: inlineHtml(quote) });
      continue;
    }
    const code = node.matches("pre") ? node : node.querySelector(":scope > pre");
    if (code) {
      blocks.push({ type: "code", html: escapeHtml(clean(code.innerText || code.textContent || "")) });
      continue;
    }
    if (node.matches("hr") || node.querySelector(":scope > hr")) {
      blocks.push({ type: "divider" });
      continue;
    }
    const nodeImages = imagesIn(node);
    const text = clean(node.innerText || node.textContent || "");
    if (nodeImages.length && !text) {
      blocks.push(...nodeImages);
      continue;
    }
    const html = inlineHtml(node);
    if (html) {
      blocks.push({ type: "paragraph", html });
      if (nodeImages.length) blocks.push(...nodeImages);
      continue;
    }
    const embed = Array.from(node.querySelectorAll("a[href]"))
      .map((anchor) => safeUrl(anchor.href))
      .find((href) => href && !/\/media\//i.test(href));
    if (embed) blocks.push({ type: "embed", url: embed, caption: "" });
  }
  const articleLink = Array.from(document.querySelectorAll("a[href]"))
    .find((anchor) => /\/i\/article\/|\/article\//.test(anchor.getAttribute("href") || ""));
  const domArticleImages = uniqueImages(imagesIn(articleRoot));
  const metadataImages = uniqueImages(Array.from(
    document.querySelectorAll("meta[property='og:image'], meta[name='twitter:image'], meta[property='twitter:image']"),
    (meta) => imageRecordFromUrl(meta.getAttribute("content"), "wide"),
  ));
  const allArticleImages = domArticleImages.length ? domArticleImages : metadataImages;
  const richImageUrls = new Set(imagesIn(richRoot).map((item) => item.url));
  const coverMedia = allArticleImages.find((item) => !richImageUrls.has(item.url)) || null;
  if (coverMedia) coverMedia.layout = "wide";
  const fallbackCandidates = Array.from(document.querySelectorAll("article, main, [data-testid='tweetText'], [data-testid='cellInnerDiv']"))
    .map((node) => clean(node.innerText || node.textContent || ""))
    .filter(Boolean)
    .sort((left, right) => right.length - left.length);
  const title = clean(
    articleRoot?.querySelector("[data-testid='twitter-article-title']")?.innerText
    || articleLink?.innerText
    || document.querySelector("h1")?.innerText
    || document.title.replace(/\s*\/\s*X$/, ""),
  );
  return JSON.stringify({
    body: clean(richRoot?.innerText || fallbackCandidates[0] || document.body.innerText || ""),
    blocks,
    media: allArticleImages,
    cover_media: coverMedia,
    title,
    current_url: location.href,
    article_url: articleRoot ? location.href : (articleLink ? new URL(articleLink.getAttribute("href"), location.href).toString() : ""),
    extraction_version: "x_rich_dom_v2",
  });
})()
`;
}
