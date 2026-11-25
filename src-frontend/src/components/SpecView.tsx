import { useEffect, useMemo } from "react";
import { CheckCircle, XCircle, AlertTriangle, RotateCw, FileText, Code2, Search, GanttChart } from "lucide-react";
import { useSpecStore, type SpecPhase } from "@/store/specStore";
import { useTranslation } from "react-i18next";

/** 阶段配置 — 图标/颜色/i18n key */
const PHASE_CONFIG: Record<SpecPhase, { icon: typeof CheckCircle; labelKey: string; color: string }> = {
  idle: { icon: GanttChart, labelKey: "specView.phase.idle", color: "text-muted-foreground" },
  checkpoint_resumed: { icon: RotateCw, labelKey: "specView.phase.checkpoint_resumed", color: "text-primary" },
  round_start: { icon: RotateCw, labelKey: "specView.phase.round_start", color: "text-muted-foreground" },
  architect: { icon: FileText, labelKey: "specView.phase.architect", color: "text-primary" },
  architect_done: { icon: CheckCircle, labelKey: "specView.phase.architect_done", color: "text-success" },
  architect_failed: { icon: XCircle, labelKey: "specView.phase.architect_failed", color: "text-destructive" },
  developer: { icon: Code2, labelKey: "specView.phase.developer", color: "text-warning" },
  developer_done: { icon: CheckCircle, labelKey: "specView.phase.developer_done", color: "text-success" },
  auditor: { icon: Search, labelKey: "specView.phase.auditor", color: "text-primary" },
  auditor_done: { icon: CheckCircle, labelKey: "specView.phase.auditor_done", color: "text-success" },
  convergence_check: { icon: CheckCircle, labelKey: "specView.phase.convergence_check", color: "text-success" },
  escalated: { icon: AlertTriangle, labelKey: "specView.phase.escalated", color: "text-destructive" },
};

const DONE_PHASES: SpecPhase[] = [
  "architect_done",
  "developer_done",
  "auditor_done",
  "convergence_check",
  "architect_failed",
];

/** 阶段分组 — 三阶段 Spec Loop */
const STAGES = [
  {
    key: "architect" as const,
    labelKey: "specView.stages.architect",
    phases: ["architect", "architect_done", "architect_failed"] as SpecPhase[],
  },
  {
    key: "developer" as const,
    labelKey: "specView.stages.developer",
    phases: ["developer", "developer_done"] as SpecPhase[],
  },
  {
    key: "auditor" as const,
    labelKey: "specView.stages.auditor",
    phases: ["auditor", "auditor_done"] as SpecPhase[],
  },
];

/** Props */
interface SpecViewProps {
  taskId: string | null;
  onBackToChat: () => void;
}

