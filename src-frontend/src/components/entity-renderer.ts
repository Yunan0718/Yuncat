/**
 * EntityRenderer — Claude Code/Cursor 风格实体高亮渲染。
 *
 * 检测文本中的特殊实体并渲染为可点击标签：
 *   - 文件路径: `/path/to/file.py` → 点击跳转工作区
 *   - URL: `https://...` → 点击复制到剪贴板
 *   - Agent 名: `@hermes` `@openclaw` → 显示信息标签
 *   - 工作流: 反引号包裹的非文件词 → 可点击
 *   - Skill 名: `skill:` 前缀 → 可点击
 *
 * 在 ResultRenderer 的 marked 解析之后、DOMPurify 之前注入。
 */

// ---- 正则模式 ----
const ENTITY_PATTERNS: Array<{ regex: RegExp; type: string; icon: string }> = [
  {
    regex: /\b([\w/.-]+\.(?:py|tsx?|jsx?|json|yaml|yml|md|html|css|rs|go|java|sql|txt|toml|ini|cfg|env))\b/g,
    type: "file",
    icon: "file",
  },
  { regex: /(https?:\/\/[^\s<>"{}|\\^`[\]]+)/g, type: "url", icon: "url" },
  { regex: /@(hermes|openclaw)/g, type: "agent", icon: "agent" },
  { regex: /skill:([a-zA-Z][\w-]*)/g, type: "skill", icon: "skill" },
  { regex: /\bwf:([a-zA-Z][\w-]*)\b/g, type: "workflow", icon: "workflow" },
  // Phase 33: 代码符号高亮（类名/函数名/方法名/常量）
  { regex: /\bclass\s+([A-Z][\w]*)\b/g, type: "symbol_class", icon: "symbol" },
  { regex: /\bdef\s+([a-z_][\w]*)\b/g, type: "symbol_func", icon: "symbol" },
  { regex: /\bfn\s+([a-z_][\w]*)\b/g, type: "symbol_func", icon: "symbol" },
  { regex: /\bfunction\s+([a-z_][\w]*)\b/g, type: "symbol_func", icon: "symbol" },
  { regex: /\bconst\s+([A-Z][A-Z_\d]*)\b/g, type: "symbol_const", icon: "symbol" },
  { regex: /\bimport\s+\{([^}]+)\}\s+from\s+['"][^'"]+['"]/g, type: "symbol_import", icon: "symbol" },
];

export interface DetectedEntity {
  type: string;
  value: string;
  icon: string;
  clickAction?: string; // 点击后的行为描述
}

/** 从文本中提取所有可点击实体 */
export function detectEntities(text: string): DetectedEntity[] {
  const results: DetectedEntity[] = [];
  const seen = new Set<string>();

  for (const { regex, type, icon } of ENTITY_PATTERNS) {
    regex.lastIndex = 0;
    let match: RegExpExecArray | null;
    while ((match = regex.exec(text)) !== null) {
      const key = `${type}:${match[0]}`;
      if (seen.has(key)) continue;
      seen.add(key);
      results.push({
        type,
        value: match[0],
        icon,
        clickAction: type === "file" ? "open" : type === "url" ? "copy" : "info",
      });
    }
  }
  return results;
}

/** 给 HTML 文本注入实体高亮 span */
export function injectEntitySpans(
  html: string,
  entities: DetectedEntity[],
  t: (k: string, opts?: Record<string, unknown>) => string,
): string {
  let result = html;
  for (const ent of entities) {
    const escaped = ent.value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    // 只在非标签内容中替换（不在 <tag> 内）
    const tagSafeRegex = new RegExp(`(?<!["'=])${escaped}(?!["'])(?![^<]*>)`, "g");
    const cls = `entity-${ent.type}`;
    result = result.replace(
      tagSafeRegex,
      `<span class="${cls}" data-entity="${ent.value}" data-entity-type="${ent.type}" title="${ent.clickAction === "open" ? t("phase49.entity.openFile") : ent.clickAction === "copy" ? t("phase49.entity.copyLink") : ent.type}">${ent.value}</span>`,
    );
  }
  return result;
}
