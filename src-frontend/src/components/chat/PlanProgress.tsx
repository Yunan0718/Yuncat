import { useState } from "react";
import { useTaskStore, type PlanStep } from "../../store/taskStore";
import {
  Check,
  X,
  Loader2,
  Rocket,
  ClipboardList,
  FileText,
  ChevronDown,
  ChevronRight,
  RotateCcw,
  AlertCircle,
} from "lucide-react";
import { Button } from "../ui/button";
import { useTranslation } from "react-i18next";

interface PlanProgressProps {
  taskId: string;
  onExecute?: () => void;
  onRetryStep?: (stepId?: string) => void;
}

export function PlanProgress({ taskId: _taskId, onExecute, onRetryStep }: PlanProgressProps) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState(true);
  const { planSteps } = useTaskStore();
  const hasSteps = planSteps.length > 0;
  const done = planSteps.filter((s) => s.status === "done").length;
  const running = planSteps.some((s) => s.status === "running");
  const failed = planSteps.some((s) => s.status === "failed");
  const pending = planSteps.filter((s) => s.status === "pending").length;
  const isComplete = done === planSteps.length && planSteps.length > 0;
  const allFiles = [...new Set(planSteps.flatMap((s) => s.filesChanged || []))];
  const runningStep = planSteps.find((s) => s.status === "running");
  const failedStep = planSteps.find((s) => s.status === "failed");
  const elapsedTotalMs = useElapsedTotal(planSteps);

  if (!hasSteps) return null;

  return (
    <div className="border rounded-xl bg-card shadow-sm overflow-hidden">
      {/* Header — 始终可见，running 状态带实时指示 */}
      <Button
        variant="ghost"
        size="default"
        onClick={() => setExpanded(!expanded)}
        className="w-full justify-start h-auto rounded-none px-4 py-3 border-b flex items-center gap-2 text-left hover:bg-muted/30 transition-colors"
      >
        <ClipboardList size={15} className="text-primary shrink-0" />
        <span className="text-sm font-semibold flex-1 truncate">
          {isComplete
            ? t("plan.complete")
            : running
              ? t("plan.executing")
              : failed
                ? t("plan.interrupted")
                : t("plan.executing")}
        </span>
        {running && (
          <span className="text-xs text-muted-foreground truncate max-w-[40%]">
            {runningStep ? `${runningStep.step}. ${runningStep.task}` : ""}
          </span>
        )}
        {!running && pending > 0 && (
          <span className="text-xs text-muted-foreground">{t("plan.pendingSteps", { n: pending })}</span>
        )}
        {running && elapsedTotalMs > 0 && (
          <span className="text-xs text-muted-foreground tabular-nums">
            {t("plan.elapsedTotal", { s: Math.max(1, Math.round(elapsedTotalMs / 1000)) })}
          </span>
        )}
        <span className="text-xs text-muted-foreground tabular-nums">
          {done}/{planSteps.length}
        </span>
        {expanded ? (
          <ChevronDown size={14} className="text-muted-foreground shrink-0" />
        ) : (
          <ChevronRight size={14} className="text-muted-foreground shrink-0" />
        )}
      </Button>

      {/* Progress bar */}
      <div className="h-1 bg-muted">
        <div
          className={`h-full transition-[width] duration-300 ease-out rounded-r ${isComplete ? "bg-success" : failed ? "bg-destructive" : "bg-primary"}`}
          style={{ width: `${Math.max(5, (done / planSteps.length) * 100)}%` }}
        />
      </div>

      {/* Steps list */}
      {expanded && (
        <div className="px-4 py-2 space-y-0.5 max-h-64 overflow-y-auto">
          {planSteps.map((s) => (
            <StepRow key={s.step} step={s} onRetry={onRetryStep} />
          ))}
        </div>
      )}

      {/* Files summary */}
      {allFiles.length > 0 && (
        <div className="px-4 py-2 border-t text-2xs text-muted-foreground">
          <span className="font-medium">{t("plan.filesInvolved")}</span>
          {allFiles.map((f) => (
            <code key={f} className="bg-muted px-1.5 py-0.5 rounded font-mono ml-1 text-2xs">
              {f}
            </code>
          ))}
        </div>
      )}

      {/* Actions */}
      {(!isComplete || failed) && !running && (
        <div className="px-4 py-2 border-t flex gap-2 items-center">
          <Button size="xs" onClick={onExecute} className="gap-1">
            <Rocket size={12} />
            {failed ? t("plan.retryPlan") : t("plan.executePlan")}
          </Button>
          {failed && failedStep && (
            <span className="text-xs text-destructive">{t("plan.stepFailed", { n: failedStep.step })}</span>
          )}
        </div>
      )}
    </div>
  );
}

