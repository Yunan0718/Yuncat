import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { RefreshCw, Shield, CheckCircle2, XCircle, Circle, Filter } from "lucide-react";
import { useAppStore } from "../store/appStore";
import { useToast } from "../hooks/use-toast";
import { Button } from "./ui/button";
import { cn } from "@/lib/utils";

/**
 * H4: 审计日志查看面板——读取 /api/v1/audit-log，展示所有副作用工具调用记录。
 *
 * 对标 VS Code Output panel / Cursor activity log / Claude Code audit trail。
 *
 * 功能:
 * - 启动时自动加载最近 100 条
 * - tool_name 过滤输入框——按工具名精确过滤
 * - 手动刷新按钮 + 自动刷新开关（每 5s 增量拉取 since_ts 之后的新记录）
 * - 表格列: 时间 | Agent | 工具 | 阶段 | 结果 | 参数摘要
 * - 阶段徽章: preview=灰 / execute=蓝 / blocked=红
 * - 结果图标: success=绿✓ / failure=红✗ / pending=灰○
 *
 * 设计: 后端 audit_log 表是 append-only（防篡改），前端只读。
 */
export interface AuditLogEntry {
  id: number;
  ts: number;
  agent_id: string;
  tool_name: string;
  args_summary: string | null;
  success: number | null; // 0/1/null
  phase: string | null;
  session_id: string | null;
  task_id: string | null;
}

const AUTO_REFRESH_INTERVAL_MS = 5000;
const DEFAULT_LIMIT = 100;