export default function SpecView({ taskId, onBackToChat }: SpecViewProps) {
  const { t } = useTranslation();
  const phase = useSpecStore((s) => s.phase);
  const round = useSpecStore((s) => s.round);
  const maxRounds = useSpecStore((s) => s.maxRounds);
  const specName = useSpecStore((s) => s.specName);
  const frCount = useSpecStore((s) => s.frCount);
  const filesCount = useSpecStore((s) => s.filesCount);
  const files = useSpecStore((s) => s.files);
  const auditReport = useSpecStore((s) => s.auditReport);
  const converged = useSpecStore((s) => s.converged);
  const totalRounds = useSpecStore((s) => s.totalRounds);
  const active = useSpecStore((s) => s.active);
  const error = useSpecStore((s) => s.error);
  const checkpointResumed = useSpecStore((s) => s.checkpointResumed);
  const reset = useSpecStore((s) => s.reset);

  // 完成后 30s 自动重置
  useEffect(() => {
    if (!active && (converged !== null || error)) {
      const timer = setTimeout(() => reset(), 30000);
      return () => clearTimeout(timer);
    }
  }, [active, converged, error, reset]);

  const currentCfg = PHASE_CONFIG[phase];

  /** 判断某个 stage 的 active / done 状态——用 key 做稳定比较 */
  function stageStatus(stageKey: string, stagePhases: SpecPhase[]): "pending" | "active" | "done" {
    if (phase === "idle") return "pending";
    if (stagePhases.includes(phase) && DONE_PHASES.includes(phase)) return "done";
    if (stagePhases.includes(phase)) return "active";
    const stageIdx = STAGES.findIndex((s) => s.phases.includes(phase));
    const thisIdx = STAGES.findIndex((s) => s.key === stageKey);
    if (stageIdx > thisIdx) return "done";
    return "pending";
  }

  const roundLabel = useMemo(() => {
    if (totalRounds > 0) return t("specView.roundTotal", { count: totalRounds });
    return round > 0 ? t("specView.roundCurrent", { current: round, max: maxRounds }) : "";
  }, [round, maxRounds, totalRounds, t]);

  return (
    <div className="flex flex-col h-full bg-card rounded-2xl overflow-hidden">
      {/* ── Header ── */}
      <div className="flex items-center justify-between px-5 py-3 bg-secondary/30">
        <div className="flex items-center gap-2">
          <GanttChart size={18} className="text-primary" />
          <span className="font-semibold text-sm">{t("specView.title")}</span>
          {active && (
            <span className="inline-flex items-center gap-1 text-[10px] text-muted-foreground bg-muted px-2 py-0.5 rounded-full">
              <span className="w-1.5 h-1.5 rounded-full bg-success animate-pulse" />
              {t("specView.running")}
            </span>
          )}
          {checkpointResumed && (
            <span className="text-[10px] text-primary bg-primary/10 px-2 py-0.5 rounded-full">
              {t("specView.checkpointResumed")}
            </span>
          )}
        </div>

        <div className="flex items-center gap-3">
          <span className="text-[11px] text-muted-foreground font-mono">{roundLabel}</span>
          <button
            onClick={onBackToChat}
            className="text-[11px] text-muted-foreground hover:text-foreground transition-colors"
          >
            {t("specView.backToChat")}
          </button>
        </div>
      </div>

      {/* ── 三阶段进度条 ── */}
      <div className="px-5 py-3 flex gap-2">
        {STAGES.map((stage, idx) => {
          const status = stageStatus(stage.key, stage.phases);
          return (
            <div key={stage.key} className="flex-1 flex items-center gap-2">
              <div
                className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-[11px] font-medium transition-colors
                  ${status === "done" ? "bg-success/10 text-success" : ""}
                  ${status === "active" ? "bg-primary/10 text-primary ring-1 ring-primary/30" : ""}
                  ${status === "pending" ? "bg-muted text-muted-foreground" : ""}`}
              >
                {status === "done" ? (
                  <CheckCircle size={12} />
                ) : status === "active" ? (
                  <RotateCw size={12} className="animate-spin" />
                ) : (
                  <span className="w-3 h-3 rounded-full border border-muted-foreground/30" />
                )}
                <span>{t(stage.labelKey)}</span>
              </div>
              {idx < STAGES.length - 1 && (
                <div className={`flex-1 h-px ${status === "done" ? "bg-success/30" : "bg-border"}`} />
              )}
            </div>
          );
        })}
      </div>

      {/* ── 状态提示 ── */}
      <div className="px-5 py-2 flex items-center gap-2">
        <currentCfg.icon size={14} className={currentCfg.color} />
        <span className={`text-xs font-medium ${currentCfg.color}`}>{t(currentCfg.labelKey)}</span>
        {error && <span className="text-xs text-destructive ml-auto">{error}</span>}
      </div>

      {/* ── 详情面板 ── */}
      <div className="flex-1 overflow-auto px-5 py-3 space-y-3">
        {/* Spec 信息 */}
        {specName && (
          <InfoCard title={t("chat.mode_spec")} icon={<FileText size={13} />}>
            <div className="text-xs text-foreground font-medium">{specName}</div>
            <div className="text-[10px] text-muted-foreground">
              {frCount > 0 && t("specView.frCount", { count: frCount })}
              {filesCount > 0 && t("specView.filesCount", { count: filesCount })}
              {round > 0 && t("specView.roundLabel", { round })}
            </div>
          </InfoCard>
        )}

        {/* 文件列表 */}
        {files.length > 0 && (
          <InfoCard title={t("legacy.specView_995b")} icon={<Code2 size={13} />}>
            <div className="flex flex-wrap gap-1">
              {files.map((f) => (
                <code key={f} className="text-[10px] bg-muted px-1.5 py-0.5 rounded font-mono">
                  {f}
                </code>
              ))}
            </div>
          </InfoCard>
        )}

        {/* 审计报告 */}
        {auditReport && (
          <InfoCard title={t("legacy.specView_1ff2")} icon={<Search size={13} />}>
            <div className="flex items-center gap-4 text-xs">
              <AuditBadge label={t("specView.match")} count={auditReport.matches} color="text-success bg-success/10" />
              <AuditBadge label={t("specView.mismatch")} count={auditReport.mismatches} color="text-destructive bg-destructive/10" />
              <AuditBadge label={t("specView.missing")} count={auditReport.missing} color="text-warning bg-warning/10" />
              <span className="text-[10px] text-muted-foreground ml-auto">
                {t("specView.verification")}: {auditReport.verificationPassed ? "✅" : "❌"}
              </span>
            </div>
            {/* MISMATCH details */}
            {auditReport.mismatchesList.length > 0 && (
              <div className="mt-2 space-y-0.5">
                {auditReport.mismatchesList.slice(0, 5).map((m) => (
                  <div key={m} className="text-[10px] text-destructive flex items-start gap-1">
                    <XCircle size={10} className="mt-0.5 shrink-0" />
                    <span>{m}</span>
                  </div>
                ))}
              </div>
            )}
            {/* MISSING details */}
            {auditReport.missingList.length > 0 && (
              <div className="mt-2 space-y-0.5">
                {auditReport.missingList.slice(0, 5).map((m) => (
                  <div key={m} className="text-[10px] text-warning flex items-start gap-1">
                    <AlertTriangle size={10} className="mt-0.5 shrink-0" />
                    <span>{m}</span>
                  </div>
                ))}
              </div>
            )}
          </InfoCard>
        )}

        {/* 收敛结果 */}
        {converged !== null && !active && (
          <div
            className={`p-4 rounded-xl border ${converged ? "border-success/20 bg-success/5" : "border-destructive/20 bg-destructive/5"}`}
          >
            <div className="flex items-center gap-2">
              {converged ? (
                <>
                  <CheckCircle size={16} className="text-success" />
                  <span className="text-sm font-semibold text-success">{t("specView.converged")}</span>
                </>
              ) : (
                <>
                  <AlertTriangle size={16} className="text-destructive" />
                  <span className="text-sm font-semibold text-destructive">{t("specView.notConverged")}</span>
                </>
              )}
            </div>
            <div className="mt-1 text-[10px] text-muted-foreground">
              {totalRounds > 0 ? t("specView.totalRoundsExecuted", { count: totalRounds }) : ""}
            </div>
          </div>
        )}
      </div>

      {/* ── Footer ── */}
      <div className="px-5 py-2 bg-secondary/30">
        <div className="text-[10px] text-muted-foreground">
          {taskId && (
            <span>
              {t("specView.taskLabel")}: {taskId.slice(0, 8)}
            </span>
          )}
          {!active && converged == null && !error && (
            <span className="ml-2">{t("specView.waiting")}</span>
          )}
        </div>
      </div>
    </div>
  );
}

/** 小卡片 */
function InfoCard({ title, icon, children }: { title: string; icon: React.ReactNode; children: React.ReactNode }) {
  return (
    <div className="rounded-xl bg-card/50 p-3">
      <div className="flex items-center gap-1.5 text-[10px] text-muted-foreground mb-2">
        {icon}
        <span className="font-medium uppercase tracking-wider">{title}</span>
      </div>
      {children}
    </div>
  );
}

/** 审计 Badge */
function AuditBadge({ label, count, color }: { label: string; count: number; color: string }) {
  return (
    <span
      className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-mono font-medium ${color}`}
    >
      {label}: {count}
    </span>
  );
}
