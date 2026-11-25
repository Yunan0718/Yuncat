import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { InlineDiff } from "./workspace/InlineDiff";
import { Button } from "./ui/button";
import { FocusTrap } from "./FocusTrap";

/* ── Tool name display ── */
function toolLabel(name: string, tFn: (key: string, opts?: Record<string, unknown>) => string): string {
  const key = `tools.${name}`;
  const translated = tFn(key, { defaultValue: name });
  return typeof translated === "string" && translated !== key ? translated : name;
}

/** 确认弹窗预览数据 — 不同工具类型对应不同字段 */
interface WriteFilePreview {
  path: string;
  /** H2: 工作区根路径——供前端确认弹窗可视化安全边界 */
  workspace_root?: string;
  unified_diff?: string;
  diff_stats?: { summary?: string; total_old?: number; total_new?: number };
  total_lines?: number;
  total_size?: number;
  old_lines?: number;
  old_content?: string;
  new_content?: string;
}

/** E1: edit_file 预览数据——SEARCH/REPLACE 精确编辑 */
interface EditFilePreview {
  path: string;
  /** H2: 工作区根路径——供前端确认弹窗可视化安全边界 */
  workspace_root?: string;
  unified_diff?: string;
  matches?: number;
  total_edits?: number;
  old_size?: number;
  new_size?: number;
  diff_summary?: string;
  failed?: string[] | null;
  old_content?: string;
  new_content?: string;
}

interface DeleteFilePreview {
  path: string;
  /** H2: 工作区根路径——供前端确认弹窗可视化安全边界 */
  workspace_root?: string;
  size?: number;
  preview?: string;
}

interface RunCommandPreview {
  command: string;
  timeout_sec?: number;
  cwd?: string;
  /** H1: 风险等级（LOW/MEDIUM/HIGH）——HIGH 在 tool_service 上游拦截，正常不会到这里 */
  risk_level?: string;
  /** H1: 风险原因（如"含中风险关键词: sudo"） */
  risk_reason?: string;
}

interface PreviewProps {
  toolName: string;
  preview: Record<string, unknown>;
  lang: string;
}

/** E1: write_file / edit_file 共用的 diff 预览——unified_diff 文本块 + Monaco DiffEditor */
function DiffPreview({
  path, unifiedDiff, oldContent, newContent, summary,
}: {
  path: string;
  unifiedDiff: string;
  oldContent: string;
  newContent: string;
  summary?: string;
}) {
  if (unifiedDiff) {
    return (
      <div className="w-full">
        {summary && (
          <div className="text-[10px] text-muted-foreground mb-2 font-mono">{summary}</div>
        )}
        <pre className="diff-block rounded-lg p-3 text-xs font-mono max-h-96 overflow-y-auto leading-relaxed whitespace-pre-wrap break-all">
          {unifiedDiff.split("\n").map((line, i) => {
            let cls = "";
            if (line.startsWith("+") || line.startsWith("-") || line.startsWith("@@")) {
              cls = "text-[var(--diff-hunk)]";
            } else if (line.startsWith("---") || line.startsWith("+++")) {
              cls = "text-[var(--diff-meta)] font-semibold";
            } else if (line.startsWith("...")) {
              cls = "text-[var(--diff-meta)] italic";
            }
            return (
              // eslint-disable-next-line react/no-array-index-key
              <div key={i + ":" + line.slice(0, 10)} className={cls}>
                {line || " "}
              </div>
            );
          })}
        </pre>
        <InlineDiff oldContent={oldContent} newContent={newContent} title={path} />
      </div>
    );
  }
  return path ? (
    <InlineDiff oldContent={oldContent} newContent={newContent} title={path} />
  ) : null;
}

/** H2: 工作区安全边界条——显示文件操作的工作区根路径 + 相对路径
 *  对标 Claude Code 文件审批弹窗：显式展示安全边界，让用户确认操作范围
 */
function WorkspaceBoundaryBar({
  workspaceRoot, relativePath, t,
}: {
  workspaceRoot?: string;
  relativePath: string;
  t: (key: string, opts?: Record<string, unknown>) => string;
}) {
  if (!workspaceRoot) return null;
  return (
    <div className="bg-muted/40 border border-border/60 rounded-lg px-3 py-2 text-xs mb-3 space-y-0.5">
      <div className="flex items-center gap-1.5 font-mono">
        <span className="shrink-0" aria-hidden>📁</span>
        <span className="text-muted-foreground text-[10px] uppercase tracking-wide shrink-0">
          {t("tool.workspaceRoot")}:
        </span>
        <span className="text-foreground/80 truncate" title={workspaceRoot}>{workspaceRoot}</span>
      </div>
      {relativePath && (
        <div className="flex items-center gap-1.5 font-mono pl-5">
          <span className="text-muted-foreground text-[10px] uppercase tracking-wide shrink-0">
            {t("tool.relativePath")}:
          </span>
          <span className="text-foreground truncate">{relativePath}</span>
        </div>
      )}
    </div>
  );
}

