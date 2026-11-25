import { useEffect, useState, useCallback, useRef } from "react";
import { useTranslation } from "react-i18next";
import { isAbortedError, useApi } from "../hooks/useApi";
import { useAppStore } from "../store/appStore";
import { emitGlobalToast } from "../components/common/toast-context";
import { ResourceChart } from "../components/ResourceChart";
import { LoadingCurve } from "../components/common/LoadingCurve";
import { HealthDot } from "../components/common/StatusDot";
import { EmptyState } from "../components/common/EmptyState";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "../components/ui/dialog";
import { Badge } from "../components/ui/badge";
import { Button } from "../components/ui/button";
import { Switch } from "../components/ui/switch";
import {
  Bot,
  Play,
  Square,
  RotateCcw,
  ChevronDown,
  ChevronUp,
  Wrench,
  Cpu,
  MemoryStick,
  Activity,
  Server,
  Zap,
  AlertCircle,
  SlidersHorizontal,
  Terminal,
} from "lucide-react";
import { agentMeta } from "../lib/agentMeta";

interface AgentInfo {
  id: string;
  name?: string;
  status: string;
  port?: number;
  mode?: string;
  version?: string;
  /** Phase X: 后端真实探测该 Agent 当前是否可用（外部 Agent 探端口 / CLI 探二进制） */
  detected?: boolean;
  /** Phase X: 该 Agent 是否已启用——只有启用的 Agent 才能在任务中被调用 */
  enabled?: boolean;
  /** Phase X (2026-09-10): 外部 Agent 标识——即使启用也仅在 MAX 模式可被调用 */
  external?: boolean;
  kind?: string;
  capabilities: {
    supports_stream: boolean;
    supports_tools: boolean;
    supports_browser?: boolean;
    supports_interrupt?: boolean;
    supports_image?: boolean;
    supports_memory?: boolean;
  };
  tools_count?: number;
}

interface ProviderInfo {
  provider: string;
  name: string;
  model: string;
  api_key_configured: boolean;
  health_status?: string;
  models: Array<{ id: string; name: string; context: number }>;
}

interface AgentModelConfig {
  agent_id: string;
  provider: string;
  provider_name: string;
  model: string;
  models: Array<{ id: string; name: string; context: number }>;
  is_custom: boolean;
}

interface MonitoringData {
  agents: Record<
    string,
    {
      cpu_percent?: number;
      memory_mb?: number;
      queue_depth?: number;
      pid?: number;
      dropped_logs?: number;
      status?: string;
    }
  >;
}

interface ToolInfo {
  name: string;
  description?: string;
  category?: string;
}

/**
 * Phase D: Agent 管理面板 —— 对齐 Docker Desktop / Claude Code 的进程管理器风格
 */
