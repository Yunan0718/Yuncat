import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { useAppStore } from "../store/appStore";
import { useApi } from "../hooks/useApi";
import { agentDisplayName } from "../lib/agentMeta";
import { Card, CardContent, CardHeader, CardTitle } from "../components/ui/card";
import { Skeleton } from "../components/ui/skeleton";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "../components/ui/collapsible";
import { Badge } from "../components/ui/badge";
import { StatusDot } from "../components/common/StatusDot";
import { Separator } from "../components/ui/separator";
import {
  AreaChart,
  Area,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  PieChart,
  Pie,
  Cell,
  Legend,
} from "recharts";
import {
  CheckCircle2,
  BarChart3,
  MessageSquare,
  Layers,
  Server,
  Target,
  TrendingUp,
  Activity,
  ChevronDown,
  ChevronRight,
  Inbox,
  Zap,
} from "lucide-react";
import { EmptyState } from "../components/common/EmptyState";

const COLORS = ["var(--chart-1)", "var(--chart-2)", "var(--chart-3)", "var(--chart-5)", "var(--chart-4)"];

const TOKEN_DAYS_KEY = "yuncat.dashboard.tokenDays";
const CHARTS_OPEN_KEY = "yuncat.dashboard.chartsOpen";

function getInitialTokenDays(): "7" | "30" {
  try {
    const stored = localStorage.getItem(TOKEN_DAYS_KEY);
    if (stored === "7" || stored === "30") return stored;
  } catch {
    /* ignore */
  }
  return "7";
}

function getInitialChartsOpen(): boolean {
  try {
    const stored = localStorage.getItem(CHARTS_OPEN_KEY);
    if (stored === "false") return false;
    if (stored === "true") return true;
  } catch {
    /* ignore */
  }
  return true;
}

interface DashboardSummary {
  total_tasks?: number;
  completed?: number;
  failed?: number;
  running?: number;
  total_messages?: number;
}
interface TokenUsageItem {
  date: string;
  total_tokens: number;
}
/** /analytics/token-usage 返回的是汇总对象（daily 是每日序列），不是数组 */
interface TokenUsageResponse {
  total_tokens?: number;
  total_messages?: number;
  daily?: { day: string; tokens: number }[];
}

function toTokenUsageItems(raw: unknown): TokenUsageItem[] {
  const daily = (raw as TokenUsageResponse | null)?.daily;
  if (!Array.isArray(daily)) return [];
  return daily.map((d) => ({ date: d.day, total_tokens: Number(d.tokens) || 0 }));
}
interface SkillStats {
  total: number;
  enabled: number;
}
interface AgentInfo {
  id?: string;
  name?: string;
  ready?: boolean;
  port?: number;
  tools_count?: number;
  mode?: string;
}
/** 与 /analytics/backend-health 的实际返回对齐：agents 逐个后端给 ready/name/port */
interface BackendHealth {
  agents?: Record<string, AgentInfo>;
}
interface PlanStats {
  total: number;
  completed: number;
  completion_rate: number;
}

function toPlanStats(raw: Record<string, unknown> | undefined): PlanStats {
  return {
    total: Number(raw?.total) || 0,
    completed: Number(raw?.completed) || 0,
    completion_rate: Number(raw?.completion_rate) || 0,
  };
}