/** Phase 107: 工具确认弹窗预览内容 — 按工具类型渲染不同预览（从旧文件保留） */
export function ToolConfirmPreview({ toolName, preview }: PreviewProps) {
  const { t } = useTranslation();

  if (toolName === "write_file") {
    const p = preview as unknown as WriteFilePreview;
    const summary = p.diff_stats?.summary
      || t("toolConfirm.linesSummary", { old: p.old_lines || 0, total: p.total_lines || 0, size: p.total_size || 0 });
    return (
      <div>
        <WorkspaceBoundaryBar workspaceRoot={p.workspace_root} relativePath={String(p.path ?? "")} t={t} />
        <DiffPreview
          path={String(p.path ?? "")}
          unifiedDiff={p.unified_diff || ""}
          oldContent={typeof p.old_content === "string" ? p.old_content : ""}
          newContent={typeof p.new_content === "string" ? p.new_content : ""}
          summary={summary}
        />
      </div>
    );
  }

  if (toolName === "edit_file") {
    // E1: SEARCH/REPLACE 精确编辑预览——复用 write_file 的 diff 渲染（对标 Cursor inline diff）
    const p = preview as unknown as EditFilePreview;
    const summary = p.diff_summary
      || t("toolConfirm.editsSummary", { matches: p.matches || 0, total: p.total_edits || 0, old: p.old_size || 0, new: p.new_size || 0 });
    return (
      <div>
        <WorkspaceBoundaryBar workspaceRoot={p.workspace_root} relativePath={String(p.path ?? "")} t={t} />
        <DiffPreview
          path={String(p.path ?? "")}
          unifiedDiff={p.unified_diff || ""}
          oldContent={typeof p.old_content === "string" ? p.old_content : ""}
          newContent={typeof p.new_content === "string" ? p.new_content : ""}
          summary={summary}
        />
      </div>
    );
  }

  if (toolName === "delete_file") {
    const p = preview as unknown as DeleteFilePreview;
    return (
      <div>
        <WorkspaceBoundaryBar workspaceRoot={p.workspace_root} relativePath={String(p.path ?? "")} t={t} />
        <div className="bg-destructive/10 border border-destructive/20 rounded-lg p-4">
          <div className="text-sm font-semibold text-destructive mb-2">
            {t("tool.deleteFile")}: {p.path}
          </div>
          <div className="text-xs text-muted-foreground mb-2">
            {t("tool.size")}: {(p.size || 0).toLocaleString()} bytes
          </div>
          {p.preview && (
            <pre className="bg-secondary rounded p-2 text-[11px] font-mono max-h-32 overflow-y-auto whitespace-pre-wrap">
              {p.preview}
            </pre>
          )}
        </div>
      </div>
    );
  }

  if (toolName === "run_command") {
    return <RunCommandPreviewView preview={preview as unknown as RunCommandPreview} t={t} />;
  }

  return null;
}

/** H1: run_command 预览——风险等级 + 工作目录 + 超时 + 命令 + 执行警告
 *  对标 Claude Code 命令审批弹窗：显式展示 cwd / timeout / 风险原因
 *  HIGH 风险在 tool_service._validate_and_prepare 已拦截，此处只会是 MEDIUM/LOW
 *  但仍防御性处理 HIGH（红色 destructive 警告）
 */
