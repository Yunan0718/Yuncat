import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Button } from "../ui/button";
import { FocusTrap } from "../FocusTrap";
import type { BridgeApproval } from "../../store/taskStore";

/**
 * P0-7: Bridge 工具审批对话框——从 ChatView.tsx 抽取（候选 4）。
 * 纯展示 + onDecide 回调，fetch 副作用由父组件处理。
 */
export interface BridgeApprovalDialogProps {
  approval: BridgeApproval;
  onDecide: (approved: boolean) => void | Promise<void>;
}

/**
 * P0-7: Bridge 工具审批对话框——从 ChatView.tsx 抽取（候选 4）。
 * 纯展示 + onDecide 回调，fetch 副作用由父组件处理。
 */
export function BridgeApprovalDialog({ approval, onDecide }: BridgeApprovalDialogProps) {
  const { t } = useTranslation();
  const rejectRef = useRef<HTMLButtonElement>(null);
  const submittingRef = useRef(false);
  const decideRef = useRef<(approved: boolean) => void>(() => {});
  const [submitting, setSubmitting] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  async function decide(approved: boolean) {
    if (submittingRef.current) return;
    submittingRef.current = true;
    setSubmitting(true);
    setActionError(null);
    try {
      await onDecide(approved);
    } catch (error) {
      const detail = error instanceof Error ? error.message : String(error);
      setActionError(t("chatView.bridge.failed", { error: detail }));
    } finally {
      submittingRef.current = false;
      setSubmitting(false);
    }
  }
  decideRef.current = decide;

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || submittingRef.current) return;
      event.preventDefault();
      decideRef.current(false);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  return (
    <div
      className="fixed inset-0 bg-black/40 flex items-center justify-center p-4 z-modal animate-modal-overlay"
      onClick={(event) => { if (event.target === event.currentTarget) return; }}
      role="dialog"
      aria-modal="true"
      aria-labelledby="bridge-approval-title"
      aria-busy={submitting}
    >
      <FocusTrap active initialFocusRef={rejectRef}>
        <div
          className="bg-card rounded-xl shadow-lg max-w-lg w-full outline-none animate-modal-content"
          onClick={(event) => event.stopPropagation()}
        >
          <div className="px-5 py-3 border-b flex items-center gap-2">
            <span
              className={`w-2 h-2 rounded-full ${approval.autoApprove ? "bg-success animate-pulse" : "bg-warning"}`}
              aria-hidden="true"
            />
            <h3 id="bridge-approval-title" className="font-medium text-sm">
              Hermes: <code className="text-xs bg-secondary px-1.5 py-0.5 rounded">{approval.toolName}</code>
              {approval.autoApprove && (
                <span className="ml-2 text-xs text-muted-foreground">{t("chatView.bridge.autoApprove")}</span>
              )}
            </h3>
          </div>
          <div className="p-4 space-y-2">
            <pre className="bg-secondary text-foreground rounded-lg p-3 text-xs font-mono max-h-[200px] overflow-y-auto whitespace-pre-wrap break-all">
              {approval.arguments || t("chatView.bridge.noParams")}
            </pre>
            <p className="text-xs text-muted-foreground">{t("chatView.bridge.description")}</p>
            {actionError && (
              <div role="alert" className="rounded-md border border-destructive/30 bg-destructive/10 px-2.5 py-2 text-xs text-destructive">
                {actionError}
              </div>
            )}
          </div>
          <div className="px-5 py-3 border-t flex gap-2 justify-end bg-muted/30 rounded-b-xl">
            <Button ref={rejectRef} type="button" variant="outline" disabled={submitting} onClick={() => void decide(false)}>
              {approval.autoApprove ? t("chatView.bridge.rejectNow") : t("chatView.bridge.reject")}
            </Button>
            {!approval.autoApprove && (
              <Button type="button" disabled={submitting} onClick={() => void decide(true)}>
                {submitting ? t("toolConfirm.processing") : t("chatView.bridge.approve")}
              </Button>
            )}
          </div>
        </div>
      </FocusTrap>
    </div>
  );
}
