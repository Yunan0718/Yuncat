import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { useApi } from "../../hooks/useApi";
import { useToast } from "../../hooks/use-toast";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../ui/card";
import { Button } from "../ui/button";
import { DependenciesPanel } from "../DependenciesPanel";
import { Play, Square, RotateCw } from "lucide-react";
import { agentMeta, agentDisplayName } from "../../lib/agentMeta";

/* ── Agents (H2.3 · H2.2) ──────────────────────── */
export function AgentsSection({ api }: { api: ReturnType<typeof useApi>["api"] }) {
  const { t } = useTranslation();
  const { toast } = useToast();
  const [agents, setAgents] = useState<
    Array<{
      id: string;
      name: string;
      status: string;
      capabilities: Record<string, boolean>;
      port?: number;
      tools?: number;
    }>
  >([]);
  const [actionLoading, setActionLoading] = useState<string>("");

  const loadAgents = useCallback(() => {
    api<typeof agents>("/api/v1/agents")
      .then((list) => {
        // enrich with tool counts
        Promise.all(
          list.map(async (a) => {
            try {
              const tools = await api<unknown[]>(`/api/v1/agents/${a.id}/tools`);
              return { ...a, tools: tools?.length || 0 };
            } catch {
              return { ...a, tools: 0 };
            }
          }),
        ).then(setAgents);
      })
      .catch((e) => console.warn("SettingsPage: agents load failed:", e));
  }, [api]);

  useEffect(() => {
    loadAgents();
  }, [loadAgents]);

  // Task 6.15: Agent 启停控件——start/stop/restart
  const doAction = useCallback(
    async (agentId: string, action: "start" | "stop" | "restart") => {
      setActionLoading(`${agentId}:${action}`);
      try {
        await api(`/api/v1/agents/${agentId}/${action}`, { method: "POST" });
        const okKey =
          action === "stop" ? "agents.stopOk" : action === "restart" ? "agents.restartOk" : "agents.startOk";
        toast(t(okKey), "success");
        loadAgents();
      } catch {
        const failKey =
          action === "stop" ? "agents.stopFail" : action === "restart" ? "agents.restartFail" : "agents.startFail";
        toast(t(failKey), "error");
      }
      setActionLoading("");
    },
    [api, t, toast, loadAgents],
  );

  return (
    <div className="space-y-6">
      <Card>
        <CardHeader>
          <CardTitle className="text-base">{t("settings.agentBackend")}</CardTitle>
          <CardDescription>{t("settings.agentBackendDesc")}</CardDescription>
        </CardHeader>
        <CardContent>
          <div className="grid grid-cols-2 gap-4">
            {agents.map((a) => (
              <div
                key={a.id}
                className={`border rounded-lg p-4 ${a.status === "running" ? "border-success bg-success/10" : "border-muted"}`}
              >
                <div className="flex items-center gap-2 mb-2">
                  <span className={`w-2 h-2 rounded-full ${a.status === "running" ? "bg-success" : "bg-muted"}`} />
                  <span className="font-semibold text-sm">{a.name}</span>
                  <span className="text-[9px] text-muted-foreground ml-auto">
                    {agentDisplayName(a.id)}
                  </span>
                </div>
                <div className="text-[11px] text-muted-foreground space-y-1">
                  <div>
                    {t("settings.statusLabel")}:{" "}
                    {a.status === "running" ? t("settings.agentRunning") : t("settings.agentStopped")}
                  </div>
                  {agentMeta(a.id)?.portDescKey && <div>{t(agentMeta(a.id)!.portDescKey!)}</div>}
                  {agentMeta(a.id)?.backendDescKey && <div>{t(agentMeta(a.id)!.backendDescKey!)}</div>}
                  {a.tools !== undefined && <div>{t("settings.toolsCount", { count: a.tools })}</div>}
                  {a.capabilities?.supports_stream && <div>{t("settings.capStream")}</div>}
                  {a.capabilities?.supports_image && <div>{t("settings.capImage")}</div>}
                  {a.capabilities?.supports_tools && <div>{t("settings.capTools")}</div>}
                </div>
                {/* Task 6.15: start/stop/restart 控件 */}
                <div className="flex gap-1.5 mt-3">
                  <Button
                    variant="outline"
                    size="sm"
                    className="h-7 text-[11px] gap-1"
                    disabled={a.status === "running" || actionLoading.startsWith(`${a.id}:`)}
                    onClick={() => doAction(a.id, "start")}
                  >
                    <Play size={12} />
                    {t("agents.start")}
                  </Button>
                  <Button
                    variant="outline"
                    size="sm"
                    className="h-7 text-[11px] gap-1"
                    disabled={a.status !== "running" || actionLoading.startsWith(`${a.id}:`)}
                    onClick={() => doAction(a.id, "stop")}
                  >
                    <Square size={12} />
                    {t("agents.stop")}
                  </Button>
                  <Button
                    variant="outline"
                    size="sm"
                    className="h-7 text-[11px] gap-1"
                    disabled={a.status !== "running" || actionLoading.startsWith(`${a.id}:`)}
                    onClick={() => doAction(a.id, "restart")}
                  >
                    <RotateCw size={12} />
                    {t("agents.restart")}
                  </Button>
                </div>
              </div>
            ))}
            {agents.length === 0 && (
              <div className="col-span-2 text-center text-sm text-muted-foreground py-8">
                {t("settings.loadingAgents")}
              </div>
            )}
          </div>
        </CardContent>
      </Card>

      {/* OC 健康检测 (H2.2) — 复用 DependenciesPanel */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base">{t("settings.healthCheck")}</CardTitle>
          <CardDescription>{t("settings.healthCheckDesc")}</CardDescription>
        </CardHeader>
        <CardContent>
          <DependenciesPanel />
        </CardContent>
      </Card>
    </div>
  );
}
