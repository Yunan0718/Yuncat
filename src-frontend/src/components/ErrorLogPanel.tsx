import { Fragment, useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { RefreshCw, Trash2, ChevronDown, ChevronRight, AlertCircle } from "lucide-react";
import { useAppStore } from "../store/appStore";
import { useToast } from "../hooks/use-toast";
import { Button } from "./ui/button";
import { cn } from "@/lib/utils";

/**
 * Batch 8.6: 本地错误事件查看面板——读取 /api/v1/errors，展示所有 HTTP 错误响应。
 *
 * 对标 VS Code Problems panel / Sentry issue list（本地化版本，不上报外部）。
 *
 * 功能:
 * - 启动时自动加载最近 50 条
 * - level 过滤（all / 4xx / 5xx）
 * - 手动刷新 + 自动刷新开关（每 5s 增量拉取）
 * - 清空按钮（需确认）
 * - 表格列: 时间 | 状态码 | 错误码 | 路径 | 消息
 * - 5xx 行可展开看 stack_trace
 *
 * 设计: 与 AuditLogPanel 模式一致；走 token_middleware 鉴权。
 */
export interface ErrorEvent {
  id: number;
  error_code: string;
  status_code: number;
  message: string;
  path: string;
  method: string;
  trace_id: string;
  task_id: string;
  agent_id: string;
  stack_trace: string;
  created_at: number;
}

const AUTO_REFRESH_INTERVAL_MS = 5000;
const DEFAULT_LIMIT = 50;

function formatTime(ts: number): string {
  const d = new Date(ts * 1000);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

function statusBadgeClass(status: number): string {
  if (status >= 500) return "bg-destructive/15 text-destructive border-destructive/30";
  if (status >= 400) return "bg-amber-500/15 text-amber-600 dark:text-amber-400 border-amber-500/30";
  return "bg-muted text-muted-foreground border-border";
}

export function ErrorLogPanel() {
  const { t } = useTranslation();
  const { toast } = useToast();
  const port = useAppStore((s) => s.port);
  const token = useAppStore((s) => s.token);

  const [errors, setErrors] = useState<ErrorEvent[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [levelFilter, setLevelFilter] = useState<"all" | "4xx" | "5xx">("all");
  const [autoRefresh, setAutoRefresh] = useState(false);
  const [expandedIds, setExpandedIds] = useState<Set<number>>(new Set());
  const lastServerTsRef = useRef<number>(0);

  const fc = useCallback(
    (path: string, opts?: RequestInit) =>
      fetch(`http://127.0.0.1:${port}${path}`, {
        ...opts,
        headers: { Authorization: `Bearer ${token}`, ...opts?.headers },
      }),
    [port, token],
  );

  const loadErrors = useCallback(
    async (incremental = false) => {
      setLoading(true);
      setError(null);
      try {
        const params = new URLSearchParams({ limit: String(DEFAULT_LIMIT) });
        if (levelFilter !== "all") params.set("level", levelFilter);
        if (incremental && lastServerTsRef.current > 0) {
          params.set("since_ts", String(lastServerTsRef.current));
        }
        const r = await fc(`/api/v1/errors?${params.toString()}`);
        const data = await r.json().catch(() => ({}));
        if (data.error && data.error !== "db_not_initialized") {
          setError(String(data.error));
          return;
        }
        const newErrors: ErrorEvent[] = data.errors || [];
        if (data.server_ts) lastServerTsRef.current = data.server_ts;
        if (data.total !== undefined) setTotal(data.total);
        if (incremental) {
          setErrors((prev) => {
            if (prev.length === 0) return newErrors;
            const existingIds = new Set(prev.map((e) => e.id));
            const fresh = newErrors.filter((e) => !existingIds.has(e.id));
            return [...fresh, ...prev].slice(0, DEFAULT_LIMIT);
          });
        } else {
          setErrors(newErrors);
        }
      } catch (e) {
        setError(String(e));
      } finally {
        setLoading(false);
      }
    },
    [fc, levelFilter],
  );

  useEffect(() => {
    loadErrors();
  }, [loadErrors]);

  useEffect(() => {
    if (!autoRefresh) return;
    const id = setInterval(() => loadErrors(true), AUTO_REFRESH_INTERVAL_MS);
    return () => clearInterval(id);
  }, [autoRefresh, loadErrors]);

  const handleClear = async () => {
    if (!window.confirm(t("errorLog.confirmClear"))) return;
    try {
      const r = await fc(`/api/v1/errors?confirm=true`, { method: "DELETE" });
      const data = await r.json().catch(() => ({}));
      if (r.ok) {
        toast(t("errorLog.cleared", { count: data.deleted || 0 }));
        setErrors([]);
        setTotal(0);
      } else {
        toast(t("errorLog.clearFailed"), "error");
      }
    } catch {
      toast(t("errorLog.clearFailed"), "error");
    }
  };

  const toggleExpand = (id: number) => {
    setExpandedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  return (
    <div className="space-y-3">
      {/* ── 工具栏 ── */}
      <div className="flex items-center gap-2 flex-wrap">
        <select
          value={levelFilter}
          onChange={(e) => setLevelFilter(e.target.value as "all" | "4xx" | "5xx")}
          className="h-8 rounded border bg-background px-2 text-xs"
        >
          <option value="all">{t("errorLog.levelAll")}</option>
          <option value="4xx">{t("errorLog.level4xx")}</option>
          <option value="5xx">{t("errorLog.level5xx")}</option>
        </select>
        <Button variant="outline" size="sm" onClick={() => loadErrors()} disabled={loading}>
          <RefreshCw size={14} className={cn("mr-1.5", loading && "animate-spin")} />
          {t("errorLog.refresh")}
        </Button>
        <label className="flex items-center gap-1.5 text-xs text-muted-foreground cursor-pointer">
          <input
            type="checkbox"
            checked={autoRefresh}
            onChange={(e) => setAutoRefresh(e.target.checked)}
            className="rounded"
          />
          {t("errorLog.autoRefresh")}
        </label>
        <div className="flex-1" />
        <span className="text-xs text-muted-foreground">
          {t("errorLog.totalCount", { count: total })}
        </span>
        <Button
          variant="outline"
          size="sm"
          onClick={handleClear}
          disabled={errors.length === 0}
          className="text-destructive hover:bg-destructive/10"
        >
          <Trash2 size={14} className="mr-1.5" />
          {t("errorLog.clear")}
        </Button>
      </div>

      {error && (
        <div className="text-xs text-destructive flex items-center gap-1.5">
          <AlertCircle size={14} />
          {error}
        </div>
      )}

      {/* ── 错误列表 ── */}
      {errors.length === 0 && !loading ? (
        <div className="text-sm text-muted-foreground py-12 text-center">
          {t("errorLog.empty")}
        </div>
      ) : (
        <div className="border rounded-md overflow-hidden">
          <table className="w-full text-xs">
            <thead className="bg-muted/50 text-muted-foreground">
              <tr>
                <th className="text-left px-2 py-1.5 font-medium w-32">{t("errorLog.colTime")}</th>
                <th className="text-left px-2 py-1.5 font-medium w-16">{t("errorLog.colStatus")}</th>
                <th className="text-left px-2 py-1.5 font-medium w-32">{t("errorLog.colCode")}</th>
                <th className="text-left px-2 py-1.5 font-medium w-32">{t("errorLog.colMethod")}</th>
                <th className="text-left px-2 py-1.5 font-medium">{t("errorLog.colPath")}</th>
                <th className="text-left px-2 py-1.5 font-medium w-8"></th>
              </tr>
            </thead>
            <tbody>
              {errors.map((e) => {
                const expanded = expandedIds.has(e.id);
                const hasStack = e.status_code >= 500 && e.stack_trace;
                return (
                  <Fragment key={e.id}>
                    <tr
                      className="border-t hover:bg-muted/30 cursor-pointer"
                      onClick={() => hasStack && toggleExpand(e.id)}
                    >
                      <td className="px-2 py-1.5 text-muted-foreground font-mono">
                        {formatTime(e.created_at)}
                      </td>
                      <td className="px-2 py-1.5">
                        <span
                          className={cn(
                            "inline-block px-1.5 py-0.5 rounded border text-[10px] font-mono",
                            statusBadgeClass(e.status_code),
                          )}
                        >
                          {e.status_code}
                        </span>
                      </td>
                      <td className="px-2 py-1.5 font-mono text-[11px]">{e.error_code}</td>
                      <td className="px-2 py-1.5 font-mono text-[11px]">{e.method}</td>
                      <td className="px-2 py-1.5 font-mono text-[11px] truncate max-w-xs" title={e.path}>
                        {e.path}
                      </td>
                      <td className="px-2 py-1.5">
                        {hasStack ? (
                          expanded ? (
                            <ChevronDown size={12} className="text-muted-foreground" />
                          ) : (
                            <ChevronRight size={12} className="text-muted-foreground" />
                          )
                        ) : null}
                      </td>
                    </tr>
                    {expanded && hasStack && (
                      <tr key={`${e.id}-stack`} className="border-t bg-muted/20">
                        <td colSpan={6} className="px-3 py-2">
                          <div className="text-[11px] text-muted-foreground mb-1">
                            {t("errorLog.message")}: {e.message}
                          </div>
                          <pre className="text-[10px] font-mono whitespace-pre-wrap break-all max-h-48 overflow-y-auto bg-background border rounded p-2">
                            {e.stack_trace}
                          </pre>
                        </td>
                      </tr>
                    )}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