function RunCommandPreviewView({
  preview: p,
  t,
}: {
  preview: RunCommandPreview;
  t: (key: string, opts?: Record<string, unknown>) => string;
}) {
  const risk = (p.risk_level || "LOW").toUpperCase();
  const isHigh = risk === "HIGH";
  const isMed = risk === "MEDIUM";

  // 风险徽章配色——HIGH=destructive 红, MEDIUM=warning 黄, LOW=muted 灰
  const badgeCls = isHigh
    ? "bg-destructive/15 text-destructive border-destructive/30"
    : isMed
      ? "bg-warning/15 text-warning border-warning/30"
      : "bg-muted text-muted-foreground border-border";
  const riskLabel = isHigh
    ? t("tool.riskHigh")
    : isMed
      ? t("tool.riskMedium")
      : t("tool.riskLow");

  // 警告横幅——只对 MEDIUM/HIGH 显示
  const showWarningBar = isHigh || isMed;
  const bannerCls = isHigh
    ? "bg-destructive/10 border-destructive/30 text-destructive"
    : "bg-warning/10 border-warning/30 text-warning";

  return (
    <div className="space-y-3">
      {/* 风险横幅 */}
      {showWarningBar && p.risk_reason && (
        <div className={`border rounded-lg px-3 py-2 text-xs flex items-start gap-2 ${bannerCls}`}>
          <span className="font-mono font-semibold shrink-0">⚠ {riskLabel}</span>
          <span className="break-words">{p.risk_reason}</span>
        </div>
      )}

      {/* 元信息：工作目录 + 超时 */}
      <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
        {p.cwd && (
          <div className="font-mono">
            <span className="opacity-70">{t("tool.workingDir")}: </span>
            <span className="text-foreground/80">{p.cwd}</span>
          </div>
        )}
        {typeof p.timeout_sec === "number" && (
          <div className="font-mono">
            <span className="opacity-70">{t("tool.timeout")}: </span>
            <span className="text-foreground/80">{p.timeout_sec}{t("tool.seconds")}</span>
          </div>
        )}
      </div>

      {/* 命令展示 + 风险徽章 */}
      <div className="relative">
        <pre
          className={`bg-secondary text-success rounded-lg p-4 pr-20 text-sm font-mono overflow-x-auto whitespace-pre-wrap break-all ${
            isHigh ? "border border-destructive/30" : ""
          }`}
        >
          <span className="text-muted-foreground">$ </span>
          {String(p.command ?? "?")}
        </pre>
        <span
          className={`absolute top-2 right-2 px-2 py-0.5 rounded text-[10px] font-semibold border ${badgeCls}`}
        >
          {riskLabel}
        </span>
      </div>

      {/* 执行警告 */}
      <div className="text-xs text-muted-foreground italic">
        ⚡ {t("tool.execWarning")}
      </div>
    </div>
  );
}

interface ToolConfirmDialogProps {
  open: boolean;
  toolName: string;
  preview: Record<string, unknown>;
  /** Batch 4b: 来源任务摘要（后端截 80 字符）——头部展示，供高危决策提供上下文 */
  taskPrompt?: string;
  /** Batch 4b: 确认 token 过期时间（epoch 秒）——0/未定义表示未知，不显示倒计时 */
  expiresAt?: number;
  onConfirm: (confirmed: boolean, remember?: boolean) => void | Promise<void>;
  /** Token 已过期时只关闭本地弹窗，不再向后端提交必然失败的拒绝请求。 */
  onExpire?: () => void;
}

/**
 * Phase 107: 工具确认弹窗 — 从 ChatView 拆出，复用 ToolConfirmPreview。
 *
 * 支持 write_file / edit_file / delete_file / run_command 等 confirmable 工具的预览与确认。
 * E1: write_file / edit_file 显示 Monaco DiffEditor，弹窗加宽到 max-w-4xl。
 * 修复 1.6 (F-031): 加 "Always allow in this session" 复选框——对标 Cursor /
 * Claude Code per-tool session 记住。
 * Batch 4b: 头部显示来源任务摘要 + 剩余有效期倒计时（mm:ss，1s interval 仅弹窗打开期间运行）；
 * 倒计时归零自动按"取消"关闭，避免用户对已过期 token 点确认收到 404。
 */
