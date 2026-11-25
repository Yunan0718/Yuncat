import { useEffect, useState, useCallback } from "react";
import { useApi } from "../hooks/useApi";
import { useAppStore } from "../store/appStore";
import { useTranslation } from "react-i18next";
import { useConfirmDialog } from "../hooks/useConfirmDialog";
import { EmptyState } from "../components/common/EmptyState";
import { Server, Play, Pause } from "lucide-react";

interface McpServer {
  id: string;
  name: string;
  command: string;
  args: string[];
  env: Record<string, string>;
  enabled: boolean;
  created_at: number;
  updated_at: number;
}

interface DiscoveredServer {
  name: string;
  command: string;
  args: string[];
  env: Record<string, string>;
}

interface DiscoverySource {
  source: string;
  path: string;
  servers: DiscoveredServer[];
}

interface McpToolInfo {
  name: string;
  description?: string;
  /** 后端一并返回工具的 JSON Schema——用来告诉用户这个工具要收什么参数 */
  input_schema?: {
    type?: string;
    properties?: Record<string, { type?: string; description?: string }>;
    required?: string[];
  };
}

interface McpToolParam {
  name: string;
  type: string;
  required: boolean;
}

/** 从 JSON Schema 抽出参数列表（后端已经连同工具描述一起返回了 schema）。 */
function toolParams(tool: McpToolInfo): McpToolParam[] {
  const properties = tool.input_schema?.properties ?? {};
  const required = new Set(tool.input_schema?.required ?? []);
  return Object.entries(properties).map(([name, spec]) => ({
    name,
    type: spec?.type ?? "any",
    required: required.has(name),
  }));
}

const EMPTY_FORM = { name: "", command: "", args: "", env: "" };

/**
 * Phase 4 McpManager: MCP 服务器统一管理面板
 *
 * 参考 CC Switch UI 布局——统一管理所有 MCP 服务器的配置。
 * 功能: 列表 + 新建/编辑/启用禁用/删除
 */