function StepRow({ step: s, onRetry }: { step: PlanStep; onRetry?: (stepId?: string) => void }) {
  const { t } = useTranslation();
  const [showOutput, setShowOutput] = useState(false);
  const hasOutput = Boolean(s.output);
  const hasError = Boolean(s.error);

  return (
    <div
      className={`flex items-start gap-2 px-2 py-1.5 rounded-md text-xs transition-colors border ${s.status === "failed" ? "bg-destructive/5 border-destructive/20" : s.status === "running" ? "bg-primary/5 border-primary/10" : "border-transparent"}`}
    >
      <span className="mt-0.5 shrink-0">
        {s.status === "done" ? (
          <Check size={13} className="text-success" />
        ) : s.status === "running" ? (
          <Loader2 size={13} className="text-primary animate-spin" />
        ) : s.status === "failed" ? (
          <X size={13} className="text-destructive" />
        ) : (
          <span className="w-3.5 h-3.5 rounded-full border-2 border-muted-foreground/30 inline-block" />
        )}
      </span>
      <div className="flex-1 min-w-0">
        <div
          className={`flex items-start gap-2 ${s.status === "done" ? "text-muted-foreground" : s.status === "failed" ? "text-destructive font-medium" : s.status === "running" ? "font-medium" : ""}`}
        >
          <span className="flex-1">
            {s.step}. {s.task}
          </span>
          {s.elapsedMs !== undefined && s.status !== "pending" && (
            <span className="text-2xs text-muted-foreground tabular-nums shrink-0">
              {t("plan.stepElapsed", { ms: s.elapsedMs })}
            </span>
          )}
          {s.status === "failed" && onRetry && (
            <Button
              variant="link"
              size="xs"
              onClick={() => onRetry(s.stepId)}
              className="h-auto px-0 py-0"
              title={t("plan.retryStep")}
            >
              <RotateCcw size={10} />
              {t("plan.retryStep")}
            </Button>
          )}
        </div>
        {s.fileHint && s.status === "pending" && (
          <div className="text-2xs text-warning mt-0.5 flex items-center gap-1">
            <FileText size={10} />
            {t("plan.willModify")}: {s.fileHint}
          </div>
        )}
        {s.filesChanged && s.filesChanged.length > 0 && s.status === "done" && (
          <div className="text-2xs text-success mt-0.5 flex items-center gap-1">
            <Check size={10} />
            {t("plan.modified")}: {s.filesChanged.join(", ")}
          </div>
        )}
        {hasError && (
          <div className="mt-1.5 flex items-start gap-1 text-2xs text-destructive">
            <AlertCircle size={11} className="mt-0.5 shrink-0" />
            <span className="break-words">
              {t("plan.stepError")}: {s.error}
            </span>
          </div>
        )}
        {hasOutput && (
          <Button variant="link" size="xs" onClick={() => setShowOutput(!showOutput)} className="h-auto px-0 py-0">
            {showOutput ? t("plan.hideOutput") : t("plan.showOutput")}
          </Button>
        )}
        {showOutput && s.output && (
          <pre className="mt-1.5 p-2 bg-secondary/50 rounded text-2xs leading-relaxed whitespace-pre-wrap break-words max-h-32 overflow-y-auto">
            {s.output}
          </pre>
        )}
      </div>
    </div>
  );
}

/**
 * 已完成步骤的实测耗时之和（毫秒）。
 *
 * 诚实耗时原则（借鉴 deepseek-harness）：进行中的事情只报已观测事实，
 * 不按「已完成步骤平均值 × 剩余步数」外推剩余时间——那种外推在步骤耗时
 * 差异大时误差可达数量级，且每完成一步就剧烈跳动，比不显示更不可信。
 */
function useElapsedTotal(steps: PlanStep[]): number {
  return steps.reduce((sum, s) => sum + (s.elapsedMs ?? 0), 0);
}
