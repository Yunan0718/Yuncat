import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Ban, CalendarClock } from "lucide-react";
import { useApi } from "../../hooks/useApi";
import { useConfirmDialog } from "../../hooks/useConfirmDialog";
import { emitGlobalToast } from "../common/toast-context";
import { LoadingCurve } from "../common/LoadingCurve";
import { EmptyState } from "../common/EmptyState";

interface ScheduleRow {
  id: string;
  task_input: string;
  agent_id: string;
  session_id?: string | null;
  cron_expr: string;
  next_run_at: number;
  status: string;
  label: string;
  last_run_at?: number | null;
  run_count: number;
  error_count: number;
  max_budget_tokens?: number | null;
  used_tokens: number;
}

function formatTime(ts?: number | null): string {
  if (!ts) return "—";
  try {
    return new Date(ts * 1000).toLocaleString();
  } catch {
    return "—";
  }
}

const STATUS_CLASS: Record<string, string> = {
  pending: "bg-primary/10 text-primary",
  running: "bg-primary/10 text-primary",
  paused: "bg-warning/10 text-warning",
  completed: "bg-success/10 text-success",
  failed: "bg-destructive/10 text-destructive",
};

/**
 * 定时任务管理（对标 DSH schedule 的"人机确认"那一半）。
 *
 * agent 用 cron_schedule 建任务，但用户侧此前没有任何入口：
 * 建了哪些、下次什么时候跑、上次成功没有、怎么取消，全都看不到。
 */
export function SchedulesSection() {
  const { t } = useTranslation();
  const { api } = useApi();
  const { confirm, confirmDialog } = useConfirmDialog();
  const [rows, setRows] = useState<ScheduleRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const r = await api<{ items: ScheduleRow[] }>("/api/v1/schedules");
      setRows(r.items || []);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setRows([]);
    }
  }, [api]);

  useEffect(() => {
    void load();
  }, [load]);

  async function cancel(row: ScheduleRow) {
    const ok = await confirm({
      title: t("schedules.cancelTitle"),
      description: t("schedules.cancelConfirm", { task: row.task_input.slice(0, 80) }),
      variant: "destructive",
      confirmText: t("schedules.cancelAction"),
    });
    if (!ok) return;
    setBusyId(row.id);
    try {
      await api(`/api/v1/schedules/${encodeURIComponent(row.id)}/cancel`, { method: "POST" });
      emitGlobalToast(t("schedules.cancelled"), "success");
      await load();
    } catch (e) {
      emitGlobalToast(`${t("schedules.cancelFailed")}: ${e instanceof Error ? e.message : String(e)}`, "error");
    } finally {
      setBusyId(null);
    }
  }

  return (
    <div className="space-y-4">
      <div>
        <h2 className="text-xl font-semibold tracking-tight flex items-center gap-2">
          <CalendarClock size={18} />
          {t("schedules.title")}
        </h2>
        <p className="text-sm text-muted-foreground mt-1">{t("schedules.description")}</p>
      </div>

      {error && <div role="alert" className="text-sm text-destructive">{error}</div>}

      {rows === null ? (
        <LoadingCurve />
      ) : rows.length === 0 ? (
        <EmptyState title={t("schedules.empty")} description={t("schedules.emptyHint")} />
      ) : (
        <div className="divide-y rounded-lg border">
          {rows.map((row) => (
            <div key={row.id} className="flex items-start gap-3 px-3 py-2.5">
              <div className="min-w-0 flex-1 space-y-1">
                <div className="flex items-center gap-2">
                  <span className={`rounded px-1.5 py-0.5 text-[10px] ${STATUS_CLASS[row.status] || "bg-muted text-muted-foreground"}`}>
                    {t(`schedules.status.${row.status}`, { defaultValue: row.status })}
                  </span>
                  <span className="truncate text-sm">{row.task_input}</span>
                </div>
                <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-muted-foreground font-mono">
                  <span title={t("schedules.cronHint")}>{row.cron_expr || t("schedules.once")}</span>
                  <span>{t("schedules.nextRun", { at: formatTime(row.next_run_at) })}</span>
                  <span>{t("schedules.lastRun", { at: formatTime(row.last_run_at) })}</span>
                  <span>{t("schedules.runCount", { count: row.run_count })}</span>
                  {row.error_count > 0 && (
                    <span className="text-warning">{t("schedules.errorCount", { count: row.error_count })}</span>
                  )}
                  {row.agent_id && <span>agent: {row.agent_id}</span>}
                </div>
              </div>
              {row.status === "pending" && (
                <button
                  type="button"
                  onClick={() => void cancel(row)}
                  disabled={busyId === row.id}
                  title={t("schedules.cancelAction")}
                  aria-label={t("schedules.cancelAction")}
                  className="shrink-0 rounded p-1 text-muted-foreground hover:text-destructive disabled:opacity-50"
                >
                  <Ban size={14} />
                </button>
              )}
            </div>
          ))}
        </div>
      )}
      {confirmDialog}
    </div>
  );
}