export function AgentManager() {
  const { t } = useTranslation();
  const { api } = useApi();
  const backendReady = useAppStore((s) => s.backendReady);
  const [agents, setAgents] = useState<AgentInfo[]>([]);
  const [monitoring, setMonitoring] = useState<MonitoringData | null>(null);
  const [statusMsg, setStatusMsg] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const loadRequestRef = useRef(0);
  const loadData = useCallback(() => {
    if (!backendReady) return;
    const requestId = ++loadRequestRef.current;
    setLoading(true);
    setStatusMsg(null);
    Promise.all([
      api<AgentInfo[]>("/api/v1/agents"),
      // 监控是可选增强；主体列表成功时不应因指标端点失败而整体失败。
      api<MonitoringData>("/api/v1/monitoring").catch(() => null),
    ])
      .then(async ([aList, mon]) => {
        const enriched = await Promise.all(
          aList.map(async (agent) => {
            try {
              const result = await api<{ ok?: boolean; tools?: ToolInfo[] }>(`/api/v1/agents/${agent.id}/tools`);
              return { ...agent, tools_count: result.tools?.length || 0 };
            } catch {
              // 工具数量是展示增强数据，失败时降级为 0，不阻断 Agent 主体列表。
              return { ...agent, tools_count: 0 };
            }
          }),
        );
        if (requestId !== loadRequestRef.current) return;
        setAgents(enriched);
        setMonitoring(mon);
      })
      .catch((error) => {
        if (requestId !== loadRequestRef.current || isAbortedError(error)) return;
        const detail = error instanceof Error ? error.message : String(error);
        setStatusMsg(`${t("agents.loadFailed")}: ${detail}`);
      })
      .finally(() => {
        if (requestId === loadRequestRef.current) setLoading(false);
      });
  }, [backendReady, api, t]);

  useEffect(() => {
    loadData();
  }, [backendReady, api, loadData]);

  useEffect(() => {
    if (!backendReady) return;
    const timer = setInterval(() => {
      api<MonitoringData>("/api/v1/monitoring")
        .then(setMonitoring)
        // 监控轮询是 best-effort；失败时保留上一次有效指标，下一轮会重试。
        .catch(() => {});
    }, 5000);
    return () => clearInterval(timer);
  }, [backendReady, api]);

  const doAction = async (agentId: string, action: "start" | "stop" | "restart") => {
    try {
      await api(`/api/v1/agents/${agentId}/${action}`, { method: "POST" });
      const okKey =
        action === "stop" ? "agents.stopOk" : action === "restart" ? "agents.restartOk" : "agents.startOk";
      loadData();
      setStatusMsg(`${t(okKey)}: ${agentId}`);
    } catch (e: unknown) {
      const failKey =
        action === "stop" ? "agents.stopFail" : action === "restart" ? "agents.restartFail" : "agents.startFail";
      setStatusMsg(`${t(failKey)}: ${e instanceof Error ? e.message : String(e)}`);
    }
  };

  // Phase X: 启用 / 禁用 Agent——写入 settings，控制其是否出现在任务调用的可选列表
  const toggleEnabled = async (agentId: string, enable: boolean) => {
    try {
      await api(`/api/v1/agents/${agentId}/${enable ? "enable" : "disable"}`, { method: "POST" });
      loadData();
      setStatusMsg(t(enable ? "agents.enableOk" : "agents.disableOk", { agent: agentId }));
    } catch (e: unknown) {
      setStatusMsg(
        `${t(enable ? "agents.enableFail" : "agents.disableFail", { agent: agentId })}: ${
          e instanceof Error ? e.message : String(e)
        }`,
      );
    }
  };

  if (!backendReady) {
    return <div className="p-8 text-muted-foreground">{t("common.back")}</div>;
  }

  const runningCount = agents.filter((a) => a.status === "running").length;
  const totalTools = agents.reduce((sum, a) => sum + (a.tools_count || 0), 0);

  return (
    <div className="h-full overflow-y-auto">
      <div className="page-shell space-y-6">
        <AgentManagerHeader
          running={runningCount}
          total={agents.length}
          tools={totalTools}
          loading={loading}
          onRefresh={loadData}
        />

        {statusMsg && (
          <div role="status" aria-live="polite" className="flex items-center gap-2 px-4 py-2.5 rounded-lg bg-secondary text-sm">
            <AlertCircle size={14} className="text-muted-foreground" />
            <span className="font-mono text-xs">{statusMsg}</span>
          </div>
        )}

        {agents.length === 0 ? (
          loading && !statusMsg ? (
            <div role="status" className="flex items-center justify-center gap-2 py-12 text-sm text-muted-foreground">
              <LoadingCurve variant="lissajous" size={18} particleCount={6} />
              <span>{t("agents.loading")}</span>
            </div>
          ) : statusMsg ? null : (
            <EmptyState
              icon={<Bot size={24} />}
              title={t("agents.noAgents")}
              description={t("agents.noAgentsDesc")}
            />
          )
        ) : (
          <div className="grid gap-4">
            {agents.map((agent) => (
              <AgentCard
                key={agent.id}
                agent={agent}
                mon={monitoring?.agents?.[agent.id]}
                onAction={doAction}
                enabled={agent.enabled}
                detected={agent.detected}
                onToggleEnabled={toggleEnabled}
              />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function AgentManagerHeader({
  running,
  total,
  tools,
  loading,
  onRefresh,
}: {
  running: number;
  total: number;
  tools: number;
  loading: boolean;
  onRefresh: () => void;
}) {
  const { t } = useTranslation();
  return (
    <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
      <div>
        <h1 className="text-xl font-semibold tracking-tight">{t("agents.title")}</h1>
        <p className="text-sm text-muted-foreground mt-0.5">{t("agents.count", { total, running })}</p>
      </div>
      <div className="flex items-center gap-3">
        <StatBadge icon={Zap} value={running} label={t("agents.running")} variant="success" />
        <StatBadge icon={Server} value={total - running} label={t("agents.stopped")} variant="muted" />
        <StatBadge icon={Wrench} value={tools} label={t("agents.toolsTotal")} variant="primary" />
        <Button variant="outline" size="sm" onClick={onRefresh} disabled={loading} className="h-8 text-xs">
          {loading ? t("agents.loading") : t("agents.refresh")}
        </Button>
      </div>
    </div>
  );
}

function StatBadge({
  icon: Icon,
  value,
  label,
  variant,
}: {
  icon: typeof Bot;
  value: number;
  label: string;
  variant: "success" | "muted" | "primary";
}) {
  const color =
    variant === "success"
      ? "text-success bg-success/10"
      : variant === "primary"
        ? "text-primary bg-primary/10"
        : "text-muted-foreground bg-secondary";
  return (
    <div className={`flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg text-xs ${color}`}>
      <Icon size={13} />
      <span className="font-semibold">{value}</span>
      <span className="opacity-80">{label}</span>
    </div>
  );
}

function AgentCard({
  agent,
  mon,
  onAction,
  enabled,
  detected,
  onToggleEnabled,
}: {
  agent: AgentInfo;
  mon?: MonitoringData["agents"][string];
  onAction: (id: string, action: "start" | "stop" | "restart") => void;
  enabled?: boolean;
  detected?: boolean;
  onToggleEnabled: (id: string, enable: boolean) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const [toolsOpen, setToolsOpen] = useState(false);
  const [modelOpen, setModelOpen] = useState(false);
  const isRunning = agent.status === "running";
  // 原生 Yuncat 恒启用且不可禁用；外部 / CLI Agent 由用户显式开关
  const isNative = agent.id === "yuncat";
  const isExternal = Boolean(agent.external) || agent.kind === "external" || agent.kind === "cli";
  const isEnabled = isNative || Boolean(enabled);
  const { t } = useTranslation();

  return (
    <div
      className={`border rounded-xl overflow-hidden transition-colors ${
        isRunning ? "border-border bg-card" : "border-border/60 bg-secondary/30"
      }`}
    >
      <div className="p-4 flex flex-col sm:flex-row sm:items-center gap-4">
        <AgentIdentity agent={agent} detected={detected} />
        <AgentMetrics agent={agent} mon={mon} />
        {/* Phase X: 启用开关——只有启用的 Agent 才能被任务调用 */}
        <div className="flex items-center gap-2 shrink-0 sm:ml-auto">
          <span className="text-[10px] text-muted-foreground whitespace-nowrap">
            {isEnabled ? t("agents.enabled") : t("agents.disabled")}
          </span>
          <Switch
            checked={isEnabled}
            disabled={isNative}
            onCheckedChange={(v) => onToggleEnabled(agent.id, v)}
            title={isNative ? t("agents.nativeCannotDisable") : t("agents.enableHint")}
            aria-label={isEnabled ? t("agents.disable") : t("agents.enable")}
          />
        </div>
        <AgentActions
          isRunning={isRunning}
          onRestart={() => onAction(agent.id, "restart")}
          onStart={() => onAction(agent.id, "start")}
          onStop={() => onAction(agent.id, "stop")}
          onToggleExpand={() => setExpanded(!expanded)}
          onOpenTools={() => setToolsOpen(true)}
          onOpenModel={() => setModelOpen(true)}
          expanded={expanded}
        />
      </div>

      {/* Phase X (2026-09-10): 外部 Agent 即使启用，也仅在 MAX（全功能）模式下可被显式调用 */}
      {isExternal && (
        <div className="px-4 pb-3 text-[10px] text-muted-foreground">
          {t("agents.maxModeOnly")}
        </div>
      )}

      {!isRunning && <AgentSetupHint agentId={agent.id} port={agent.port} />}

      {expanded && (
        <div className="border-t px-4 pb-4">
          <div className="pt-4">
            <ResourceChart agentId={agent.id} />
          </div>
        </div>
      )}

      <AgentToolsDialog agentId={agent.id} open={toolsOpen} onClose={() => setToolsOpen(false)} />
      <AgentModelDialog agentId={agent.id} open={modelOpen} onClose={() => setModelOpen(false)} />
    </div>
  );
}

function AgentIdentity({ agent, detected }: { agent: AgentInfo; detected?: boolean }) {
  const { t } = useTranslation();
  const isRunning = agent.status === "running";
  const detectedKnown = detected !== undefined;
  return (
    <div className="flex items-center gap-3 min-w-0 flex-1">
      <div
        className={`w-10 h-10 rounded-xl flex items-center justify-center shrink-0 ${
          isRunning ? "bg-primary/10 text-primary" : "bg-muted text-muted-foreground"
        }`}
      >
        <Bot size={20} strokeWidth={1.7} />
      </div>
      <div className="min-w-0">
        <div className="flex items-center gap-2">
          <span className="font-semibold text-sm truncate">{agent.name || agent.id}</span>
          <AgentStatusBadge status={agent.status} />
          {detectedKnown && (
            <span
              className={`text-[9px] px-1.5 py-0.5 rounded-full font-medium ${
                detected
                  ? "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400"
                  : "bg-muted text-muted-foreground"
              }`}
            >
              {detected ? t("agents.detected") : t("agents.notDetected")}
            </span>
          )}
        </div>
        <div className="flex items-center gap-2 mt-1 flex-wrap">
          <CapabilityBadge active={agent.capabilities.supports_stream} label={t("agents.capStream")} variant="stream" />
          <CapabilityBadge active={agent.capabilities.supports_tools} label={t("agents.capTools")} variant="tools" />
          <CapabilityBadge active={agent.capabilities.supports_image} label={t("agents.capImage")} variant="image" />
          <CapabilityBadge
            active={agent.capabilities.supports_browser}
            label={t("agents.capBrowser")}
            variant="browser"
          />
          {agent.mode && (
            <Badge variant="secondary" className="text-[9px] px-1.5 py-0 h-4">
              {modeLabel(t, agent.mode)}
            </Badge>
          )}
        </div>
      </div>
    </div>
  );
}

function AgentStatusBadge({ status }: { status: string }) {
  const { t } = useTranslation();
  const isRunning = status === "running";
  return (
    <span
      className={`inline-flex items-center gap-1 text-[10px] px-2 py-0.5 rounded-full font-medium ${
        isRunning ? "bg-success/10 text-success" : "bg-muted text-muted-foreground"
      }`}
    >
      <span className={`w-1.5 h-1.5 rounded-full ${isRunning ? "bg-success" : "bg-muted-foreground/50"}`} />
      {isRunning ? t("agents.running") : t("agents.stopped")}
    </span>
  );
}

function CapabilityBadge({
  active,
  label,
  variant,
}: {
  active?: boolean;
  label: string;
  variant: "stream" | "tools" | "image" | "browser";
}) {
  if (!active) return null;
  const colors: Record<typeof variant, string> = {
    stream: "bg-blue-500/10 text-blue-600 dark:text-blue-400",
    tools: "bg-violet-500/10 text-violet-600 dark:text-violet-400",
    image: "bg-amber-500/10 text-amber-600 dark:text-amber-400",
    browser: "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400",
  };
  return <span className={`text-[9px] px-1.5 py-0.5 rounded font-medium ${colors[variant]}`}>{label}</span>;
}

function AgentMetrics({ agent, mon }: { agent: AgentInfo; mon?: MonitoringData["agents"][string] }) {
  const { t } = useTranslation();
  return (
    <div className="flex items-center gap-4 sm:gap-6 text-xs text-muted-foreground shrink-0">
      {agent.port ? <Metric icon={Server} label={`:${agent.port}`} title={t("agents.port")} /> : null}
      {mon?.pid ? <Metric icon={Cpu} label={`PID ${mon.pid}`} title={t("agents.pid")} /> : null}
      {mon?.cpu_percent !== undefined ? (
        <Metric icon={Activity} label={`${mon.cpu_percent.toFixed(1)}%`} title={t("agents.cpu")} />
      ) : null}
      {mon?.memory_mb !== undefined ? (
        <Metric icon={MemoryStick} label={`${mon.memory_mb.toFixed(0)} MB`} title={t("agents.memory")} />
      ) : null}
    </div>
  );
}

function Metric({ icon: Icon, label, title }: { icon: typeof Bot; label: string; title: string }) {
  return (
    <div className="flex items-center gap-1.5" title={title}>
      <Icon size={13} />
      <span className="font-mono">{label}</span>
    </div>
  );
}

function AgentActions({
  isRunning,
  onRestart,
  onStart,
  onStop,
  onToggleExpand,
  onOpenTools,
  onOpenModel,
  expanded,
}: {
  isRunning: boolean;
  onRestart: () => void;
  onStart: () => void;
  onStop: () => void;
  onToggleExpand: () => void;
  onOpenTools: () => void;
  onOpenModel: () => void;
  expanded: boolean;
}) {
  const { t } = useTranslation();
  return (
    <div className="flex items-center gap-2 shrink-0">
      <Button variant="ghost" size="sm" className="h-8 text-xs gap-1" onClick={onOpenModel}>
        <SlidersHorizontal size={13} />
        {t("agents.model")}
      </Button>
      <Button variant="ghost" size="sm" className="h-8 text-xs gap-1" onClick={onOpenTools}>
        <Wrench size={13} />
        {t("agents.tools")}
      </Button>
      <Button variant="ghost" size="sm" className="h-8 text-xs gap-1" onClick={onToggleExpand}>
        {expanded ? <ChevronUp size={13} /> : <ChevronDown size={13} />}
        {t("agents.chart")}
      </Button>
      <Button variant="outline" size="sm" className="h-8 text-xs gap-1" onClick={onRestart}>
        <RotateCcw size={13} />
        {t("agents.restart")}
      </Button>
      {isRunning ? (
        <Button
          variant="outline"
          size="sm"
          className="h-8 text-xs gap-1 text-destructive hover:bg-destructive/10"
          onClick={onStop}
        >
          <Square size={13} />
          {t("agents.stop")}
        </Button>
      ) : (
        <Button size="sm" className="h-8 text-xs gap-1" onClick={onStart}>
          <Play size={13} />
          {t("agents.start")}
        </Button>
      )}
    </div>
  );
}

/**
 * 外部 agent（Hermes / OpenClaw）由用户自行启动，Yuncat 不托管其生命周期。
 * 当它们处于停止状态时，给出「仓库 + 启动命令」引导，取代无效的原生 start 按钮。
 * 数据全部来自 lib/agentMeta，避免写死 openclaw/hermes。
 */
function AgentSetupHint({ agentId, port }: { agentId: string; port?: number }) {
  const { t } = useTranslation();
  const meta = agentMeta(agentId);
  if (!meta?.repo) return null;
  const cmd = meta.cliEntry ? meta.cliEntry.replace("{port}", String(port ?? "?")) : "";
  return (
    <div className="border-t px-4 py-2.5 flex items-center justify-between gap-3 text-xs bg-secondary/30">
      <div className="min-w-0">
        <div className="flex items-center gap-1.5">
          <Terminal size={12} className="text-muted-foreground shrink-0" />
          <span className="font-medium text-foreground">{meta.descKey ? t(meta.descKey) : meta.name}</span>
        </div>
        {cmd && <code className="block mt-1 text-[10px] font-mono text-muted-foreground truncate">{cmd}</code>}
      </div>
      <a
        href={`https://${meta.repo}`}
        target="_blank"
        rel="noopener noreferrer"
        className="shrink-0 text-[10px] px-2 py-1 border rounded hover:bg-secondary text-muted-foreground transition-colors"
      >
        {t("agentGuide.github")} →
      </a>
    </div>
  );
}

function modeLabel(t: (key: string) => string, mode: string): string {
  const key = `agents.mode${mode.charAt(0).toUpperCase()}${mode.slice(1)}`;
  const translated = t(key);
  return translated === key ? mode : translated;
}

function AgentToolsDialog({ agentId, open, onClose }: { agentId: string; open: boolean; onClose: () => void }) {
  const { t } = useTranslation();
  const { api } = useApi();
  const [tools, setTools] = useState<ToolInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    setLoading(true);
    setLoadError(null);
    api<{ ok?: boolean; tools?: ToolInfo[]; error?: string }>(`/api/v1/agents/${agentId}/tools`)
      .then((r) => setTools(r.tools || []))
      .catch((error) => {
        setTools([]);
        setLoadError(error instanceof Error ? error.message : String(error));
      })
      .finally(() => setLoading(false));
  }, [open, agentId, api]);

  return (
    <Dialog open={open} onOpenChange={onClose}>
      <DialogContent className="max-w-2xl max-h-[80vh] flex flex-col">
        <DialogHeader>
          <DialogTitle className="text-base flex items-center gap-2">
            <Wrench size={16} />
            {t("agents.toolsOf", { agent: agentId })}
          </DialogTitle>
        </DialogHeader>
        <div className="flex-1 overflow-y-auto pr-1">
          {loadError && <p role="alert" className="pb-2 text-xs text-destructive">{loadError}</p>}
          {loading ? (
            <div className="flex items-center justify-center gap-2 py-8 text-sm text-muted-foreground">
              <LoadingCurve variant="lissajous" size={20} particleCount={6} />
              <span>{t("agents.loading")}</span>
            </div>
          ) : loadError ? null : tools.length === 0 ? (
            <EmptyState compact title={t("agents.noTools")} />
          ) : (
            <div className="grid gap-2">
              {tools.map((tool) => (
                <div key={tool.name} className="border rounded-lg p-3 hover:bg-secondary/50 transition-colors">
                  <div className="flex items-center gap-2">
                    <span className="font-mono text-xs font-semibold text-primary">{tool.name}</span>
                    {tool.category && (
                      <Badge variant="outline" className="text-[9px] h-4 px-1">
                        {tool.category}
                      </Badge>
                    )}
                  </div>
                  {tool.description && (
                    <p className="text-xs text-muted-foreground mt-1 line-clamp-2">{tool.description}</p>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}

/**
 * Phase 43 M.2: per-agent 模型选择对话框
 *
 * 行业标准做法（VSCode Docker / Claude Code）：每个 Agent 后端可独立配置 Provider，
 * 不强制共享全局配置。例如：Hermes 用 DeepSeek，OC 用 Claude。
 */
function AgentModelDialog({ agentId, open, onClose }: { agentId: string; open: boolean; onClose: () => void }) {
  const { t } = useTranslation();
  const { api } = useApi();
  const [providers, setProviders] = useState<ProviderInfo[]>([]);
  const [config, setConfig] = useState<AgentModelConfig | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    setLoading(true);
    setLoadError(null);
    Promise.all([
      api<ProviderInfo[]>("/api/v1/models"),
      api<AgentModelConfig>(`/api/v1/models/agent/${agentId}`),
    ])
      .then(([list, cfg]) => {
        setProviders(Array.isArray(list) ? list : []);
        setConfig(cfg);
      })
      .catch((error) => {
        setProviders([]);
        setConfig(null);
        setLoadError(error instanceof Error ? error.message : String(error));
      })
      .finally(() => setLoading(false));
  }, [open, agentId, api]);

  const handleSetProvider = async (provider: string) => {
    if (!provider || saving) return;
    setSaving(true);
    try {
      await api(`/api/v1/models/agent/${agentId}`, {
        method: "POST",
        body: JSON.stringify({ provider }),
      });
      const cfg = await api<AgentModelConfig>(`/api/v1/models/agent/${agentId}`);
      setConfig(cfg);
    } catch {
      // P1: 用户主动切换 Provider 失败必须反馈，否则用户误以为已切换
      emitGlobalToast(t("agents.setProviderFailed"), "error");
    }
    setSaving(false);
  };

  const handleReset = async () => {
    if (saving) return;
    setSaving(true);
    try {
      await api(`/api/v1/models/agent/${agentId}`, { method: "DELETE" });
      const cfg = await api<AgentModelConfig>(`/api/v1/models/agent/${agentId}`);
      setConfig(cfg);
    } catch {
      // P1: 用户主动重置失败必须反馈
      emitGlobalToast(t("agents.resetFailed"), "error");
    }
    setSaving(false);
  };

  // 健康状态指示器——状态同时以无障碍文本给出，不能只靠颜色
  const healthDot = (status?: string) => <HealthDot status={status} />;

  return (
    <Dialog open={open} onOpenChange={onClose}>
      <DialogContent className="max-w-xl max-h-[80vh] flex flex-col">
        <DialogHeader>
          <DialogTitle className="text-base flex items-center gap-2">
            <SlidersHorizontal size={16} />
            {t("agents.modelOf", { agent: agentId })}
          </DialogTitle>
        </DialogHeader>

        {loading ? (
          <div className="flex items-center justify-center gap-2 py-8 text-sm text-muted-foreground">
            <LoadingCurve variant="lissajous" size={20} particleCount={6} />
            <span>{t("agents.loading")}</span>
          </div>
        ) : (
          <div className="space-y-4">
            {loadError && <p role="alert" className="text-xs text-destructive">{loadError}</p>}
            {/* 当前配置概览 */}
            {config && (
              <div className="border rounded-lg p-3 bg-secondary/30">
                <div className="flex items-center gap-2">
                  {healthDot(providers.find((p) => p.provider === config.provider)?.health_status || (config.provider ? "configured" : "unconfigured"))}
                  <span className="text-sm font-semibold">{config.provider_name}</span>
                  {config.is_custom && (
                    <Badge variant="secondary" className="text-[9px] h-4 px-1">
                      {t("agents.custom")}
                    </Badge>
                  )}
                </div>
                {config.model && (
                  <div className="text-xs text-muted-foreground mt-1 font-mono">{config.model}</div>
                )}
              </div>
            )}

            {/* Provider 列表 */}
            <div>
              <div className="text-xs text-muted-foreground mb-2">{t("agents.selectProvider")}</div>
              <div className="max-h-64 overflow-y-auto space-y-1">
                {providers.map((p) => {
                  const isActive = config?.provider === p.provider;
                  return (
                    <button
                      key={p.provider}
                      onClick={() => !isActive && handleSetProvider(p.provider)}
                      disabled={isActive || saving || !p.api_key_configured}
                      className={`w-full flex items-center gap-2 px-3 py-2 text-xs rounded-lg border transition-colors
                        ${isActive ? "border-primary bg-primary/10 text-primary cursor-default" : "hover:bg-secondary border-border"}
                        ${!p.api_key_configured ? "opacity-50" : ""}`}
                    >
                      {healthDot(p.health_status || (p.api_key_configured ? "configured" : "unconfigured"))}
                      <span className="flex-1 text-left truncate">{p.name}</span>
                      <span className="text-[10px] text-muted-foreground shrink-0">
                        {p.api_key_configured ? p.model : t("agents.notConfigured")}
                      </span>
                    </button>
                  );
                })}
              </div>
            </div>

            {/* 重置按钮 */}
            {config?.is_custom && (
              <Button variant="outline" size="sm" onClick={handleReset} disabled={saving} className="w-full">
                {t("agents.resetToGlobal")}
              </Button>
            )}

            <div className="text-[10px] text-muted-foreground pt-2 border-t">
              {t("agents.modelHint")}
            </div>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
