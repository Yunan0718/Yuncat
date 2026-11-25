import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Activity, RefreshCw, Timer } from "lucide-react";
import { useApi } from "../../hooks/useApi";
import { usePromptDialog } from "../../hooks/usePromptDialog";
import { emitGlobalToast } from "../common/toast-context";
import { LoadingCurve } from "../common/LoadingCurve";
import { EmptyState } from "../common/EmptyState";

interface LoopRow {
  loop_id: string;
  status: string;
  loop_type: string;
  interval: number;
  priority: number;
  last_run: number;
  last_ok: number;
  restart_count: number;
  error_count: number;
  iteration_count: number;
  last_error: string;
  strategy: string;
}

interface StatusPayload {
  ok: boolean;
  total: number;
  running: number;
  degraded: number;
  loops: LoopRow[];
}

// 与后端 runtime_bounds.LOOP_INTERVAL_MIN_SEC / LOOP_INTERVAL_MAX_SEC 保持一致：
// interval=0 会让循环空转，超大值等于停掉循环——前端先拦一道，后端仍是权威。
const LOOP_INTERVAL_MIN_SEC = 1;
const LOOP_INTERVAL_MAX_SEC = 86400;

const STATUS_CLASS: Record<string, string> = {
  running: "bg-success/10 text-success",
  degraded: "bg-warning/10 text-warning",
  stopped: "bg-muted text-muted-foreground",
  failed: "bg-destructive/10 text-destructive",
};

function relativeTime(ts: number, t: (k: string, o?: Record<string, unknown>) => string): string {
  if (!ts) return "—";
  const diff = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (diff < 60) return t("time.justNow");
  if (diff < 3600) return t("time.minutesAgo", { n: Math.floor(diff / 60) });
  if (diff < 86400) return t("time.hoursAgo", { n: Math.floor(diff / 3600) });
  return t("time.daysAgo", { n: Math.floor(diff / 86400) });
}

/**
 * 后台循环（LoopSupervisor）健康面板。
 *
 * 后端 `/api/v1/supervisor/status` + restart/interval 三个端点一直没有前端引用——
 * 「后台会不会掉」只能翻日志。这里把每个循环的状态、间隔、最近运行、重启/错误
 * 次数列出来，并支持启动（非 running 时）与调间隔。
 */
export function LoopsSection() {
  const { t } = useTranslation();
  const { api } = useApi();
  const { prompt, promptDialog } = usePromptDialog();
  const [data, setData] = useState<StatusPayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const payload = await api<StatusPayload>("/api/v1/supervisor/status");
      setData(payload);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [api]);

  useEffect(() => {
    void load();
    const timer = setInterval(() => void load(), 5000);
    return () => clearInterval(timer);
  }, [load]);

  async function startLoop(row: LoopRow) {
    setBusyId(row.loop_id);
    try {
      await api(`/api/v1/supervisor/loops/${encodeURIComponent(row.loop_id)}/restart`, {
        method: "POST",
      });
      emitGlobalToast(t("loops.started", { id: row.loop_id }), "success");
      await load();
    } catch (e) {
      emitGlobalToast(`${t("loops.failed")}: ${e instanceof Error ? e.message : String(e)}`, "error");
    } finally {
      setBusyId(null);
    }
  }

  async function changeInterval(row: LoopRow) {
    const input = await prompt({
      title: t("loops.intervalTitle", { id: row.loop_id }),
      placeholder: t("loops.intervalHint"),
      defaultValue: String(row.interval),
    });
    if (input === null) return;
    const seconds = Number(input.trim());
    if (!Number.isFinite(seconds)
      || seconds < LOOP_INTERVAL_MIN_SEC || seconds > LOOP_INTERVAL_MAX_SEC) {
      emitGlobalToast(t("loops.intervalInvalid"), "error");
      return;
    }
    setBusyId(row.loop_id);
    try {
      await api(
        `/api/v1/supervisor/loops/${encodeURIComponent(row.loop_id)}/interval?interval=${Math.round(seconds)}`,
        { method: "POST" },
      );
      emitGlobalToast(t("loops.intervalUpdated", { id: row.loop_id, seconds: Math.round(seconds) }), "success");
      await load();
    } catch (e) {
      emitGlobalToast(`${t("loops.failed")}: ${e instanceof Error ? e.message : String(e)}`, "error");
    } finally {
      setBusyId(null);
    }
  }

  return (
    <div className="space-y-4">
      <div>
        <h2 className="text-xl font-semibold tracking-tight flex items-center gap-2">
          <Activity size={18} />
          {t("loops.title")}
        </h2>
        <p className="text-sm text-muted-foreground mt-1">{t("loops.description")}</p>
      </div>

      {data && (
        <div className="flex gap-4 text-xs text-muted-foreground">
          <span>{t("loops.summary", { running: data.running, total: data.total })}</span>
          {data.degraded > 0 && (
            <span className="text-warning">{t("loops.degradedCount", { count: data.degraded })}</span>
          )}
        </div>
      )}
      {error && <div role="alert" className="text-sm text-destructive">{error}</div>}

      {data === null ? (
        <LoadingCurve />
      ) : data.loops.length === 0 ? (
        <EmptyState title={t("loops.empty")} />
      ) : (
        <div className="divide-y rounded-lg border">
          {data.loops.map((row) => (
            <div key={row.loop_id} className="flex items-start gap-3 px-3 py-2.5">
              <div className="min-w-0 flex-1 space-y-1">
                <div className="flex flex-wrap items-center gap-2">
                  <span className={`rounded px-1.5 py-0.5 text-[10px] ${STATUS_CLASS[row.status] || "bg-muted text-muted-foreground"}`}>
                    {t(`loops.status.${row.status}`, { defaultValue: row.status })}
                  </span>
                  <span className="truncate font-mono text-xs">{row.loop_id}</span>
                  <span className="text-[10px] text-muted-foreground">{row.loop_type}</span>
                </div>
                <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-muted-foreground">
                  <span className="inline-flex items-center gap-1">
                    <Timer size={11} />
                    {t("loops.interval", { seconds: row.interval })}
                  </span>
                  <span>{t("loops.lastRun", { at: relativeTime(row.last_run, t) })}</span>
                  <span>{t("loops.iterations", { count: row.iteration_count })}</span>
                  {row.restart_count > 0 && (
                    <span>{t("loops.restarts", { count: row.restart_count })}</span>
                  )}
                  {row.error_count > 0 && (
                    <span className="text-warning">{t("loops.errors", { count: row.error_count })}</span>
                  )}
                </div>
                {row.last_error && (
                  <div className="truncate text-[10px] text-destructive" title={row.last_error}>
                    {row.last_error}
                  </div>
                )}
              </div>
              <div className="flex shrink-0 items-center gap-1">
                {row.status !== "running" && (
                  <button
                    type="button"
                    onClick={() => void startLoop(row)}
                    disabled={busyId === row.loop_id}
                    aria-label={t("loops.start")}
                    title={t("loops.start")}
                    className="rounded border px-2 py-1 text-[10px] hover:bg-secondary disabled:opacity-50"
                  >
                    <RefreshCw size={11} className="inline" /> {t("loops.start")}
                  </button>
                )}
                <button
                  type="button"
                  onClick={() => void changeInterval(row)}
                  disabled={busyId === row.loop_id}
                  aria-label={t("loops.setInterval")}
                  title={t("loops.setInterval")}
                  className="rounded border px-2 py-1 text-[10px] hover:bg-secondary disabled:opacity-50"
                >
                  {t("loops.setInterval")}
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
      {promptDialog}
    </div>
  );
}