function formatTime(ts: number): string {
  const d = new Date(ts * 1000);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

function phaseBadgeClass(phase: string | null): string {
  if (!phase) return "bg-muted text-muted-foreground border-border";
  if (phase === "blocked") return "bg-destructive/15 text-destructive border-destructive/30";
  if (phase === "execute") return "bg-primary/15 text-primary border-primary/30";
  return "bg-muted text-muted-foreground border-border"; // preview
}

export function AuditLogPanel() {
  const { t } = useTranslation();
  const { toast } = useToast();
  const port = useAppStore((s) => s.port);
  const token = useAppStore((s) => s.token);

  const [logs, setLogs] = useState<AuditLogEntry[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [toolNameFilter, setToolNameFilter] = useState("");
  const [autoRefresh, setAutoRefresh] = useState(false);
  const lastServerTsRef = useRef<number>(0);
  const containerRef = useRef<HTMLDivElement>(null);

  const fc = useCallback(
    (path: string, opts?: RequestInit) =>
      fetch(`http://127.0.0.1:${port}${path}`, {
        ...opts,
        headers: { Authorization: `Bearer ${token}`, ...opts?.headers },
      }),
    [port, token],
  );

  const loadLogs = useCallback(
    async (incremental = false) => {
      setLoading(true);
      setError(null);
      try {
        const params = new URLSearchParams({ limit: String(DEFAULT_LIMIT) });
        if (toolNameFilter) params.set("tool_name", toolNameFilter);
        if (incremental && lastServerTsRef.current > 0) {
          params.set("since_ts", String(lastServerTsRef.current));
        }
        const r = await fc(`/api/v1/audit-log?${params.toString()}`);
        const data = await r.json().catch(() => ({}));
        if (data.error && data.error !== "db_not_initialized") {
          setError(String(data.error));
          return;
        }
        const newLogs: AuditLogEntry[] = data.logs || [];
        if (data.server_ts) lastServerTsRef.current = data.server_ts;
        if (incremental) {
          // Task 6.6: 用 functional update 读 prev——避免 logs 进 useCallback 依赖导致
          // interval 频繁重建（logs 变 → loadLogs 引用变 → useEffect 重跑 → setInterval 重置）
          setLogs((prev) => {
            if (prev.length === 0) return newLogs;
            // 增量: 合并新日志到顶部，去重，限制总数 ≤ DEFAULT_LIMIT
            const existingIds = new Set(prev.map((l) => l.id));
            const fresh = newLogs.filter((l) => !existingIds.has(l.id));
            return [...fresh, ...prev].slice(0, DEFAULT_LIMIT);
          });
        } else {
          setLogs(newLogs);
        }
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
        toast(t("audit.loadFailed"), "error");
      } finally {
        setLoading(false);
      }
    },
    [fc, toolNameFilter, t, toast],
  );

  // 启动时 + 过滤变化时全量加载
  useEffect(() => {
    loadLogs(false);
  }, [loadLogs]);

  // 自动刷新
  useEffect(() => {
    if (!autoRefresh) return;
    const id = setInterval(() => loadLogs(true), AUTO_REFRESH_INTERVAL_MS);
    return () => clearInterval(id);
  }, [autoRefresh, loadLogs]);

  if (error) {
    return (
      <div className="flex flex-col items-center justify-center h-full text-xs text-destructive gap-2">
        <XCircle size={20} />
        <span>{t("audit.loadFailed")}: {error}</span>
        <Button
          onClick={() => loadLogs(false)}
          variant="link"
          size="xs"
        >
          {t("common.retry")}
        </Button>
      </div>
    );
  }

  if (logs.length === 0 && !loading) {
    return (
      <div className="flex flex-col items-center justify-center h-full text-xs text-muted-foreground gap-2">
        <Shield size={20} className="opacity-40" />
        <span>{t("audit.empty")}</span>
        <Button
          onClick={() => loadLogs(false)}
          variant="link"
          size="xs"
        >
          <RefreshCw size={11} /> {t("common.refresh")}
        </Button>
      </div>
    );
  }

  return (
    <div className="h-full flex flex-col">
      {/* 工具栏 */}
      <div className="flex items-center gap-2 px-2 py-1.5 border-b bg-muted/30 shrink-0">
        <Filter size={11} className="text-muted-foreground shrink-0" />
        <input
          type="text"
          value={toolNameFilter}
          onChange={(e) => setToolNameFilter(e.target.value)}
          placeholder={t("audit.filterPlaceholder")}
          className="flex-1 min-w-0 h-6 px-2 text-[11px] bg-background border rounded font-mono focus:outline-none focus:ring-1 focus:ring-primary"
        />
        <Button
          onClick={() => loadLogs(false)}
          disabled={loading}
          variant="outline"
          size="xs"
          title={t("common.refresh")}
        >
          <RefreshCw size={11} className={loading ? "animate-spin" : ""} />
          <span className="hidden sm:inline">{t("common.refresh")}</span>
        </Button>
        <Button
          onClick={() => setAutoRefresh(!autoRefresh)}
          variant="outline"
          size="xs"
          title={t("audit.autoRefresh")}
          className={cn(autoRefresh && "bg-primary/15 text-primary border-primary/30")}
        >
          <Circle size={8} className={autoRefresh ? "fill-primary text-primary" : ""} />
          <span className="hidden sm:inline">{t("audit.autoRefresh")}</span>
        </Button>
      </div>

      {/* 日志表格 */}
      <div ref={containerRef} className="flex-1 overflow-auto">
        <table className="w-full text-[11px] font-mono">
          <thead className="sticky top-0 bg-muted/80 backdrop-blur">
            <tr className="text-left text-muted-foreground border-b">
              <th className="px-2 py-1 font-medium">{t("audit.colTime")}</th>
              <th className="px-2 py-1 font-medium">{t("audit.colAgent")}</th>
              <th className="px-2 py-1 font-medium">{t("audit.colTool")}</th>
              <th className="px-2 py-1 font-medium">{t("audit.colPhase")}</th>
              <th className="px-2 py-1 font-medium">{t("audit.colResult")}</th>
              <th className="px-2 py-1 font-medium">{t("audit.colArgs")}</th>
            </tr>
          </thead>
          <tbody>
            {logs.map((log) => {
              const isSuccess = log.success === 1;
              const isFailure = log.success === 0;
              const isPending = log.success === null;
              return (
                <tr key={log.id} className="border-b hover:bg-secondary/40 transition-colors">
                  <td className="px-2 py-1 text-muted-foreground whitespace-nowrap">
                    {formatTime(log.ts)}
                  </td>
                  <td className="px-2 py-1 whitespace-nowrap">
                    <span className="text-primary">{log.agent_id}</span>
                  </td>
                  <td className="px-2 py-1 whitespace-nowrap text-foreground font-semibold">
                    {log.tool_name}
                  </td>
                  <td className="px-2 py-1 whitespace-nowrap">
                    {log.phase && (
                      <span className={cn("px-1.5 py-0.5 rounded text-[10px] border", phaseBadgeClass(log.phase))}>
                        {log.phase}
                      </span>
                    )}
                  </td>
                  <td className="px-2 py-1 whitespace-nowrap">
                    {isSuccess && <CheckCircle2 size={12} className="text-success" />}
                    {isFailure && <XCircle size={12} className="text-destructive" />}
                    {isPending && <Circle size={12} className="text-muted-foreground" />}
                  </td>
                  <td className="px-2 py-1 text-muted-foreground max-w-md truncate" title={log.args_summary ?? ""}>
                    {log.args_summary ?? ""}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {/* 状态栏 */}
      <div className="shrink-0 border-t bg-muted/30 px-2 py-1 text-[10px] text-muted-foreground flex items-center justify-between">
        <span>
          {logs.length} {t("audit.entries")} {loading && `· ${t("common.loading")}`}
        </span>
        <span>
          {autoRefresh ? t("audit.autoRefreshOn") : t("audit.autoRefreshOff")}
        </span>
      </div>
    </div>
  );
}
