import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { X } from "lucide-react";
import { useTranslation } from "react-i18next";
import { ToastContext, _setGlobalToastListener, type Toast } from "./toast-context";

const NOTIFICATION_MAX_VISIBLE = 5;
const NOTIFICATION_EXIT_MS = 140;
const NOTIFICATION_DURATION_MS: Record<Toast["type"], number> = {
  info: 4_000,
  success: 4_000,
  error: 6_000,
};

let _nextId = 0;

function useToastLifecycle(durationMs: number, onDismiss: () => void) {
  const [paused, setPaused] = useState(false);
  const [leaving, setLeaving] = useState(false);
  const remainingRef = useRef(durationMs);

  useEffect(() => {
    if (paused || leaving) return;
    const startedAt = Date.now();
    const timer = window.setTimeout(() => setLeaving(true), remainingRef.current);
    return () => {
      window.clearTimeout(timer);
      remainingRef.current = Math.max(0, remainingRef.current - (Date.now() - startedAt));
    };
  }, [paused, leaving]);

  useEffect(() => {
    if (!leaving) return;
    const timer = window.setTimeout(onDismiss, NOTIFICATION_EXIT_MS);
    return () => window.clearTimeout(timer);
  }, [leaving, onDismiss]);

  return { paused, setPaused, leaving, beginExit: () => setLeaving(true) };
}

function ToastItem({
  item, onDismiss, closeLabel,
}: {
  item: Toast;
  onDismiss: (id: number) => void;
  closeLabel: string;
}) {
  const dismiss = useCallback(() => onDismiss(item.id), [item.id, onDismiss]);
  const { paused, setPaused, leaving, beginExit } = useToastLifecycle(
    NOTIFICATION_DURATION_MS[item.type],
    dismiss,
  );
  const toneClass = item.type === "error"
    ? "bg-destructive-solid text-destructive-foreground border-destructive-solid"
    : item.type === "success"
      ? "bg-success-solid text-success-foreground border-success-solid"
      : "bg-card text-foreground border-border";

  return (
    <div
      role={item.type === "error" ? "alert" : "status"}
      aria-live={item.type === "error" ? "assertive" : "polite"}
      aria-atomic="true"
      data-paused={paused || undefined}
      className={`${leaving ? "animate-toast-exit" : "animate-slide-up"} pointer-events-auto flex w-full items-start gap-2 rounded-lg border px-4 py-3 text-sm font-medium shadow-lg sm:w-auto sm:min-w-[280px] sm:max-w-[380px] ${toneClass}`}
      onMouseEnter={() => setPaused(true)}
      onMouseLeave={() => setPaused(false)}
      onFocusCapture={() => setPaused(true)}
      onBlurCapture={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setPaused(false);
      }}
    >
      <span className="min-w-0 flex-1 whitespace-pre-wrap break-words">{item.message}</span>
      <button
        type="button"
        onClick={beginExit}
        className="-m-1 inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-sm opacity-70 hover:opacity-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
        aria-label={closeLabel}
      >
        <X size={14} aria-hidden="true" />
      </button>
    </div>
  );
}

export function ToastProvider({ children }: { children: ReactNode }) {
  const { t } = useTranslation();
  const [toasts, setToasts] = useState<Toast[]>([]);

  const toast = useCallback((message: string, type: Toast["type"] = "info") => {
    const id = ++_nextId;
    setToasts((previous) => [
      ...previous.slice(-(NOTIFICATION_MAX_VISIBLE - 1)),
      { id, message, type },
    ]);
  }, []);

  // 非 React 代码（WS handler 等）通过全局 emitter 触发同一套通知。
  useEffect(() => {
    _setGlobalToastListener(toast);
    return () => _setGlobalToastListener(null);
  }, [toast]);

  const dismiss = useCallback((id: number) => {
    setToasts((previous) => previous.filter((item) => item.id !== id));
  }, []);

  // Portal 避免被祖先 overflow/stacking context 裁剪；fixed 容器本身不拦截空白区域的点击。
  const toastRoot = document.getElementById("overlay-root") ?? document.body;

  return (
    <ToastContext.Provider value={{ toast }}>
      {children}
      {createPortal(
        <div className="pointer-events-none fixed inset-x-4 bottom-4 z-toast flex flex-col items-end gap-2 sm:inset-x-auto sm:right-4">
          {toasts.map((item) => (
            <ToastItem key={item.id} item={item} onDismiss={dismiss} closeLabel={t("common.close")} />
          ))}
        </div>,
        toastRoot,
      )}
    </ToastContext.Provider>
  );
}
