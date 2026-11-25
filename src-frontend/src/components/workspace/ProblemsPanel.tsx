import { useTranslation } from "react-i18next";

import { useAppStore } from "../../store/appStore";

/**
 * 问题面板（2026-09-15）——对标 VS Code 的 Problems 视图。
 *
 * 数据来自 appStore.diagnosticsByFile：MonacoEditor 写 marker 的同一份数据
 * （后端 lint 经 WS 推送），这里只是把它列出来并支持点击跳转，不重复取数。
 * 之前诊断只以波浪线的形式出现在编辑器里，用户看不到"整个工作区有哪些问题"。
 */
export function ProblemsPanel({
  onOpen,
}: {
  onOpen: (path: string, line: number, column: number) => void;
}) {
  const { t } = useTranslation();
  const diagnosticsByFile = useAppStore((s) => s.diagnosticsByFile);

  const files = Object.entries(diagnosticsByFile).filter(([, items]) => items && items.length > 0);

  if (files.length === 0) {
    return (
      <div className="flex items-center justify-center h-full text-xs text-muted-foreground">
        {t("workspacePage.noProblems")}
      </div>
    );
  }

  return (
    <div className="h-full overflow-auto text-xs">
      {files.map(([file, items]) => (
        <div key={file}>
          <div className="px-2 py-0.5 font-semibold text-muted-foreground sticky top-0 bg-background/95 backdrop-blur">
            {file} <span className="font-normal">({items.length})</span>
          </div>
          {items.map((d, i) => (
            <button
              key={`${file}-${d.line}-${d.col}-${i}`}
              type="button"
              onClick={() => onOpen(file, d.line, d.col)}
              className="w-full text-left px-2 py-0.5 flex items-start gap-2 hover:bg-secondary"
            >
              <span
                className={
                  d.severity === "error" ? "text-destructive shrink-0" : "text-warning shrink-0"
                }
              >
                {d.severity === "error" ? "✕" : "!"}
              </span>
              <span className="text-muted-foreground shrink-0">
                {t("workspacePage.problemLocation", { line: d.line, col: d.col })}
              </span>
              <span className="truncate">{d.message}</span>
            </button>
          ))}
        </div>
      ))}
    </div>
  );
}
