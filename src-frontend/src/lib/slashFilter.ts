/**
 * lib/slashFilter.ts —— “/” 命令菜单的模糊匹配与排序
 *
 * 对标 deepseek-harness 的 `rankByName`（见其 2026-08-04 笔记）：只前缀匹配时，
 * 用户只记得关键字母就找不到命令（`/cpt` 找不到 `/compact`）。
 *
 * 匹配分四档，越前越先（同档内保持原来的目录顺序，保证结果稳定）：
 *   1. 命令名完全相等          2. 命令名前缀 / 名称前缀
 *   3. 子序列（模糊）匹配，连续且越靠前越好   4. 描述里出现关键字
 * 也就是说：模糊匹配只能让命令“被看到”，不会把它排到精确匹配前面。
 */
export interface SlashCommandLike {
  slash_command: string;
  name: string;
  description?: string;
}

/** 子序列匹配（字母依次出现即可），间隔多/起点靠后就扣分 */
function subsequenceScore(needle: string, haystack: string): number | null {
  let cursor = 0;
  let firstIndex = -1;
  let lastMatch = -1;
  let gaps = 0;

  for (const ch of needle) {
    const found = haystack.indexOf(ch, cursor);
    if (found === -1) return null;
    if (firstIndex === -1) firstIndex = found;
    if (lastMatch !== -1 && found !== lastMatch + 1) gaps += 1;
    lastMatch = found;
    cursor = found + 1;
  }
  return 100 + gaps * 4 + firstIndex * 2;
}

/** 返回匹配分（越小越靠前）；null 表示不匹配 */
export function scoreSlashCommand(query: string, command: SlashCommandLike): number | null {
  const raw = query.trim().toLowerCase();
  if (raw.startsWith("/")) return scoreSlashCommand(raw.slice(1), command);

  const slash = String(command.slash_command || "").toLowerCase().replace(/^\//, "");
  const name = String(command.name || "").toLowerCase();
  const description = String(command.description || "").toLowerCase();
  if (!raw) return 0;

  if (slash === raw) return 0;
  if (slash.startsWith(raw)) return 10 + (slash.length - raw.length) * 0.1;
  if (name.startsWith(raw)) return 20 + (name.length - raw.length) * 0.1;

  const fuzzy = subsequenceScore(raw, slash);
  if (fuzzy !== null) return fuzzy;
  const nameFuzzy = subsequenceScore(raw, name);
  if (nameFuzzy !== null) return nameFuzzy + 5;

  if (description.includes(raw)) return 300 + description.indexOf(raw);
  return null;
}

/** 按相关度排序；空查询保持原目录顺序 */
export function rankSlashCommands<T extends SlashCommandLike>(
  commands: readonly T[],
  query: string,
): T[] {
  if (!query.trim().replace(/^\//, "")) return [...commands];

  const scored: Array<{ item: T; score: number; index: number }> = [];
  commands.forEach((item, index) => {
    const score = scoreSlashCommand(query, item);
    if (score !== null) scored.push({ item, score, index });
  });
  scored.sort((a, b) => a.score - b.score || a.index - b.index);
  return scored.map((entry) => entry.item);
}
