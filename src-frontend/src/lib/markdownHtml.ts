import DOMPurify from "dompurify";
import { marked } from "marked";

/**
 * Markdown → 安全 HTML 的**唯一实现**（2026-09-15）。
 *
 * 此前 TaskDetail 与 DeliberationView 各写了一份 `renderMd`（marked + DOMPurify），
 * 工作区预览再加一份就是第四份——同一份知识写四处，改一处漏三处。
 * ResultRenderer 的管线更复杂（实体高亮 + LRU 缓存），暂未并入；它内部同样走 marked + DOMPurify。
 *
 * 安全边界：解析后一律过 DOMPurify，`target` / `data-code` 属性按需放行；
 * 任何调用方都不要再自己拼 innerHTML。
 */
export function renderMarkdownHtml(text: string): string {
  // marked.parse 返回类型随配置可能为 Promise——运行时守卫避免静默产出 "[object Promise]"
  const parsed = marked.parse(text ?? "");
  const html = typeof parsed === "string" ? parsed : "";
  return `<div class="markdown-body">${DOMPurify.sanitize(html, {
    ADD_ATTR: ["target", "data-code"],
  })}</div>`;
}
