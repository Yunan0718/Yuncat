/**
 * 任务级 LLM 用量面板
 *
 * 在 TaskDetail 的 "用量" tab 中渲染：
 * - 4 张聚合卡片（调用次数/总延迟/总 Token/缓存命中）
 * - Token 细分（输入/输出/推理）
 * - 明细表（轮次/模型/Agent/Token/延迟）
 *
 * 2026-09-10 去价格化：**只统计 token 消耗，不再统计价格**。
 * - 调研结论：厂商没有"逐次用量查询"API（只有余额/配额窗口），token 统计的主流做法
 *   是本地账本——累计每次响应自带的 usage（详见 src-python/services/llm_usage.py）；
 * - 旧成本计算口径从未与定价 YAML 对上（恒为 $0），没有保留价值；
 * - cached_tokens / reasoning_tokens 是"看不见但真实消耗"的部分，必须单独展示。
 * 数据源：analytics_events 中 event_type='token_usage'（兼容旧 token_cost）。
 */
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { useApi } from "../hooks/useApi";
import { EmptyState } from "./common/EmptyState";
import { RequestLogPanel } from "./RequestLogPanel";

interface LlmSummary {
  call_count: number;
  total_duration_ms: number;
  total_tokens: number;
  total_prompt_tokens: number;
  total_completion_tokens: number;
  total_cached_tokens?: number;
  total_reasoning_tokens?: number;
}

interface LlmMetric {
  round: number;
  model: string;
  agent_id: string;
  duration_ms: number;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  cached_tokens?: number;
  reasoning_tokens?: number;
}

const fmtNum = (n: number | undefined): string => (n ?? 0).toLocaleString("en-US");

export function CostPanel({ taskId }: { taskId: string }) {
  const { t } = useTranslation();
  const { api } = useApi();
  const [summary, setSummary] = useState<LlmSummary | null>(null);
  const [metrics, setMetrics] = useState<LlmMetric[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);

  useEffect(() => {
    // Batch E M24: 添加 cancelled 标志——组件 unmount 后不再 setState（防止 React 警告）
    let cancelled = false;
    setLoading(true);
    setError(false);
    Promise.all([
      api<LlmSummary>(`/api/v1/analytics/tasks/${taskId}/llm-summary`),
      api<{ metrics: LlmMetric[] }>(`/api/v1/analytics/tasks/${taskId}/llm-metrics`),
    ])
      .then(([s, m]) => {
        if (cancelled) return;
        setSummary(s);
        setMetrics(m.metrics || []);
      })
      .catch((e) => {
        if (cancelled) return;
        console.warn("CostPanel: load failed:", e);
        setError(true);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => { cancelled = true; };
  }, [taskId, api]);

  if (loading) {
    return <p className="text-sm text-muted-foreground p-2">{t("taskDetail.loading")}</p>;
  }
  if (error || !summary) {
    return <p className="text-sm text-destructive p-2">{t("cost.loadFailed")}</p>;
  }
  if (summary.call_count === 0) {
    return (
      <div className="space-y-3">
        <EmptyState compact title={t("cost.empty")} />
        <RequestLogPanel taskId={taskId} />
      </div>
    );
  }

  const cards = [
    { label: t("cost.callCount"), value: String(summary.call_count) },
    { label: t("cost.totalDuration"), value: `${summary.total_duration_ms}ms` },
    { label: t("cost.totalTokens"), value: fmtNum(summary.total_tokens) },
    { label: t("cost.cachedTokens"), value: fmtNum(summary.total_cached_tokens) },
  ];

  return (
    <div className="space-y-3 text-sm">
      {/* 聚合卡片 */}
      <div className="grid grid-cols-2 gap-2">
        {cards.map((c) => (
          <div key={c.label} className="bg-secondary rounded p-2">
            <div className="text-xs text-muted-foreground">{c.label}</div>
            <div className="font-semibold text-lg tabular-nums">{c.value}</div>
          </div>
        ))}
      </div>

      {/* Token 细分：缓存命中与推理 token 是"看不见但真实消耗"的部分 */}
      <div className="grid grid-cols-3 gap-2 text-xs">
        <div className="bg-secondary rounded p-2">
          <span className="text-muted-foreground">{t("cost.promptTokens")}: </span>
          <span className="font-semibold tabular-nums">{fmtNum(summary.total_prompt_tokens)}</span>
        </div>
        <div className="bg-secondary rounded p-2">
          <span className="text-muted-foreground">{t("cost.completionTokens")}: </span>
          <span className="font-semibold tabular-nums">{fmtNum(summary.total_completion_tokens)}</span>
        </div>
        <div className="bg-secondary rounded p-2">
          <span className="text-muted-foreground">{t("cost.reasoningTokens")}: </span>
          <span className="font-semibold tabular-nums">{fmtNum(summary.total_reasoning_tokens)}</span>
        </div>
      </div>

      {/* 明细表 */}
      <div className="border rounded-md overflow-hidden">
        <table className="w-full text-xs">
          <thead className="bg-muted/50 text-muted-foreground">
            <tr>
              <th className="text-left px-2 py-1.5 font-medium">{t("cost.colRound")}</th>
              <th className="text-left px-2 py-1.5 font-medium">{t("cost.colModel")}</th>
              <th className="text-left px-2 py-1.5 font-medium">{t("cost.colAgent")}</th>
              <th className="text-right px-2 py-1.5 font-medium">{t("cost.colTokens")}</th>
              <th className="text-right px-2 py-1.5 font-medium">{t("cost.colCached")}</th>
              <th className="text-right px-2 py-1.5 font-medium">{t("cost.colDuration")}</th>
            </tr>
          </thead>
          <tbody>
            {metrics.map((m, i) => (
              <tr key={i} className="border-t">
                <td className="px-2 py-1.5 font-mono">{m.round}</td>
                <td className="px-2 py-1.5 font-mono truncate max-w-[120px]" title={m.model}>{m.model}</td>
                <td className="px-2 py-1.5 font-mono">{m.agent_id}</td>
                <td className="px-2 py-1.5 font-mono text-right tabular-nums">{fmtNum(m.total_tokens)}</td>
                <td className="px-2 py-1.5 font-mono text-right tabular-nums">{fmtNum(m.cached_tokens)}</td>
                <td className="px-2 py-1.5 font-mono text-right">{m.duration_ms}ms</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <RequestLogPanel taskId={taskId} />
    </div>
  );
}
