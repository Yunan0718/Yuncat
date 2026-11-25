import type { TaskState } from "../../store/taskStore";
import { useTranslation } from "react-i18next";
import { LoadingCurve } from "./LoadingCurve";

interface StatusIndicatorProps {
  status: TaskState["status"];
  progress?: { step: number; total: number };
  errorMessage?: string | null;
  chunkCount?: number;
  thinkingLength?: number;
}

export function StatusIndicator({
  status,
  progress,
  errorMessage,
  chunkCount = 0,
  thinkingLength = 0,
}: StatusIndicatorProps) {
  const { t } = useTranslation();

  if (status === "running" && progress) {
    const pct = Math.round((progress.step / progress.total) * 100);
    return (
      <div className="flex items-center gap-2 text-xs text-muted-foreground mt-1">
        <div className="w-20 h-1.5 bg-secondary rounded-full overflow-hidden">
          <div className="h-full bg-primary transition-[width] duration-300 ease-out" style={{ width: `${pct}%` }} />
        </div>
        <span>
          {progress.step}/{progress.total} {t("status.step")}
        </span>
      </div>
    );
  }

  if (status === "running" && chunkCount === 0) {
    return (
      <div className="flex items-center gap-2 text-xs text-muted-foreground mt-1">
        <LoadingCurve variant="rose" size={16} particleCount={4} />
        <span>{t("status.thinking")}</span>
      </div>
    );
  }

  if (status === "running" && chunkCount > 0) {
    return (
      <span className="inline-block mt-1 text-xs text-muted-foreground animate-pulse">
        ● {t("status.streaming")}
        {thinkingLength > 0 && (
          <span className="ml-1.5 tabular-nums">{t("status.thinkingLength", { length: thinkingLength })}</span>
        )}
      </span>
    );
  }

  if (status === "completed") {
    return <span className="inline-block mt-1 text-xs text-success">{t("status.done")}</span>;
  }

  if (status === "failed") {
    return (
      <div className="mt-1 space-y-1">
        <span className="inline-block text-xs text-destructive">{t("status.failed")}</span>
        {errorMessage && (
          <div className="text-xs text-destructive bg-destructive/10 rounded px-2 py-1 max-w-md">{errorMessage}</div>
        )}
      </div>
    );
  }

  if (status === "cancelled") {
    return <span className="inline-block mt-1 text-xs text-muted-foreground/50">{t("status.cancelled")}</span>;
  }

  return null;
}
