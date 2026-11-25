/**
 * 模型请求日志面板（借鉴 deepseek-harness 的 request/header + assistant/attempt）
 *
 * 数据来源：GET /api/v1/tasks/{taskId}/llm-requests
 * 只展示摘要（provider/model/历史条数/工具数/状态/耗时/token），不展示提示词正文。
 */
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { useApi } from "../hooks/useApi";

interface RequestLogItem {
  id: number;
  event: "llm_request" | "llm_attempt" | string;
  created_at: number;
  data: Record<string, unknown>;
}

function num(value: unknown, fallback = 0): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

export function RequestLogPanel({ taskId }: { taskId: string }) {
  const { t } = useTranslation();
  const { api } = useApi();
  const [items, setItems] = useState<RequestLogItem[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    api<{ items?: RequestLogItem[] }>(`/api/v1/tasks/${taskId}/llm-requests`)
      .then((res) => {
        if (!cancelled) setItems(Array.isArray(res?.items) ? res.items : []);
      })
      .catch((e) => {
        // 日志面板是诊断辅助：读取失败不回退成错误页，只显示空态
        console.warn("RequestLogPanel: load failed:", e);
        if (!cancelled) setItems([]);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => { cancelled = true; };
  }, [taskId, api]);

  if (loading) {
    return <p className="text-xs text-muted-foreground p-2">{t("taskDetail.loading")}</p>;
  }
  if (items.length === 0) {
    return <p className="text-xs text-muted-foreground p-2">{t("cost.requestEmpty")}</p>;
  }

  return (
    <div className="border rounded-md overflow-hidden">
      <div className="px-2 py-1.5 bg-muted/50 text-xs font-medium flex items-center justify-between gap-2">
        <span>{t("cost.requestLog")}</span>
        <span className="text-[10px] text-muted-foreground font-normal truncate">
          {t("cost.requestLogHint")}
        </span>
      </div>
      <ul className="divide-y text-xs">
        {items.map((it) => {
          const d = it.data || {};
          const isAttempt = it.event === "llm_attempt";
          const provider = String(d.provider || "");
          const model = String(d.model || "");
          const status = String(d.status || "");
          const failed = isAttempt && status !== "ok";
          return (
            <li key={`${it.event}-${it.id}`} className="px-2 py-1.5 flex items-start gap-2">
              <span className={`mt-0.5 text-[9px] px-1 rounded shrink-0 ${
                isAttempt
                  ? (failed ? "bg-destructive/10 text-destructive" : "bg-success/10 text-success")
                  : "bg-primary/10 text-primary"
              }`}>
                {isAttempt ? (failed ? t("cost.attemptError") : t("cost.attemptOk")) : t("cost.request")}
              </span>
              <div className="min-w-0 flex-1">
                <div className="font-mono truncate" title={`${provider} / ${model}`}>
                  {provider || "?"} / {model || "?"}
                </div>
                {isAttempt ? (
                  <div className="text-[10px] text-muted-foreground">
                    {t("cost.attemptMeta", {
                      chunks: num(d.chunks),
                      duration: num(d.duration_ms),
                      tokens: num((d.usage as Record<string, unknown> | undefined)?.total_tokens),
                    })}
                    {typeof d.ttft_ms === "number" ? ` · ${t("cost.ttft")}: ${d.ttft_ms}ms` : ""}
                    {d.error ? <span className="text-destructive"> · {String(d.error)}</span> : null}
                  </div>
                ) : (
                  <div className="text-[10px] text-muted-foreground">
                    {t("cost.requestMeta", {
                      history: num(d.history_messages),
                      tools: num(d.tool_count),
                    })}
                  </div>
                )}
              </div>
              <span className="text-[9px] text-muted-foreground shrink-0">#{it.id}</span>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
