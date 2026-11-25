import { useTranslation } from "react-i18next";
import { useEffect, useState } from "react";
import { GitBranch, Loader2, CheckCircle2, XCircle, PauseCircle } from "lucide-react";
import { useAppStore } from "../store/appStore";
import { useTaskStore } from "../store/taskStore";
import { useApi } from "../hooks/useApi";

export function StatusBar() {
  const { t } = useTranslation();
  const { api } = useApi();
  const backendDead = useAppStore((s) => s.backendDead);
  const backendReady = useAppStore((s) => s.backendReady);
  const tasks = useTaskStore((s) => s.tasks);

  const [gitBranch, setGitBranch] = useState<string | null>(null);
  const editorContext = useAppStore((s) => s.editorContext);

  useEffect(() => {
    if (!backendReady) return;
    // 2026-09-15 修复：后端实际路由是 /api/v1/git/branches（返回 {branches:[{name,current}]}），
    // 原来的 /workspace/git/branch 不存在 → 状态栏分支永远为空
    api<{ branches?: Array<{ name: string; current: boolean }> }>("/api/v1/git/branches")
      .then((r) => setGitBranch(r.branches?.find((b) => b.current)?.name || null))
      .catch(() => setGitBranch(null));
  }, [backendReady, api]);

  const runningCount = Object.values(tasks).filter((t) => t.status === "running").length;
  const failedCount = Object.values(tasks).filter((t) => t.status === "failed").length;
  const pausedCount = Object.values(tasks).filter((t) => t.status === "paused").length;

  return (
    <footer className="h-5 bg-secondary/40 flex items-center px-2 gap-4 text-[10px] select-none shrink-0 z-10">
      <div className="flex items-center gap-1.5 text-muted-foreground">
        {backendDead ? (
          <>
            <XCircle size={9} className="text-destructive" />
            <span className="text-destructive">{t("statusBar.backendDisconnected")}</span>
          </>
        ) : !backendReady ? (
          <>
            <Loader2 size={9} className="animate-spin" />
            <span>{t("statusBar.loading")}</span>
          </>
        ) : (
          <>
            <CheckCircle2 size={9} className="text-success" />
            <span>{t("statusBar.ready")}</span>
          </>
        )}
      </div>

      {gitBranch && (
        <div className="flex items-center gap-1.5 text-muted-foreground" title={t("statusBar.gitBranch")}>
          <GitBranch size={9} />
          <span className="truncate max-w-[120px]">{gitBranch}</span>
        </div>
      )}

      {/* 光标位置与编辑器信息——对标 VS Code 状态栏（行:列 / 语言 / 缩进） */}
      {editorContext?.activeFile && editorContext.cursorLine > 0 && (
        <div className="flex items-center gap-2 text-muted-foreground">
          <span>
            {t("statusBar.lineCol", {
              line: editorContext.cursorLine,
              col: editorContext.cursorColumn || 1,
            })}
          </span>
          {editorContext.language && <span>{editorContext.language}</span>}
          {editorContext.indentSize ? (
            <span>{t("statusBar.indent", { size: editorContext.indentSize })}</span>
          ) : null}
        </div>
      )}

      <div className="flex-1" />

      <div className="flex items-center gap-2">
        {runningCount > 0 && (
          <span className="flex items-center gap-1 text-primary">
            <Loader2 size={9} className="animate-spin" />
            {runningCount}
          </span>
        )}
        {failedCount > 0 && (
          <span className="flex items-center gap-1 text-destructive">
            <XCircle size={9} />
            {failedCount}
          </span>
        )}
        {pausedCount > 0 && (
          <span className="flex items-center gap-1 text-warning">
            <PauseCircle size={9} />
            {pausedCount}
          </span>
        )}
      </div>
    </footer>
  );
}
