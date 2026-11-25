import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, Legend } from "recharts";
import { useApi } from "../hooks/useApi";

interface AgentMonitoring {
  agents: Record<
    string,
    {
      status: string;
      pid?: number;
      cpu_percent: number;
      memory_mb: number;
      dropped_logs: number;
    }
  >;
  timestamp: number;
}

/**
 * Phase 7b: Agent 资源占用图表
 *
 * 展开 Agent 卡片时显示过去 1 小时 CPU/内存曲线（实时采样）。
 * 由于无历史数据存储，当前渲染实时快照。
 */
export function ResourceChart({ agentId }: { agentId: string }) {
  const { t } = useTranslation();
  const { api } = useApi();
  const [mon, setMon] = useState<AgentMonitoring | null>(null);
  const [history, setHistory] = useState<Array<{ time: string; cpu: number; mem: number }>>([]);

  useEffect(() => {
    const fetch = async () => {
      try {
        const data = await api<AgentMonitoring>("/api/v1/monitoring");
        setMon(data);
        const agent = data.agents?.[agentId];
        if (agent) {
          setHistory((prev) => {
            const next = [
              ...prev,
              {
                time: new Date().toLocaleTimeString("en-GB", { hour12: false }),
                cpu: agent.cpu_percent,
                mem: agent.memory_mb,
              },
            ];
            return next.slice(-60); // 保留最近 60 个采样点
          });
        }
      } catch {
        /* ignore */
      }
    };
    fetch();
    const timer = setInterval(fetch, 5000);
    return () => clearInterval(timer);
  }, [agentId, api]);

  const agent = mon?.agents?.[agentId];

  return (
    <div className="space-y-3">
      {/* 实时快照 */}
      {!agent || agent.status === "stopped" ? (
        <div className="text-xs text-muted-foreground py-3 text-center">{t("agents.noMonitor")}</div>
      ) : (
        <>
          <div className="grid grid-cols-3 gap-2 text-xs">
            <div className="bg-secondary rounded p-2 text-center">
              <div className="text-muted-foreground">{t("agents.cpu")}</div>
              <div className="font-semibold text-sm">{agent.cpu_percent}%</div>
            </div>
            <div className="bg-secondary rounded p-2 text-center">
              <div className="text-muted-foreground">{t("agents.memory")}</div>
              <div className="font-semibold text-sm">{agent.memory_mb} MB</div>
            </div>
            <div className="bg-secondary rounded p-2 text-center">
              <div className="text-muted-foreground">{t("agents.pid")}</div>
              <div className="font-semibold text-sm">{agent.pid || "-"}</div>
            </div>
          </div>
          {/* 历史曲线 */}
          {history.length > 1 && (
            <div className="border rounded p-3">
              <h3 className="text-xs font-semibold text-muted-foreground mb-2">{t("agents.resourceChart")}</h3>
              <ResponsiveContainer width="100%" height={180}>
                <LineChart data={history}>
                  <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
                  <XAxis dataKey="time" tick={{ fontSize: 10 }} interval={9} />
                  <YAxis tick={{ fontSize: 10 }} />
                  <Tooltip />
                  <Legend wrapperStyle={{ fontSize: 10 }} />
                  <Line
                    type="monotone"
                    dataKey="cpu"
                    stroke="var(--primary)"
                    name="CPU %"
                    dot={false}
                    strokeWidth={1.5}
                  />
                  <Line
                    type="monotone"
                    dataKey="mem"
                    stroke="var(--success)"
                    name={t("resourceChart.memMb")}
                    dot={false}
                    strokeWidth={1.5}
                  />
                </LineChart>
              </ResponsiveContainer>
            </div>
          )}
        </>
      )}
    </div>
  );
}
