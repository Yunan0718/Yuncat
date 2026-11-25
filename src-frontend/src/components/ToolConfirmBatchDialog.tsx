import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Button } from "./ui/button";
import { FocusTrap } from "./FocusTrap";
import type { ConfirmDialogItem } from "../types/task";

function toolLabel(
  name: string,
  tFn: (key: string, opts?: Record<string, unknown>) => string,
): string {
  const key = `tools.${name}`;
  const translated = tFn(key, { defaultValue: name });
  return typeof translated === "string" && translated !== key ? translated : name;
}

function itemSummary(item: ConfirmDialogItem): string {
  const p = item.preview;
  if (item.toolName === "run_command") {
    return String(p.command ?? "");
  }
  return String(
    p.path ?? p.old_path ?? p.new_path ?? p.file ?? "",
  );
}

interface ToolConfirmBatchDialogProps {
  open: boolean;
  items: ConfirmDialogItem[];
  taskPrompt?: string;
  onConfirm: (approved: boolean, remember: boolean) => void | Promise<void>;
  /** 所有确认令牌都已过期时只关闭本地弹窗，不再提交必然失败的请求。 */
  onExpire?: () => void;
}

/**
 * Batch 3.2: 多项工具批量确认弹窗。
 *
 * 同一轮出现多个 confirmable 工具时，统一展示列表供用户一次性批准/拒绝。
 * 提交期间锁定操作；失败时保留弹窗并展示错误，过期项会单独标记。
 */
export function ToolConfirmBatchDialog({
  open,
  items,
  taskPrompt,
  onConfirm,
  onExpire,
}: ToolConfirmBatchDialogProps) {
  const { t } = useTranslation();
  const rejectRef = useRef<HTMLButtonElement>(null);
  const expiryHandledRef = useRef(false);
  const submittingRef = useRef(false);
  const decideRef = useRef<(approved: boolean, remember?: boolean) => void>(() => {});
  const [remember, setRemember] = useState(false);
  const [remaining, setRemaining] = useState<number | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  async function decide(approved: boolean, rememberChoice = remember) {
    if (submittingRef.current) return;
    submittingRef.current = true;
    setSubmitting(true);
    setActionError(null);
    try {
      await onConfirm(approved, rememberChoice);
    } catch (error) {
      const detail = error instanceof Error ? error.message : String(error);
      setActionError(t("toolConfirmBatch.failed", { error: detail }));
    } finally {
      submittingRef.current = false;
      setSubmitting(false);
    }
  }
  decideRef.current = decide;

  const latestExpiry = items.reduce((latest, item) => Math.max(latest, item.expiresAt || 0), 0);
  useEffect(() => {
    if (!open) {
      setRemember(false);
      setActionError(null);
      setSubmitting(false);
      expiryHandledRef.current = false;
      return;
    }
    if (!latestExpiry) {
      setRemaining(null);
      return;
    }
    const calc = () => Math.max(0, Math.ceil(latestExpiry - Date.now() / 1000));
    setRemaining(calc());
    const timer = setInterval(() => setRemaining(calc()), 1000);
    return () => clearInterval(timer);
  }, [open, latestExpiry]);

  useEffect(() => {
    if (!open || remaining !== 0 || expiryHandledRef.current) return;
    expiryHandledRef.current = true;
    if (onExpire) onExpire();
    else decideRef.current(false, false);
  }, [open, remaining, onExpire]);

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || submittingRef.current) return;
      event.preventDefault();
      decideRef.current(false, false);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open]);

  if (!open) return null;
  const now = Date.now() / 1000;
  const disabled = submitting || remaining === 0;

  return (
    <div
      className="fixed inset-0 bg-black/40 flex items-center justify-center p-4 z-modal animate-modal-overlay"
      onClick={(event) => { if (event.target === event.currentTarget) return; }}
      role="dialog"
      aria-modal="true"
      aria-labelledby="tool-confirm-batch-title"
      aria-busy={submitting}
    >
      <FocusTrap active={open} initialFocusRef={rejectRef}>
        <div
          className="bg-card rounded-xl shadow-lg max-w-2xl w-full max-h-[80vh] overflow-y-auto outline-none animate-modal-content"
          onClick={(event) => event.stopPropagation()}
        >
          <div className="px-5 py-3 border-b flex items-center gap-2">
            <span className="w-2 h-2 rounded-full bg-warning" aria-hidden="true" />
            <h3 id="tool-confirm-batch-title" className="font-medium text-sm">
              {t("toolConfirmBatch.title", { count: items.length })}
            </h3>
            {remaining !== null && (
              <span className="ml-auto text-xs font-mono tabular-nums text-muted-foreground" data-testid="batch-confirm-remaining">
                {t("toolConfirm.remaining", {
                  time: `${String(Math.floor(remaining / 60)).padStart(2, "0")}:${String(remaining % 60).padStart(2, "0")}`,
                })}
              </span>
            )}
          </div>
          {taskPrompt ? (
            <div className="px-5 py-2 bg-muted/40 border-b text-xs text-muted-foreground truncate" title={taskPrompt}>
              {t("toolConfirm.sourceTask", { prompt: taskPrompt })}
            </div>
          ) : null}
          <div className="p-4 space-y-2">
            {items.map((item, idx) => {
              const expired = Boolean(item.expiresAt && item.expiresAt <= now);
              return (
                <div key={item.confirmToken || idx} className="border rounded-lg p-3">
                  <div className="flex items-center gap-2 text-sm font-medium">
                    <span>{toolLabel(item.toolName, t)}</span>
                    {expired && (
                      <span className="ml-auto rounded bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">
                        {t("toolConfirm.expiredItem")}
                      </span>
                    )}
                  </div>
                  <div className="text-xs text-muted-foreground truncate">{itemSummary(item)}</div>
                </div>
              );
            })}
          </div>
          <div className="px-5 py-3 border-t bg-muted/30 rounded-b-xl space-y-3">
            <label className="flex items-center gap-2 text-xs text-muted-foreground cursor-pointer select-none">
              <input
                type="checkbox"
                checked={remember}
                onChange={(event) => setRemember(event.target.checked)}
                disabled={disabled}
                className="rounded border-border"
                aria-label={t("toolConfirmBatch.alwaysAllowSession")}
              />
              {t("toolConfirmBatch.alwaysAllowSession")}
            </label>
            {actionError && (
              <div role="alert" className="rounded-md border border-destructive/30 bg-destructive/10 px-2.5 py-2 text-xs text-destructive">
                {actionError}
              </div>
            )}
            <div className="flex gap-2 justify-end">
              <Button ref={rejectRef} type="button" variant="outline" disabled={disabled} onClick={() => void decide(false, false)}>
                {t("toolConfirmBatch.rejectAll")}
              </Button>
              <Button type="button" disabled={disabled} onClick={() => void decide(true)}>
                {submitting ? t("toolConfirm.processing") : t("toolConfirmBatch.approveAll")}
              </Button>
            </div>
          </div>
        </div>
      </FocusTrap>
    </div>
  );
}