export function McpManager() {
  const { t } = useTranslation();
  const { api } = useApi();
  const { confirm, confirmDialog } = useConfirmDialog();
  const backendReady = useAppStore((s) => s.backendReady);
  const [servers, setServers] = useState<McpServer[]>([]);
  const [statusMsg, setStatusMsg] = useState<string | null>(null);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [form, setForm] = useState(EMPTY_FORM);
  const [showNew, setShowNew] = useState(false);
  const [discovered, setDiscovered] = useState<DiscoverySource[]>([]);
  const [discovering, setDiscovering] = useState(false);
  const [toolsPanel, setToolsPanel] = useState<{ id: string; name: string; tools: McpToolInfo[] } | null>(null);
  const [toolsLoadingId, setToolsLoadingId] = useState("");

  const loadServers = useCallback(() => {
    if (!backendReady) return;
    api<McpServer[]>("/api/v1/mcp/servers")
      .then(setServers)
      .catch(() => setStatusMsg(t("mcp.loadFailed")));
  }, [api, backendReady, t]);

  useEffect(() => {
    loadServers();
  }, [loadServers]);

  const doDiscover = async () => {
    setDiscovering(true);
    setStatusMsg(null);
    try {
      const result = await api<{ sources: DiscoverySource[] }>("/api/v1/mcp/discover");
      setDiscovered(result.sources || []);
      if (!result.sources?.length) {
        setStatusMsg(t("mcp.noDiscovered"));
      }
    } catch (e: unknown) {
      setStatusMsg(`${t("mcp.discoverFailed")}: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setDiscovering(false);
    }
  };

  // 查看某台 MCP 服务器提供的工具——后端会临时拉起该服务器再列出工具
  const showTools = async (s: McpServer) => {
    setToolsLoadingId(s.id);
    setStatusMsg(null);
    try {
      const r = await api<{ ok: boolean; tools: McpToolInfo[] }>(
        `/api/v1/mcp/servers/${s.id}/tools`);
      setToolsPanel({ id: s.id, name: s.name, tools: r.tools || [] });
    } catch (e: unknown) {
      setToolsPanel(null);
      setStatusMsg(`${t("mcp.toolsFailed")}: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setToolsLoadingId("");
    }
  };

  const importDiscovered = async (source: string, server: DiscoveredServer) => {
    try {
      await api("/api/v1/mcp/discover/import", {
        method: "POST",
        body: JSON.stringify({ source, server }),
      });
      setStatusMsg(t("mcp.imported"));
      loadServers();
    } catch (e: unknown) {
      setStatusMsg(`${t("mcp.importFailed")}: ${e instanceof Error ? e.message : String(e)}`);
    }
  };

  const resetForm = () => {
    setForm(EMPTY_FORM);
    setEditingId(null);
    setShowNew(false);
  };

  const doSave = async () => {
    const args = form.args
      .split("\n")
      .map((s) => s.trim())
      .filter(Boolean);
    let env: Record<string, string> = {};
    try {
      if (form.env.trim()) env = JSON.parse(form.env);
    } catch {
      setStatusMsg(t("mcp.envJsonError"));
      return;
    }

    try {
      if (editingId) {
        await api(`/api/v1/mcp/servers/${editingId}`, {
          method: "PUT",
          body: JSON.stringify({ name: form.name, command: form.command, args, env }),
        });
        setStatusMsg(t("mcp.updated"));
      } else {
        await api("/api/v1/mcp/servers", {
          method: "POST",
          body: JSON.stringify({ name: form.name, command: form.command, args, env }),
        });
        setStatusMsg(t("mcp.created"));
      }
      resetForm();
      loadServers();
    } catch (e: unknown) {
      setStatusMsg(
        `${editingId ? t("mcp.updateFailed") : t("mcp.createFailed")}: ${e instanceof Error ? e.message : String(e)}`,
      );
    }
  };

  const toggleEnabled = async (server: McpServer) => {
    try {
      await api(`/api/v1/mcp/servers/${server.id}`, {
        method: "PUT",
        body: JSON.stringify({ enabled: !server.enabled }),
      });
      loadServers();
    } catch (e: unknown) {
      setStatusMsg(`${t("mcp.updateFailed")}: ${e instanceof Error ? e.message : String(e)}`);
    }
  };

  const doDelete = async (id: string) => {
    const ok = await confirm({ title: t("common.confirm"), description: t("mcp.deleteConfirm"), variant: "destructive" });
    if (!ok) return;
    try {
      await api(`/api/v1/mcp/servers/${id}`, { method: "DELETE" });
      setStatusMsg(t("mcp.deleted"));
      loadServers();
    } catch (e: unknown) {
      setStatusMsg(`${t("mcp.deleteFailed")}: ${e instanceof Error ? e.message : String(e)}`);
    }
  };

  const startEdit = (s: McpServer) => {
    setEditingId(s.id);
    setShowNew(false);
    setForm({
      name: s.name,
      command: s.command,
      args: s.args.join("\n"),
      env: JSON.stringify(s.env, null, 2),
    });
  };

  if (!backendReady) return <div className="p-8 text-muted-foreground">{t("legacy.wf_276d")}</div>;

  return (
    <div className="page-shell space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold tracking-tight">{t("mcp.title")}</h1>
        <div className="flex items-center gap-2">
          <button
            onClick={doDiscover}
            disabled={discovering}
            className="px-3 py-1.5 border rounded text-sm hover:bg-secondary disabled:opacity-50"
          >
            {discovering ? t("mcp.discovering") : t("mcp.discover")}
          </button>
          <button
            onClick={() => {
              resetForm();
              setShowNew(true);
            }}
            className="px-3 py-1.5 bg-primary text-white rounded text-sm"
          >
            {t("mcp.add")}
          </button>
        </div>
      </div>

      {statusMsg && <div className="px-4 py-2 rounded bg-secondary text-sm font-mono">{statusMsg}</div>}

      {/* 自动发现结果 */}
      {discovered.length > 0 && (
        <div className="border rounded p-4 space-y-3 bg-secondary/50">
          <h2 className="font-semibold text-sm">{t("mcp.discoveredTitle")}</h2>
          {discovered.map((source) => (
            <div key={source.source} className="space-y-2">
              <div className="text-xs text-muted-foreground">
                {t(`mcp.source.${source.source}`, { defaultValue: source.source })} — {source.path}
              </div>
              <div className="divide-y border rounded bg-background">
                {source.servers.map((s) => (
                  <div key={s.name} className="px-3 py-2 flex items-center justify-between gap-3">
                    <div className="min-w-0">
                      <div className="text-sm font-medium">{s.name}</div>
                      <div className="text-xs text-muted-foreground font-mono truncate">
                        {s.command} {s.args.join(" ")}
                      </div>
                    </div>
                    <button
                      onClick={() => importDiscovered(source.source, s)}
                      className="px-2 py-1 text-xs bg-primary text-white rounded shrink-0"
                    >
                      {t("mcp.import")}
                    </button>
                  </div>
                ))}
              </div>
            </div>
          ))}
        </div>
      )}

      {/* 新建/编辑表单 */}
      {(showNew || editingId) && (
        <div className="border rounded p-4 space-y-3 bg-secondary">
          <h2 className="font-semibold text-sm">{editingId ? t("mcp.editTitle") : t("mcp.newTitle")}</h2>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="text-xs text-muted-foreground">{t("mcp.name")}</label>
              <input
                value={form.name}
                onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))}
                className="w-full px-3 py-1.5 border rounded text-sm"
                placeholder={t("phase49.mcp.placeholderName")}
              />
            </div>
            <div>
              <label className="text-xs text-muted-foreground">{t("mcp.command")}</label>
              <input
                value={form.command}
                onChange={(e) => setForm((f) => ({ ...f, command: e.target.value }))}
                className="w-full px-3 py-1.5 border rounded text-sm font-mono"
                placeholder={t("phase49.mcp.placeholderCommand")}
              />
            </div>
          </div>
          <div>
            <label className="text-xs text-muted-foreground">{t("mcp.args")}</label>
            <textarea
              value={form.args}
              onChange={(e) => setForm((f) => ({ ...f, args: e.target.value }))}
              rows={3}
              className="w-full px-3 py-1.5 border rounded text-xs font-mono resize-y"
              placeholder={"-y\n@modelcontextprotocol/server-filesystem\n/tmp"}
            />
          </div>
          <div>
            <label className="text-xs text-muted-foreground">{t("mcp.env")}</label>
            <textarea
              value={form.env}
              onChange={(e) => setForm((f) => ({ ...f, env: e.target.value }))}
              rows={3}
              className="w-full px-3 py-1.5 border rounded text-xs font-mono resize-y"
              placeholder={t("phase49.mcp.placeholderEnv")}
            />
          </div>
          <div className="flex gap-2">
            <button onClick={doSave} className="px-4 py-1.5 bg-primary text-white rounded text-sm">
              {t("mcp.save")}
            </button>
            <button onClick={resetForm} className="px-4 py-1.5 border rounded text-sm hover:bg-secondary">
              {t("mcp.cancel")}
            </button>
          </div>
        </div>
      )}

      {/* 服务器列表 */}
      {servers.length === 0 ? (
        <EmptyState icon={<Server size={20} />} title={t("mcp.noServers")} compact />
      ) : (
        <div className="border rounded-lg divide-y">
          {servers.map((s) => (
            <div key={s.id} className="px-4 py-3 flex items-center justify-between gap-4">
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2">
                  <span className="font-semibold text-sm">{s.name}</span>
                  <span
                    className={`text-xs px-1.5 py-0.5 rounded ${
                      s.enabled ? "bg-success/10 text-success" : "bg-muted text-foreground-secondary"
                    }`}
                  >
                    {s.enabled ? t("mcp.enabled") : t("mcp.disabled")}
                  </span>
                </div>
                <div className="text-xs text-muted-foreground font-mono mt-0.5 truncate">
                  {s.command} {s.args.join(" ")}
                </div>
              </div>
              <div className="flex items-center gap-1 shrink-0">
                <button
                  onClick={() => void showTools(s)}
                  disabled={!s.enabled || toolsLoadingId === s.id}
                  className="px-2 py-1 text-xs border rounded hover:bg-secondary disabled:opacity-50"
                  title={t("mcp.viewToolsHint")}
                >
                  {toolsLoadingId === s.id ? t("mcp.toolsLoading") : t("mcp.viewTools")}
                </button>
                <button
                  onClick={() => toggleEnabled(s)}
                  className="px-2 py-1 text-xs border rounded hover:bg-secondary"
                  title={s.enabled ? t("common.disable") : t("common.enable")}
                  aria-label={s.enabled ? t("common.disable") : t("common.enable")}
                >
                  {s.enabled ? <Pause size={12} /> : <Play size={12} />}
                </button>
                <button onClick={() => startEdit(s)} className="px-2 py-1 text-xs border rounded hover:bg-secondary">
                  {t("common.edit")}
                </button>
                <button
                  onClick={() => doDelete(s.id)}
                  className="px-2 py-1 text-xs border rounded-lg hover:bg-destructive/10 text-destructive"
                >
                  {t("common.delete")}
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
      {toolsPanel && (
        <div className="border rounded p-4 space-y-2 bg-secondary/50">
          <div className="flex items-center justify-between">
            <h2 className="font-semibold text-sm">
              {t("mcp.toolsTitle")} — {toolsPanel.name}
            </h2>
            <button
              onClick={() => setToolsPanel(null)}
              className="px-2 py-0.5 text-xs border rounded hover:bg-secondary"
            >
              {t("common.close")}
            </button>
          </div>
          {toolsPanel.tools.length === 0 ? (
            <div className="text-xs text-muted-foreground">{t("mcp.noTools")}</div>
          ) : (
            <div className="divide-y border rounded bg-background">
              {toolsPanel.tools.map((tool) => {
                const params = toolParams(tool);
                return (
                  <div key={tool.name} className="px-3 py-2">
                    <div className="text-sm font-medium font-mono">{tool.name}</div>
                    {tool.description && (
                      <div className="text-xs text-muted-foreground">{tool.description}</div>
                    )}
                    {params.length > 0 && (
                      <div className="mt-1.5 flex flex-wrap items-center gap-1">
                        <span className="text-[11px] text-muted-foreground">
                          {t("mcp.toolsParams")}
                        </span>
                        {params.map((param) => (
                          <span
                            key={param.name}
                            title={param.required ? t("mcp.toolsRequired") : undefined}
                            className="rounded border px-1.5 py-0.5 font-mono text-[11px] text-muted-foreground"
                          >
                            {param.name}
                            <span className="opacity-60">: {param.type}</span>
                            {param.required && (
                              <span className="ml-0.5 text-warning">*</span>
                            )}
                          </span>
                        ))}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          )}
        </div>
      )}
      {confirmDialog}
    </div>
  );
}
