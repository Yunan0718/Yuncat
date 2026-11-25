import { useEffect, useRef, useState } from "react";
import { ArrowLeft, FileCheck2, FileDiff, Package } from "lucide-react";
import { useTranslation } from "react-i18next";
import { useApi } from "../../hooks/useApi";
import { useAppStore } from "../../store/appStore";
import { useTaskStore } from "../../store/taskStore";
import { UnifiedDiffView } from "../git/UnifiedDiffView";

interface DeliverableRow {
  path: string;
  /** 记录的绝对路径（diff 端点按它定位）；展示/打开用 path（相对） */
  abs_path?: string;
  display?: string;
  rel_path?: string;
  file_path?: string;
  description?: string;
  tool?: string;
  added?: number;
  deleted?: number;
  binary?: boolean;
  oversized?: boolean;
  directory?: boolean;
  missing?: boolean;
  /** 来源：present（工具声明）/ shell（shell 命令改动） */
  source?: string;
}

interface FileDiffRow {
  path: string;
  display: string;
  kind: "text" | "binary" | "oversized" | "unknown";
  diff: string;
  added: number;
  deleted: number;
  truncated?: boolean;
}

/**
 * 会话交付物（对标 DSH `deliverables`）。
 *
 * 三类来源：present 工具声明的最终文件 + 快照口径的改动文件（带增删行数，
 * 对标 DSH workspace/changes）+ 工具调用口径的历史还原（旧字段，兼容）。
 * 点文件名走 appStore.pendingWorkspaceFile——应用会跳到工作区并打开该文件。
 */