export function ToolConfirmDialog({
  open, toolName, preview, taskPrompt, expiresAt, onConfirm, onExpire,
}: ToolConfirmDialogProps) {
  const { t, i18n } = useTranslation();
  const dialogRef = useRef<HTMLDivElement>(null);
  const rejectRef = useRef<HTMLButtonElement>(null);
  const expiryHandledRef = useRef(false);
  const submittingRef = useRef(false);
  const decideRef = useRef<(confirmed: boolean, remember?: boolean) => void>(() => {});
  const [remember, setRemember] = useState(false);
  const [remaining, setRemaining] = useState<number | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  async function decide(confirmed: boolean, rememberChoice = remember) {
    if (submittingRef.current) return;
    submittingRef.current = true;
    setSubmitting(true);
    setActionError(null);
    try {
      await onConfirm(confirmed, rememberChoice);
    } catch (error) {
      const detail = error instanceof Error ? error.message : String(error);
      setActionError(t("toolConfirm.failed", { error: detail }));
    } finally {
      submittingRef.current = false;
      setSubmitting(false);
    }
  }
  decideRef.current = decide;

  useEffect(() => {
    if (!open) {
      setRemember(false);
      setActionError(null);
      setSubmitting(false);
      expiryHandledRef.current = false;
      return;
    }
    if (!expiresAt) {
      setRemaining(null);
      return;
    }
    const calc = () => Math.max(0, Math.round(expiresAt - Date.now() / 1000));
    setRemaining(calc());
    const timer = setInterval(() => setRemaining(calc()), 1000);
    return () => clearInterval(timer);
  }, [open, expiresAt]);

  useEffect(() => {
    if (!open || remaining !== 0 || expiryHandledRef.current) return;
    expiryHandledRef.current = true;
    if (onExpire) {
      onExpire();
      return;
    }
    void Promise.resolve(onConfirm(false)).catch((error) => {
      const detail = error instanceof Error ? error.message : String(error);
      setActionError(t("toolConfirm.failed", { error: detail }));
    });
  }, [open, remaining, onExpire, onConfirm, t]);

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || submittingRef.current) return;
      event.preventDefault();
      decideRef.current(false);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open]);

  if (!open) return null;
  const isDiffTool = toolName === "write_file" || toolName === "edit_file";
  const maxWidthClass = isDiffTool ? "max-w-4xl" : "max-w-2xl";
  const expired = remaining === 0;
  const disabled = submitting || expired;

  return (
    <div
      className="fixed inset-0 bg-black/40 flex items-center justify-center p-4 z-modal animate-modal-overlay"
      onClick={(event) => { if (event.target === event.currentTarget) return; }}
      role="dialog"
      aria-modal="true"
      aria-labelledby="tool-confirm-title"
      aria-busy={submitting}
    >
      <FocusTrap active={open} initialFocusRef={rejectRef}>
        <div
          ref={dialogRef}
          tabIndex={-1}
          className={`bg-card rounded-xl shadow-lg ${maxWidthClass} w-full max-h-[80vh] overflow-y-auto outline-none animate-modal-content`}
          onClick={(event) => event.stopPropagation()}
        >
          <div className="px-5 py-3 border-b flex items-center gap-2">
            <span className="w-2 h-2 rounded-full bg-warning" aria-hidden="true" />
            <h3 id="tool-confirm-title" className="font-medium text-sm">{toolLabel(toolName, t)}</h3>
            {remaining !== null && (
              <span
                className={`ml-auto text-xs font-mono tabular-nums ${remaining <= 60 ? "text-destructive font-semibold" : "text-muted-foreground"}`}
                data-testid="confirm-remaining"
                data-remaining={remaining}
                aria-live="off"
              >
                {t("toolConfirm.remaining", {
                  time: `${String(Math.floor(remaining / 60)).padStart(2, "0")}:${String(remaining % 60).padStart(2, "0")}`,
                })}
              </span>
            )}
          </div>
          {taskPrompt ? (
            <div
              className="px-5 py-2 bg-muted/40 border-b text-xs text-muted-foreground truncate"
              title={taskPrompt}
              data-testid="confirm-source-task"
            >
              {t("toolConfirm.sourceTask", { prompt: taskPrompt })}
            </div>
          ) : null}
          <div className="p-4">
            <ToolConfirmPreview toolName={toolName} preview={preview} lang={i18n.language} />
          </div>
          <div className="px-5 py-3 border-t bg-muted/30 rounded-b-xl space-y-3">
            <label className="flex items-center gap-2 text-xs text-muted-foreground cursor-pointer select-none">
              <input
                type="checkbox"
                checked={remember}
                onChange={(event) => setRemember(event.target.checked)}
                disabled={disabled}
                className="rounded border-border"
                aria-label={t("tool.alwaysAllowSession")}
              />
              {t("tool.alwaysAllowSession")}
            </label>
            {actionError && (
              <div role="alert" className="rounded-md border border-destructive/30 bg-destructive/10 px-2.5 py-2 text-xs text-destructive">
                {actionError}
              </div>
            )}
            <div className="flex gap-2 justify-end">
              <Button ref={rejectRef} type="button" variant="outline" disabled={disabled} onClick={() => void decide(false)}>
                {t("tool.reject")}
              </Button>
              <Button type="button" disabled={disabled} onClick={() => void decide(true)}>
                {submitting ? t("toolConfirm.processing") : t("tool.confirmExec")}
              </Button>
            </div>
          </div>
        </div>
      </FocusTrap>
    </div>
  );
}
