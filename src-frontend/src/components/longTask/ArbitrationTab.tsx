import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Scale } from "lucide-react";
import { useApi } from "../../hooks/useApi";
import { emitGlobalToast } from "../common/toast-context";
import { Button } from "../ui/button";

interface PendingArbitration {
  task_id: string;
  pending_arbitration: boolean;
  current_generation?: number;
  goal_summary?: string;
  last_escalation?: {
    verdict?: string;
    issues?: string[];
    ts?: number;
  };
}

type Decision = "adjust" | "reanchor" | "dismiss" | "abort";

const DECISIONS: Decision[] = ["adjust", "reanchor", "dismiss", "abort"];

function shortId(id: string): string {
  return id.length > 8 ? `${id.slice(0, 8)}…` : id;
}

/**
 * 长任务「待仲裁」页签。
 *
 * supervise_round 发现 supervisor/worker 偏离时会置 pending_arbitration 并广播
 * long_task_supervisor_escalation；只有 POST /tasks/{id}/arbitration 能真正消费它，
 * 而这个端点在桌面端一直没有入口——用户收到"监督升级"提示后无处可点。
 *
 * decision 语义（与后端一致）：
 *   adjust   把 directive 作为纠偏约束广播回运行节点
 *   reanchor 用 directive 重新锚定目标
 *   dismiss  只清除待仲裁标志
 *   abort    置任务 interrupted 并停止执行
 */
export function ArbitrationTab() {
  const { t } = useTranslation();
  const { api } = useApi();
  const [rows, setRows] = useState<PendingArbitration[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [drafts, setDrafts] = useState<Record<string, { decision: Decision; directive: string }>>({});
  const [busyId, setBusyId] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const r = await api<{ pending: PendingArbitration[] }>("/api/v1/tasks/arbitrations/pending");
      setRows(r.pending || []);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setRows([]);
    }
  }, [api]);

  useEffect(() => {
    void load();
  }, [load]);

  function draftFor(taskId: string) {
    return drafts[taskId] || { decision: "adjust" as Decision, directive: "" };
  }

  function updateDraft(taskId: string, patch: Partial<{ decision: Decision; directive: string }>) {
    setDrafts((prev) => ({ ...prev, [taskId]: { ...draftFor(taskId), ...patch } }));
  }

  async function submit(taskId: string) {
    const draft = draftFor(taskId);
    if ((draft.decision === "adjust" || draft.decision === "reanchor") && !draft.directive.trim()) {
      emitGlobalToast(t("supervisor.arbitrationDirectiveRequired"), "error");
      return;
    }
    setBusyId(taskId);
    try {
      await api(`/api/v1/tasks/${taskId}/arbitration`, {
        method: "POST",
        body: JSON.stringify({
          decision: draft.decision,
          directive: draft.directive.trim() || null,
        }),
      });
      emitGlobalToast(t("supervisor.arbitrationResolvedToast", { decision: draft.decision }), "success");
      setDrafts((prev) => {
        const next = { ...prev };
        delete next[taskId];
        return next;
      });
      await load();
    } catch (e) {
      emitGlobalToast(
        `${t("supervisor.arbitrationFailed")}: ${e instanceof Error ? e.message : String(e)}`,
        "error",
      );
    } finally {
      setBusyId(null);
    }
  }

  if (rows === null) return null;
  if (error) return <div className="text-xs text-destructive">{error}</div>;
  if (rows.length === 0) {
    return <div className="py-6 text-center text-xs text-muted-foreground">{t("supervisor.arbitrationEmpty")}</div>;
  }

  return (
    <div className="space-y-3">
      {rows.map((row) => {
        const draft = draftFor(row.task_id);
        const escalation = row.last_escalation || {};
        return (
          <div key={row.task_id} className="space-y-2 rounded border p-2 text-xs">
            <div className="flex flex-wrap items-center gap-2">
              <Scale size={12} className="text-warning" />
              <span className="font-mono">{shortId(row.task_id)}</span>
              {typeof row.current_generation === "number" && (
                <span className="text-muted-foreground">gen {row.current_generation}</span>
              )}
              {escalation.verdict && (
                <span className="text-warning">{escalation.verdict}</span>
              )}
            </div>
            {row.goal_summary && (
              <div className="line-clamp-2 text-[11px] text-muted-foreground">{row.goal_summary}</div>
            )}
            {escalation.issues && escalation.issues.length > 0 && (
              <ul className="list-inside list-disc text-[10px] text-muted-foreground">
                {escalation.issues.slice(0, 3).map((issue) => <li key={issue}>{issue}</li>)}
              </ul>
            )}
            <div className="flex flex-wrap items-center gap-2">
              <select
                value={draft.decision}
                onChange={(e) => updateDraft(row.task_id, { decision: e.target.value as Decision })}
                aria-label={t("supervisor.arbitrationDecision")}
                className="rounded border bg-background px-2 py-1 text-[11px]"
              >
                {DECISIONS.map((d) => (
                  <option key={d} value={d}>{t(`supervisor.arbitration.${d}`)}</option>
                ))}
              </select>
              <input
                value={draft.directive}
                onChange={(e) => updateDraft(row.task_id, { directive: e.target.value })}
                placeholder={t("supervisor.arbitrationDirective")}
                className="min-w-40 flex-1 rounded border bg-background px-2 py-1 text-[11px]"
              />
              <Button size="sm" disabled={busyId === row.task_id} onClick={() => void submit(row.task_id)}>
                {t("supervisor.arbitrationSubmit")}
              </Button>
            </div>
          </div>
        );
      })}
    </div>
  );
}
