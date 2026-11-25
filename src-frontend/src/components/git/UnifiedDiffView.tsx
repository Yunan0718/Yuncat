import { useMemo } from "react";
import { useTranslation } from "react-i18next";

/**
 * Task 2.4: Unified diff 解析 + 渲染——对标 GitHub / GitLab / VS Code SCM。
 * 原 GitDiffView 把 git diff 文本直接当 newContent 传给 Monaco DiffEditor，
 * 显示出来是带 +/- 前缀的纯文本而非真正的 diff 视图。
 *
 * 主流做法：解析 unified diff 格式（git diff 默认输出），按行渲染红绿视图。
 * - `+` added 行：绿色背景
 * - `-` removed 行：红色背景
 * - ` ` context 行：默认背景
 * - `@@ -a,b +c,d @@` hunk header：蓝色背景
 * - `diff --git` / `index` / `---` / `+++` meta 行：灰色
 */

export type DiffLineType = "context" | "added" | "removed" | "hunk" | "meta";

export interface DiffLine {
  type: DiffLineType;
  content: string;
  oldLineNo?: number;
  newLineNo?: number;
}

/**
 * 解析 unified diff 文本为结构化行数组。
 * 纯函数——便于单元测试。对标 git diff 默认输出格式。
 */
export function parseUnifiedDiff(diffText: string): DiffLine[] {
  if (!diffText) return [];
  const lines = diffText.split("\n");
  const result: DiffLine[] = [];
  let oldLine = 0;
  let newLine = 0;

  for (const line of lines) {
    if (line.startsWith("diff --git") || line.startsWith("index ") || line.startsWith("--- ") || line.startsWith("+++ ")) {
      result.push({ type: "meta", content: line });
      continue;
    }
    const hunkMatch = line.match(/^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/);
    if (hunkMatch) {
      oldLine = parseInt(hunkMatch[1]!, 10);
      newLine = parseInt(hunkMatch[2]!, 10);
      result.push({ type: "hunk", content: line });
      continue;
    }
    if (line.startsWith("\\ No newline")) {
      // 文件末尾无换行标记——跳过（不影响行号）
      continue;
    }
    if (line.startsWith("+")) {
      result.push({ type: "added", content: line.slice(1), newLineNo: newLine });
      newLine++;
    } else if (line.startsWith("-")) {
      result.push({ type: "removed", content: line.slice(1), oldLineNo: oldLine });
      oldLine++;
    } else if (line.startsWith(" ")) {
      result.push({ type: "context", content: line.slice(1), oldLineNo: oldLine, newLineNo: newLine });
      oldLine++;
      newLine++;
    }
    // 其它行（纯空行等）忽略——git diff 中所有有效行都有前缀字符
  }
  return result;
}

const LINE_STYLE: Record<DiffLineType, string> = {
  added: "bg-green-500/10 text-green-700 dark:text-green-400",
  removed: "bg-red-500/10 text-red-700 dark:text-red-400",
  context: "",
  hunk: "bg-blue-500/10 text-blue-700 dark:text-blue-400",
  meta: "bg-muted text-muted-foreground",
};

const LINE_PREFIX: Record<DiffLineType, string> = {
  added: "+",
  removed: "-",
  context: " ",
  hunk: "@",
  meta: " ",
};

export interface UnifiedDiffViewProps {
  diffText: string;
  title?: string;
}

export function UnifiedDiffView({ diffText, title }: UnifiedDiffViewProps) {
  const { t } = useTranslation();
  const lines = useMemo(() => parseUnifiedDiff(diffText), [diffText]);

  if (lines.length === 0) {
    return (
      <p className="text-muted-foreground text-center py-4">{t("gitPanel.diff.hint")}</p>
    );
  }

  return (
    <div className="border rounded overflow-hidden bg-background">
      {title && (
        <div className="flex items-center justify-between px-3 py-2 border-b bg-secondary">
          <span className="text-xs font-semibold font-mono">{title}</span>
        </div>
      )}
      <div className="overflow-x-auto font-mono text-xs leading-5">
        {lines.map((line, i) => (
          <div key={i} className={`flex ${LINE_STYLE[line.type]}`}>
            <span className="inline-block w-12 shrink-0 text-right pr-2 text-muted-foreground/60 select-none">
              {line.oldLineNo ?? ""}
            </span>
            <span className="inline-block w-12 shrink-0 text-right pr-2 text-muted-foreground/60 select-none">
              {line.newLineNo ?? ""}
            </span>
            <span className="inline-block w-4 shrink-0 text-center select-none opacity-70">
              {LINE_PREFIX[line.type]}
            </span>
            <span className="whitespace-pre flex-1 pr-3">
              {line.content || "\u00A0"}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}