export function Dashboard() {
  const { t } = useTranslation();
  const { api } = useApi();
  const navigate = useNavigate();
  const backendReady = useAppStore((s) => s.backendReady);
  const [loading, setLoading] = useState(true);
  const [summary, setSummary] = useState<DashboardSummary>({});
  const [tokenUsage, setTokenUsage] = useState<TokenUsageItem[]>([]);
  const [skillStats, setSkillStats] = useState<SkillStats>({ total: 0, enabled: 0 });
  const [backendHealth, setBackendHealth] = useState<BackendHealth>({});
  const [planStats, setPlanStats] = useState<PlanStats>(toPlanStats(undefined));
  // 2026-10-22：统计接口失败要显式提示——此前全部 .catch(() => null)，
  // "接口挂了"和"真的没有数据"在界面上完全一样（0 token / 0 任务 / 0% 成功率）。
  const [loadError, setLoadError] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);
  // 缓存命中率——后端有账本口径（2026-10-22 修掉了"只取最近 500 条"的静默截断）
  const [cacheStats, setCacheStats] = useState<{ hit_rate_pct: number; total_cache_tokens: number }>({
    hit_rate_pct: 0,
    total_cache_tokens: 0,
  });
  const [tokenDays, setTokenDays] = useState<"7" | "30">(getInitialTokenDays);
  const [chartsOpen, setChartsOpen] = useState(getInitialChartsOpen);
  // 窗口内 token 合计（与图表同一数据源，避免和接口的全量总计口径打架）
  const tokenTotal = tokenUsage.reduce((sum, d) => sum + d.total_tokens, 0);

  useEffect(() => {
    if (!backendReady) return;
    setLoading(true);
    const failed: string[] = [];
    const guarded = async (key: string, url: string) => {
      try {
        return await api(url);
      } catch {
        failed.push(key);
        return null;
      }
    };
    Promise.all([
      guarded("summary", "/api/v1/analytics/summary"),
      guarded("tokenUsage", `/api/v1/analytics/token-usage?days=${tokenDays}`),
      guarded("backendHealth", "/api/v1/analytics/backend-health"),
      guarded("planStats", "/api/v1/analytics/plan-stats"),
      guarded("skills", "/api/v1/skills"),
      guarded("cacheStats", "/api/v1/analytics/cache-stats"),
    ])
      .then(([s, tu, bh, ps, skills, cs]) => {
        if (s) setSummary(s);
        if (tu) setTokenUsage(toTokenUsageItems(tu));
        if (bh) setBackendHealth(bh);
        if (ps) setPlanStats(toPlanStats(ps as Record<string, unknown>));
        if (Array.isArray(skills))
          setSkillStats({
            total: skills.length,
            enabled: skills.filter((x: { enabled?: boolean }) => x.enabled).length,
          });
        if (cs) {
          setCacheStats({
            hit_rate_pct: Number((cs as { hit_rate_pct?: number }).hit_rate_pct) || 0,
            total_cache_tokens: Number((cs as { total_cache_tokens?: number }).total_cache_tokens) || 0,
          });
        }
        setLoadError(failed.length ? failed.join(", ") : null);
        setLoading(false);
      })
      .catch(() => {
        setLoadError("unknown");
        setLoading(false);
      });
  }, [backendReady, tokenDays, api, reloadKey]);

  // Task 6.11: 持久化 Dashboard 图表区间与展开状态
  useEffect(() => {
    try {
      localStorage.setItem(TOKEN_DAYS_KEY, tokenDays);
    } catch {
      /* ignore */
    }
  }, [tokenDays]);

  useEffect(() => {
    try {
      localStorage.setItem(CHARTS_OPEN_KEY, String(chartsOpen));
    } catch {
      /* ignore */
    }
  }, [chartsOpen]);

  if (!backendReady) return <div className="p-8 text-muted-foreground">{t("common.back")}</div>;

  const successRate = summary?.total_tasks ? Math.round(((summary.completed || 0) / summary.total_tasks) * 100) : 0;
  const agents = backendHealth?.agents || {};
  const agentOnline = Object.values(agents).filter((a: AgentInfo) => a.ready).length;
  const running = summary?.running || 0;

  return (
    <div className="h-full overflow-y-auto">
      <div className="page-shell space-y-6">
        {/* Header */}
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-2xl font-bold tracking-tight">{t("dashboard.title")}</h1>
            <p className="text-sm text-muted-foreground mt-1">{t("dashboard.subtitle")}</p>
          </div>
        </div>

        {loadError && (
          <div className="flex items-center justify-between gap-3 rounded-lg border border-destructive/40 bg-destructive/5 px-3 py-2 text-[12px] text-destructive">
            <span>{t("dashboard.statsLoadFailed", { items: loadError })}</span>
            <button
              type="button"
              onClick={() => setReloadKey((k) => k + 1)}
              className="shrink-0 rounded px-2 py-0.5 font-medium hover:bg-destructive/10"
            >
              {t("common.retry")}
            </button>
          </div>
        )}

        {/* KPI Cards — clickable drill-down */}
        <div className="grid grid-cols-3 gap-4">
          <KpiCard
            loading={loading}
            icon={<BarChart3 size={18} />}
            value={summary?.total_tasks || 0}
            label={t("dashboard.totalTasks")}
            color="text-primary"
            onClick={() => navigate("/chat")}
          />
          <KpiCard
            loading={loading}
            icon={<CheckCircle2 size={18} />}
            value={`${successRate}%`}
            label={t("dashboard.successRate")}
            color="text-success"
            onClick={() => navigate("/chat?status=completed")}
          />
          <KpiCard
            loading={loading}
            icon={<MessageSquare size={18} />}
            value={(summary?.total_messages || 0).toLocaleString()}
            label={t("dashboard.messages")}
            color="text-warning"
            onClick={() => navigate("/chat")}
          />
          <KpiCard
            loading={loading}
            icon={<Layers size={18} />}
            value={skillStats.total}
            label={t("dashboard.totalSkills")}
            color="text-[var(--chart-4)]"
            sub={`${skillStats.enabled} ${t("dashboard.enabled")}`}
            onClick={() => navigate("/skills")}
          />
          <KpiCard
            loading={loading}
            icon={<Zap size={18} />}
            value={`${cacheStats.hit_rate_pct}%`}
            label={t("dashboard.cacheHitRate")}
            color="text-[var(--chart-2)]"
            sub={`${cacheStats.total_cache_tokens.toLocaleString()} ${t("dashboard.cacheTokens")}`}
            onClick={() => navigate("/chat")}
          />
          <KpiCard
            loading={loading}
            icon={<Target size={18} />}
            value={running}
            label={t("dashboard.runningNow")}
            color="text-primary"
            onClick={() => navigate("/chat?status=running")}
          />
          <KpiCard
            loading={loading}
            icon={<Server size={18} />}
            value={`${agentOnline}/${Object.keys(agents).length}`}
            label={t("dashboard.agentsActive")}
            color="text-entity-workflow"
            onClick={() => navigate("/agents")}
          />
        </div>

        {/* Charts Section */}
        <Card>
          <Collapsible open={chartsOpen} onOpenChange={setChartsOpen}>
            <CollapsibleTrigger className="w-full">
              <CardHeader className="pb-2 cursor-pointer hover:bg-muted/30 transition-colors rounded-t-xl">
                <div className="flex items-center justify-between">
                  <CardTitle className="text-sm flex items-center gap-2">
                    <TrendingUp size={16} className="text-primary" /> {t("dashboard.tokenUsage")}
                    {tokenTotal > 0 && (
                      <span className="text-xs font-normal text-muted-foreground">
                        {tokenTotal.toLocaleString()}
                      </span>
                    )}
                  </CardTitle>
                  <div className="flex items-center gap-2">
                    <div className="flex gap-1 bg-muted rounded-md p-0.5">
                      <span
                        onClick={(e) => {
                          e.stopPropagation();
                          setTokenDays("7");
                        }}
                        className={`px-2.5 py-1 rounded text-2xs font-medium transition-colors cursor-pointer ${tokenDays === "7" ? "bg-background shadow-sm text-foreground" : "text-muted-foreground hover:text-foreground"}`}
                      >
                        7d
                      </span>
                      <span
                        onClick={(e) => {
                          e.stopPropagation();
                          setTokenDays("30");
                        }}
                        className={`px-2.5 py-1 rounded text-2xs font-medium transition-colors cursor-pointer ${tokenDays === "30" ? "bg-background shadow-sm text-foreground" : "text-muted-foreground hover:text-foreground"}`}
                      >
                        30d
                      </span>
                    </div>
                    {chartsOpen ? (
                      <ChevronDown size={16} className="text-muted-foreground" />
                    ) : (
                      <ChevronRight size={16} className="text-muted-foreground" />
                    )}
                  </div>
                </div>
              </CardHeader>
            </CollapsibleTrigger>
            <CollapsibleContent>
              <CardContent className="pt-0">
                {loading ? (
                  <Skeleton className="h-72" />
                ) : tokenUsage.length > 0 ? (
                  <div className="grid grid-cols-3 gap-6">
                    <div className="col-span-2">
                      <ResponsiveContainer width="100%" height={260}>
                        <AreaChart data={tokenUsage}>
                          <defs>
                            <linearGradient id="tokenGrad" x1="0" y1="0" x2="0" y2="1">
                              <stop offset="5%" stopColor="var(--chart-1)" stopOpacity={0.2} />
                              <stop offset="95%" stopColor="var(--chart-1)" stopOpacity={0} />
                            </linearGradient>
                          </defs>
                          <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
                          <XAxis dataKey="date" tick={{ fontSize: 10 }} stroke="var(--muted-foreground)" />
                          <YAxis tick={{ fontSize: 10 }} stroke="var(--muted-foreground)" />
                          <Tooltip
                            contentStyle={{
                              borderRadius: 8,
                              border: "1px solid var(--border)",
                              background: "var(--background)",
                            }}
                          />
                          <Area
                            type="monotone"
                            dataKey="total_tokens"
                            stroke="var(--chart-1)"
                            fill="url(#tokenGrad)"
                            strokeWidth={2}
                            name="Tokens"
                          />
                        </AreaChart>
                      </ResponsiveContainer>
                    </div>
                    <div>
                      <ResponsiveContainer width="100%" height={260}>
                        <PieChart>
                          <Pie
                            data={[
                              { name: t("dashboard.completed"), value: summary?.completed || 0, status: "completed" },
                              { name: t("dashboard.failed"), value: summary?.failed || 0, status: "failed" },
                              { name: t("dashboard.running"), value: summary?.running || 0, status: "running" },
                            ]}
                            cx="50%"
                            cy="50%"
                            innerRadius={35}
                            outerRadius={70}
                            dataKey="value"
                            paddingAngle={2}
                            onClick={(d) => {
                              if (d?.payload?.status) navigate(`/chat?status=${d.payload.status}`);
                            }}
                            style={{ cursor: "pointer" }}
                          >
                            {[{ status: "completed" }, { status: "failed" }, { status: "running" }].map((d, i) => (
                              <Cell key={`cell-${d.status}`} fill={COLORS[i]} />
                            ))}
                          </Pie>
                          <Tooltip
                            contentStyle={{
                              borderRadius: 8,
                              border: "1px solid var(--border)",
                              background: "var(--background)",
                            }}
                          />
                          <Legend wrapperStyle={{ fontSize: 10 }} />
                        </PieChart>
                      </ResponsiveContainer>
                    </div>
                  </div>
                ) : (
                  <EmptyState
                    icon={<Inbox size={24} />}
                    title={t("dashboard.noData")}
                    description={t("dashboard.noDataDesc")}
                    compact
                  />
                )}
              </CardContent>
            </CollapsibleContent>
          </Collapsible>
        </Card>

        {/* Backend Status Bar */}
        {!loading && Object.keys(agents).length > 0 && (
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-sm flex items-center gap-2">
                <Activity size={14} className="text-success" />
                {t("dashboard.backendHealth")}
              </CardTitle>
            </CardHeader>
            <CardContent>
              <div className="flex items-center gap-6 flex-wrap">
                {Object.entries(agents).map(([id, a]) => (
                  <div key={id} className="flex items-center gap-3">
                    <div className="flex items-center gap-2">
                      <StatusDot
                        label={t(a.ready ? "health.ready" : "health.notReady")}
                        className={a.ready ? "bg-success" : "bg-muted-foreground/30"}
                        sizeClassName="w-2.5 h-2.5"
                        pulse={a.ready}
                      />
                      <button
                        onClick={() => navigate("/agents")}
                        className="text-sm font-medium hover:text-primary transition-colors"
                      >
                        {a.name || agentDisplayName(id)}
                      </button>
                    </div>
                    {a.mode && (
                      <Badge variant="secondary" className="text-2xs">
                        {a.mode}
                      </Badge>
                    )}
                    {a.port ? <span className="text-xs text-muted-foreground">:{a.port}</span> : null}
                    {typeof a.tools_count === "number" && (
                      <span className="text-xs text-muted-foreground/60">{a.tools_count} tools</span>
                    )}
                  </div>
                ))}
                <Separator orientation="vertical" className="h-5" />
                {planStats.total > 0 && (
                  <span className="text-xs text-muted-foreground">
                    {t("dashboard.planCompletion", {
                      rate: planStats.completion_rate,
                      completed: planStats.completed,
                      total: planStats.total,
                    })}
                  </span>
                )}
              </div>
            </CardContent>
          </Card>
        )}
      </div>
    </div>
  );
}

/* ── KPI Card ── */
function KpiCard({
  loading,
  icon,
  value,
  label,
  color,
  sub,
  onClick,
}: {
  loading: boolean;
  icon: React.ReactNode;
  value: string | number;
  label: string;
  color: string;
  sub?: string;
  onClick?: () => void;
}) {
  return (
    <Card
      className={`border-border/50 transition-colors ${onClick ? "cursor-pointer hover:bg-muted/40" : ""}`}
      onClick={onClick}
    >
      <CardContent className="p-4 flex items-center gap-3">
        <div className={`${color} shrink-0`}>{icon}</div>
        <div className="min-w-0">
          <div className="text-lg font-bold tabular-nums truncate">
            {loading ? <Skeleton className="h-6 w-14" /> : value}
          </div>
          <div className="text-2xs text-muted-foreground">{label}</div>
          {sub && <div className="text-2xs text-muted-foreground/60">{sub}</div>}
        </div>
      </CardContent>
    </Card>
  );
}