export function DeliverablesPanel() {
  const { t } = useTranslation();
  const { api } = useApi();
  const currentSessionId = useTaskStore((s) => s.currentSessionId);
  const version = useTaskStore((s) => s.deliverablesVersion);
  const [rows, setRows] = useState<DeliverableRow[]>([]);
  const [open, setOpen] = useState(false);
  const [diff, setDiff] = useState<FileDiffRow | null>(null);
  const [diffError, setDiffError] = useState("");
  const [loadError, setLoadError] = useState("");
  const [loadingDiff, setLoadingDiff] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    let cancelled = false;
    setLoadError("");
    if (!currentSessionId) {
      setRows([]);
      return;
    }
    api<{
      presented: DeliverableRow[];
      changed: DeliverableRow[];
      changes?: DeliverableRow[];
      shell?: DeliverableRow[];
    }>(
      `/api/v1/sessions/${currentSessionId}/deliverables`,
    )
      .then((r) => {
        if (cancelled) return;
        const presented = Array.isArray(r?.presented) ? r.presented : [];
        const changes = Array.isArray(r?.changes) ? r.changes : [];
        const changed = Array.isArray(r?.changed) ? r.changed : [];
        // shell 改动：命令改的文件，工具层看不见——单列出来，别混进工具层口径
        const shell = Array.isArray(r?.shell) ? r.shell : [];
        const byPath = new Map<string, DeliverableRow>();
        for (const item of [...presented, ...changes, ...changed, ...shell]) {
          const path = item.rel_path || item.file_path || item.path;
          if (!path) continue;
          const existing = byPath.get(path);
          // 后到的来源补字段（present 的描述 + 快照的行数可以并存）
          byPath.set(path, {
            ...existing,
            ...item,
            path,
            abs_path: existing?.abs_path || item.file_path || item.path,
            description: existing?.description ?? item.description,
          });
        }
        setRows(Array.from(byPath.values()));
      })
      .catch((error) => {
        if (cancelled) return;
        setRows([]);
        setLoadError(error instanceof Error ? error.message : String(error));
      });
    return () => { cancelled = true; };
  }, [api, currentSessionId, version, open]);

  // 文件改动没有 WS 事件（present 有）——面板打开期间轻量轮询，避免数据停在打开那一刻
  useEffect(() => {
    if (!open || rows.length === 0) return;
    const timer = setInterval(() => useTaskStore.getState().bumpDeliverables(), 3000);
    return () => clearInterval(timer);
  }, [open, rows.length]);


  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: PointerEvent) => {
      if (rootRef.current && !rootRef.current.contains(event.target as Node)) setOpen(false);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      setOpen(false);
      setDiff(null);
      triggerRef.current?.focus();
    };
    document.addEventListener("pointerdown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);
  if (rows.length === 0 && !loadError) return null;
  const openFile = (path: string) => useAppStore.getState().setPendingWorkspaceFile(path);

  const openDiff = async (row: DeliverableRow) => {
    setDiffError("");
    setLoadingDiff(true);
    try {
      const r = await api<FileDiffRow>(
        `/api/v1/sessions/${currentSessionId}/deliverables/diff?path=${encodeURIComponent(row.abs_path || row.path)}`,
      );
      setDiff(r);
    } catch (error) {
      const detail = error instanceof Error ? error.message : String(error);
      setDiffError(t("deliverables.diffUnavailable") + (detail ? `: ${detail}` : ""));
    } finally {
      setLoadingDiff(false);
    }
  };
  return (
    <div ref={rootRef} className="relative shrink-0">
      <button
        ref={triggerRef}
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-controls={open ? "deliverables-popover" : undefined}
        aria-label={t("deliverables.title")}
        title={t("deliverables.title")}
        className="h-7 inline-flex items-center gap-1 rounded-lg px-1.5 text-[11px] text-muted-foreground hover:text-foreground hover:bg-secondary transition-colors"
      >
        <Package size={15} strokeWidth={1.7} />
        <span className="tabular-nums">{rows.length}</span>
      </button>
      {open && (
        <div
          id="deliverables-popover"
          data-testid="deliverables-panel"
          role="dialog"
          aria-label={t("deliverables.title")}
          className={`absolute top-full left-0 mt-1 z-dropdown max-w-[calc(100vw-2rem)] rounded-lg border border-border bg-card shadow-lg p-2 ${diff ? "w-[min(440px,calc(100vw-2rem))]" : "w-[min(18rem,calc(100vw-2rem))]"}`}
        >
          {diff ? (
            <>
              <div className="flex items-center gap-1 px-1 pb-1.5">
                <button
                  type="button"
                  onClick={() => setDiff(null)}
                  aria-label={t("deliverables.back")}
                  title={t("deliverables.back")}
                  className="shrink-0 rounded p-0.5 text-muted-foreground hover:text-foreground"
                >
                  <ArrowLeft size={12} />
                </button>
                <span className="min-w-0 flex-1 truncate text-[10px] font-semibold text-foreground">
                  {diff.display}
                </span>
                <span className="shrink-0 tabular-nums text-[10px]">
                  <span className="text-success">+{diff.added}</span>
                  <span className="text-destructive"> -{diff.deleted}</span>
                </span>
              </div>
              <div className="max-h-80 overflow-auto rounded border border-border" aria-busy={loadingDiff}>
                {diff.kind === "text" ? (
                  <UnifiedDiffView diffText={diff.diff} />
                ) : (
                  <div className="p-2 text-[10px] text-muted-foreground">
                    {t("deliverables.diffUnavailable")} ({diff.kind})
                  </div>
                )}
              </div>
              {diff.truncated && (
                <div className="px-1 pt-1 text-[9px] text-muted-foreground">
                  {t("deliverables.diffTruncated")}
                </div>
              )}
            </>
          ) : (
            <>
          <div className="px-1 pb-1.5 text-[10px] font-semibold text-muted-foreground">
            {t("deliverables.title")}
          </div>
          {loadError && (
            <div role="alert" className="px-1 pb-1 text-[10px] text-destructive">{t("deliverables.loadFailed")}: {loadError}</div>
          )}
          {loadingDiff && (
            <div role="status" className="px-1 pb-1 text-[10px] text-muted-foreground">{t("common.loading")}</div>
          )}
          {diffError && (
            <div role="alert" className="px-1 pb-1 text-[10px] text-destructive">{diffError}</div>
          )}
          <div className="max-h-64 overflow-y-auto space-y-0.5">
            {rows.map((row) => (
              <div
                key={row.path}
                className="flex items-start gap-1 w-full rounded px-1 py-0.5 hover:bg-secondary transition-colors"
              >
                <button
                  type="button"
                  onClick={() => openFile(row.path)}
                  className="flex items-start gap-1.5 min-w-0 flex-1 text-left text-[11px]"
                >
                  <FileCheck2 size={12} className="mt-0.5 shrink-0 text-primary" />
                  <span className="min-w-0 flex-1">
                  <span className="block truncate text-foreground">
                    {row.display || row.path}
                    {typeof row.added === "number" && (row.added > 0 || (row.deleted ?? 0) > 0) && (
                      <span className="ml-1 tabular-nums text-[10px]">
                        <span className="text-success">+{row.added}</span>
                        {(row.deleted ?? 0) > 0 && (
                          <span className="text-destructive"> -{row.deleted}</span>
                        )}
                      </span>
                    )}
                    {row.binary && (
                      <span className="ml-1 text-[9px] text-muted-foreground">
                        {t("deliverables.binary")}
                      </span>
                    )}
                    {row.oversized && (
                      <span className="ml-1 text-[9px] text-muted-foreground">
                        {t("deliverables.oversized")}
                      </span>
                    )}
                    {row.missing && (
                      <span className="ml-1 text-[9px] text-muted-foreground">
                        {t("deliverables.missing")}
                      </span>
                    )}
                    {row.source === "shell" && (
                      <span
                        className="ml-1 text-[9px] text-muted-foreground"
                        title={t("deliverables.shellHint")}
                      >
                        {t("deliverables.shellTag")}
                      </span>
                    )}
                  </span>
                  {row.description && (
                    <span className="block truncate text-[10px] text-muted-foreground">
                      {row.description}
                    </span>
                  )}
                  </span>
                </button>
                {/* shell 改动的文件没有行数统计，但可以看 diff（旧侧取 git HEAD） */}
                {(typeof row.added === "number" || row.source === "shell") && (
                  <button
                    type="button"
                    onClick={() => void openDiff(row)}
                    disabled={loadingDiff}
                    aria-label={t("deliverables.viewDiff")}
                    title={t("deliverables.viewDiff")}
                    className="shrink-0 mt-0.5 rounded p-0.5 text-muted-foreground hover:text-foreground"
                  >
                    <FileDiff size={12} />
                  </button>
                )}
              </div>
            ))}
          </div>
            </>
          )}
        </div>
      )}
    </div>
  );
}
