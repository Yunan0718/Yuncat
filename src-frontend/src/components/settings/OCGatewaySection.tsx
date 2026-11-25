import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useApi } from "../../hooks/useApi";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../ui/card";
import { Label } from "../ui/label";
import { Input } from "../ui/input";
import { Switch } from "../ui/switch";
import { useToast } from "../../hooks/use-toast";

/**
 * OC 可配置工具列表——与 config/tools/openclaw.yaml 中的工具名对齐。
 * 这层硬编码是 UI 配置层需要知道"有哪些工具可配置"——对标 VS Code settings UI
 * 也硬编码可配置项列表（实际工具定义仍在 YAML 单源）。
 */
const OC_CONFIGURABLE_TOOLS = [
  "web_search",
  "web_fetch",
  "openclaw_vision",
  "pdf_extract",
  "github_graphql",
  "telegram_send",
  "discord_send",
  "slack_send",
  "dingtalk_send",
  "wechat_work_send",
  "whatsapp_send",
];

/* ── OC Gateway (H2.1 · H2.4 · Task 2.12 · Task 6.1) ──────────────────── */
export function OCGatewaySection() {
  const { t } = useTranslation();
  const { api } = useApi();
  const { toast } = useToast();
  const [gatewayStatus, setGatewayStatus] = useState<string>(t("settings.checking"));
  const [gatewayRunning, setGatewayRunning] = useState<boolean | null>(null);
  const [actionLoading, setActionLoading] = useState(false);
  // Task 2.12: 工具开关绑定 state——原 defaultChecked 无 onChange 是装饰 UI
  const [disabledTools, setDisabledTools] = useState<Set<string>>(new Set());
  const [toolsLoading, setToolsLoading] = useState(true);

  // t 只用于构造提示文案，不属于"数据依赖"：若把它列进 refreshStatus 的依赖，
  // 切一次语言就会重新轮询一次网关状态。用 latest-ref 读取当前 t，既能保证
  // 文案跟随最新语言，又让 callback 引用稳定、依赖只留真正的数据源。
  const tRef = useRef(t);
  tRef.current = t;

  // 用 useCallback 固定引用：它既在下面的 effect 里调用，也作为按钮回调（第 91 行
  // 附近的 await）。若保持普通函数，effect 的依赖只能写成 [api, t] 去"间接命中"
  // refreshStatus 的真实依赖，规则校验不到，将来给它加状态时就会漏更新。
  const refreshStatus = useCallback(
    () =>
      api<{ status: string; port: number; version?: string }>("/api/v1/agents/openclaw/status")
        .then((r) => {
          const running = r.status === "running";
          setGatewayRunning(running);
          setGatewayStatus(
            running
              ? tRef.current("settings.gatewayRunning", { port: r.port, version: r.version || "" })
              : tRef.current("settings.gatewayStopped"),
          );
        })
        .catch(() => {
          setGatewayRunning(false);
          setGatewayStatus(tRef.current("settings.gatewayStopped"));
        }),
    [api],
  );

  useEffect(() => {
    refreshStatus();
  }, [refreshStatus]);

  // Task 2.12: 加载已禁用工具列表
  useEffect(() => {
    api<Record<string, string>>("/api/v1/settings")
      .then((s) => {
        const raw = s.oc_tools_disabled;
        if (raw) {
          try {
            const arr = JSON.parse(raw);
            if (Array.isArray(arr)) setDisabledTools(new Set(arr.filter((x) => typeof x === "string")));
          } catch {
            // 损坏的数据——视为空
          }
        }
      })
      .catch(() => {
        // 设置加载失败——保持全部启用（默认安全行为）
      })
      .finally(() => setToolsLoading(false));
  }, [api]);

  const toggleGateway = async () => {
    if (gatewayRunning === null || actionLoading) return;
    const nextRunning = !gatewayRunning;
    const action = nextRunning ? "start" : "stop";
    setActionLoading(true);
    // 乐观更新 UI，失败回滚
    setGatewayRunning(nextRunning);
    setGatewayStatus(nextRunning ? t("settings.gatewayStarting") : t("settings.gatewayStopping"));
    try {
      await api(`/api/v1/agents/openclaw/${action}`, { method: "POST" });
      await refreshStatus();
      toast(t(nextRunning ? "settings.gatewayStart" : "settings.gatewayStop"), "success");
    } catch {
      setGatewayRunning(gatewayRunning);
      setGatewayStatus(gatewayRunning ? t("settings.gatewayRunning") : t("settings.gatewayStopped"));
      toast(t(nextRunning ? "settings.gatewayStartFailed" : "settings.gatewayStopFailed"), "error");
    } finally {
      setActionLoading(false);
    }
  };

  const toggleTool = async (tool: string) => {
    const next = new Set(disabledTools);
    if (next.has(tool)) next.delete(tool);
    else next.add(tool);
    setDisabledTools(next);
    try {
      await api("/api/v1/settings", {
        method: "POST",
        body: JSON.stringify({ key: "oc_tools_disabled", value: JSON.stringify([...next]) }),
      });
    } catch {
      // 回滚 + 提示
      setDisabledTools(disabledTools);
      toast(t("settings.toolToggleFailed"), "error");
    }
  };

  const gatewayStopped = gatewayRunning === false;
  const toolsDisabled = toolsLoading || gatewayStopped || actionLoading;

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader>
          <CardTitle className="text-base">{t("phase49.settings.openClawGateway")}</CardTitle>
          <CardDescription>{t("settings.ocGatewayDesc")}</CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="flex items-center justify-between">
            <Label className="text-sm">{t("settings.gatewayStatus")}</Label>
            <div className="flex items-center gap-2">
              <span className="text-xs font-mono">{gatewayStatus}</span>
              {/* Task 6.1: Gateway 启停总开关 */}
              <Switch
                checked={gatewayRunning === true}
                onCheckedChange={toggleGateway}
                disabled={gatewayRunning === null || actionLoading}
                aria-label={t("settings.gatewayStatus")}
                size="sm"
              />
            </div>
          </div>
          <div className="space-y-1.5">
            <Label className="text-xs">{t("settings.gatewayPort")}</Label>
            <Input defaultValue="18789" className="font-mono text-sm" disabled placeholder="18789" />
            <p className="text-[10px] text-muted-foreground">{t("settings.gatewayPortHint")}</p>
          </div>
          <div className="space-y-1.5">
            <Label className="text-xs">{t("settings.toolSwitches")}</Label>
            <div className={`grid grid-cols-2 gap-1 text-[11px] ${toolsDisabled ? "opacity-60" : ""}`}>
              {OC_CONFIGURABLE_TOOLS.map((tool) => (
                <label key={tool} className="flex items-center gap-1.5 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={!disabledTools.has(tool)}
                    onChange={() => toggleTool(tool)}
                    disabled={toolsDisabled}
                    className="accent-primary"
                  />
                  {tool}
                </label>
              ))}
            </div>
            {gatewayStopped && (
              <p className="text-[10px] text-warning">{t("settings.gatewayToolsDisabledHint")}</p>
            )}
            {!gatewayStopped && <p className="text-[10px] text-muted-foreground">{t("settings.toolSwitchesHint")}</p>}
          </div>
          <div className="space-y-1.5">
            <Label className="text-xs">{t("settings.ocWorkDir")}</Label>
            <Input className="font-mono text-[11px]" disabled value={`${"~"}/.openclaw/agents/main/`} />
            <p className="text-[10px] text-muted-foreground">MEMORY.md · sessions/ · skills/ · config/</p>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
