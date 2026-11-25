import { useEffect, useState } from "react";
import { useApi } from "../../hooks/useApi";
import { useTranslation } from "react-i18next";

interface WorkflowNodeResult {
  id: string;
  type: string;
  prompt: string;
  output?: string;
  error?: string;
  start_time?: number;
  end_time?: number;
}

interface RunDetail {
  id: string;
  workflow_id: string;
  status: string;
  node_results: Record<string, WorkflowNodeResult>;
  started_at: number;
  finished_at: number | null;
}

/**
 * Phase 18: 工作流执行时间线——含失败节点重试/跳过按钮
 *
 * 垂直时间线布局，每个节点一张卡片。
 * 显示名称、类型、时间、耗时、状态、输入/输出（可折叠）、错误、操作按钮。
 */
export function WorkflowTimeline({ runId, workflowId }: { runId: string; workflowId?: string }) {
  const { t } = useTranslation();
  const { api } = useApi();
  const [run, setRun] = useState<RunDetail | null>(null);
  const [expandedNodes, setExpandedNodes] = useState<Set<string>>(new Set());
  const [actionMsg, setActionMsg] = useState("");

  useEffect(() => {
    api<RunDetail>(`/api/v1/workflows/runs/${runId}`)
      .then(setRun)
      .catch((e) => console.warn("WorkflowTimeline: run detail load failed:", e));
  }, [runId, api]);

  if (!run) return <p className="text-xs text-muted-foreground">{t("legacy.git_26b5")}</p>;

  const nodes = Object.values(run.node_results || {});

  const toggleExpand = (id: string) => {
    setExpandedNodes((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  // Phase 18: 重试/跳过失败节点
  const resumeNode = async (action: "retry" | "skip", nodeId: string) => {
    if (!workflowId) {
      setActionMsg(t("workflowTimeline.missingId"));
      return;
    }
    setActionMsg(action === "retry" ? t("workflowTimeline.retrying") : t("workflowTimeline.skipping"));
    try {
      const overrides = action === "skip" ? { [nodeId]: {} } : undefined;
      await api(`/api/v1/workflows/${workflowId}/resume`, {
        method: "POST",
        body: JSON.stringify({
          run_id: runId,
          action: action === "skip" ? "skip" : "retry",
          node_overrides: overrides,
        }),
      });
      setActionMsg(action === "retry" ? t("workflowTimeline.okRetry") : t("workflowTimeline.okSkip"));
      // 重新加载运行状态
      const updated = await api<RunDetail>(`/api/v1/workflows/runs/${runId}`);
      setRun(updated);
    } catch {
      setActionMsg(action === "retry" ? t("workflowTimeline.errRetry") : t("workflowTimeline.errSkip"));
    }
  };

  const statusColor = (n: WorkflowNodeResult) => {
    if (n.error) return "border-l-destructive";
    if (n.output) return "border-l-success";
    return "border-l-warning";
  };

  const statusLabel = (n: WorkflowNodeResult) => {
    if (n.error) return t("workflowTimeline.statusFailed");
    if (n.output) return t("workflowTimeline.statusDone");
    return t("workflowTimeline.statusRunning");
  };

  // 运行级控制：暂停 / 继续 / 取消——后端早就有了（且带原子 CAS），前端此前没有入口，
  // 长任务一旦跑偏就只能干等。
  const controlRun = async (action: "pause" | "resume" | "cancel") => {
    const cap = action.charAt(0).toUpperCase() + action.slice(1);
    setActionMsg("");
    try {
      await api(`/api/v1/workflows/runs/${runId}/${action}`, { method: "POST" });
      setActionMsg(t(`workflowTimeline.ok${cap}`));
      const updated = await api<RunDetail>(`/api/v1/workflows/runs/${runId}`);
      setRun(updated);
    } catch {
      setActionMsg(t(`workflowTimeline.err${cap}`));
    }
  };

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
        <span>
          {t("workflowTimeline.status")}: <span className="font-semibold">{run.status}</span>
          {run.finished_at && <span> · {t("workflowTimeline.totalElapsed", { sec: (run.finished_at - run.started_at).toFixed(1) })}</span>}
        </span>
        {run.status === "running" && (
          <>
            <button
              type="button"
              onClick={() => void controlRun("pause")}
              className="rounded border border-input px-1.5 py-0.5 text-[10px] hover:bg-secondary"
            >
              {t("workflowTimeline.pause")}
            </button>
            <button
              type="button"
              onClick={() => void controlRun("cancel")}
              className="rounded border border-destructive/50 px-1.5 py-0.5 text-[10px] text-destructive hover:bg-destructive/10"
            >
              {t("workflowTimeline.cancel")}
            </button>
          </>
        )}
        {run.status === "paused" && (
          <button
            type="button"
            onClick={() => void controlRun("resume")}
            className="rounded border border-input px-1.5 py-0.5 text-[10px] hover:bg-secondary"
          >
            {t("workflowTimeline.resume")}
          </button>
        )}
      </div>
      {actionMsg && (
        <div
          className={`text-[10px] px-2 py-1 rounded ${actionMsg.startsWith("OK") ? "text-success bg-success/10" : actionMsg.startsWith("ERR") ? "text-destructive bg-destructive/10" : "text-muted-foreground bg-secondary"}`}
        >
          {actionMsg}
        </div>
      )}
      <div className="relative pl-4 border-l-2 border-muted space-y-3">
        {nodes.map((n) => (
          <div key={n.id} className={`relative -left-[19px] pl-6 border-l-2 ${statusColor(n)}`}>
            {/* 时间线圆点 */}
            <div
              className="absolute -left-[7px] top-1.5 w-3 h-3 rounded-full border-2 bg-background"
              style={{ borderColor: n.error ? "var(--destructive)" : n.output ? "var(--success)" : "var(--warning)" }}
            />
            {/* 卡片 */}
            <div className="border rounded p-3 text-xs space-y-1.5">
              <div className="flex items-center justify-between">
                <span className="font-semibold">{n.id}</span>
                <span className="text-[10px] bg-secondary px-1.5 py-0.5 rounded">{n.type}</span>
              </div>
              <p className="text-muted-foreground">{n.prompt?.slice(0, 80)}</p>
              <div className="flex items-center gap-3 text-[10px] text-muted-foreground">
                <span>{statusLabel(n)}</span>
              </div>
              {n.error && <p className="text-destructive text-[10px]">{n.error}</p>}

              {/* Phase 18: 失败节点操作按钮 */}
              {n.error && workflowId && (
                <div className="flex gap-1 pt-1 border-t">
                  <button
                    onClick={() => resumeNode("retry", n.id)}
                    className="text-[10px] px-2 py-0.5 bg-primary text-primary-foreground rounded font-semibold"
                  >
                    {t("workflowTimeline.retry")}
                  </button>
                  <button
                    onClick={() => resumeNode("skip", n.id)}
                    className="text-[10px] px-2 py-0.5 border rounded hover:bg-secondary"
                  >
                    ⏭ {t("workflowTimeline.skip")}
                  </button>
                </div>
              )}

              {/* 可折叠详情 */}
              <button onClick={() => toggleExpand(n.id)} className="text-[10px] text-primary hover:underline">
                {expandedNodes.has(n.id) ? t("workflowTimeline.collapse") : t("workflowTimeline.expand")} {t("workflowTimeline.details")}
              </button>
              {expandedNodes.has(n.id) && (
                <div className="space-y-1.5 pt-1 border-t">
                  {n.output && (
                    <div>
                      <span className="text-[10px] font-semibold text-muted-foreground">{t("workflowTimeline.output")}:</span>
                      <pre className="bg-secondary rounded p-1.5 text-[10px] max-h-32 overflow-y-auto whitespace-pre-wrap">
                        {n.output.slice(0, 500)}
                      </pre>
                    </div>
                  )}
                  <div>
                    <span className="text-[10px] font-semibold text-muted-foreground">Prompt:</span>
                    <pre className="bg-secondary rounded p-1.5 text-[10px] max-h-20 overflow-y-auto whitespace-pre-wrap">
                      {n.prompt}
                    </pre>
                  </div>
                </div>
              )}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
