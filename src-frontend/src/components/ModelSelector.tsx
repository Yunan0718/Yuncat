import { useState, useEffect } from "react";
import { ChevronDown, Check, Settings } from "lucide-react";
import { useTranslation } from "react-i18next";
import { useApi } from "../hooks/useApi";
import { useToast } from "../hooks/use-toast";
import { HealthDot } from "./common/StatusDot";
import { LoadingCurve } from "./common/LoadingCurve";
import type { ProviderInfo } from "./settings/ModelConfigPanel";
import { FocusTrap } from "./FocusTrap";
import { useModelReasoning } from "../hooks/useModelReasoning";
import { effortDescKey, effortLabelKey } from "../lib/reasoningLevels";
import { useAppStore, type ReasoningEffort } from "../store/appStore";

interface ModelSelectorProps {
  variant?: "chat" | "taskcenter";
}

interface ActiveModelConfig {
  provider: string;
  api_key_configured?: boolean;
  credential_state?: "ok" | "missing" | "unreadable";
  has_stored_ciphertext?: boolean;
}

export default function ModelSelector({ variant = "chat" }: ModelSelectorProps) {
  const { t } = useTranslation();
  const { api } = useApi();
  const { toast } = useToast();
  const [providers, setProviders] = useState<ProviderInfo[]>([]);
  const [active, setActive] = useState("");
  const [activeCfg, setActiveCfg] = useState<ActiveModelConfig | null>(null);
  const [open, setOpen] = useState(false);
  const [switching, setSwitching] = useState(false);
  // 2026-10-07：默认只列出已配置的模型——未配置的 16 家全铺开会让真正能用的那个淹没在灰名单里。
  const [showAll, setShowAll] = useState(false);
  const { info: reasoning } = useModelReasoning(api);
  const reasoningEffort = useAppStore((s) => s.reasoningEffort);
  const reasoningEffortSource = useAppStore((s) => s.reasoningEffortSource);
  const setReasoningEffort = useAppStore((s) => s.setReasoningEffort);
  const resetReasoningEffort = useAppStore((s) => s.resetReasoningEffort);
  const capabilityLevel = useAppStore((s) => s.capabilityLevel);
  const setMaxMode = useAppStore((s) => s.setMaxMode);
  const isMax = capabilityLevel >= 2;

  useEffect(() => {
    api<ProviderInfo[]>("/api/v1/models")
      .then((list) => {
        if (Array.isArray(list)) setProviders(list);
      })
      .catch((e) => console.warn("ModelSelector: providers load failed:", e));
    api<ActiveModelConfig>("/api/v1/models/active")
      .then((a) => {
        if (a?.provider) setActive(a.provider);
        setActiveCfg(a || null);
      })
      .catch((e) => console.warn("ModelSelector: active model load failed:", e));
  }, [api]);

  const activeProvider = Array.isArray(providers) ? providers.find((p) => p.provider === active) : undefined;

  // 已配置 = 后端确认"能解密"（api_key_configured 的语义见 provider_key_state）。
  const providerList = Array.isArray(providers) ? providers : [];
  const configuredProviders = providerList.filter((p) => p.api_key_configured);
  // 当前激活的模型永远可见——即便它的 key 刚被清掉，也要让用户看到自己选的是什么。
  const visibleProviders = showAll
    ? providerList
    : providerList.filter((p) => p.api_key_configured || p.provider === active);
  // 折叠入口的计数必须按"折叠时会被藏起来多少"算——用 visibleProviders 反推会让
  // 展开后 hiddenCount 变成 0，按钮随之消失，用户再也收不回去。
  const unconfiguredCount = providerList.length - configuredProviders.length;
  // 本机密钥文件丢失/不可读 + 库里确实存着密文 = 已保存的 Key 解不开。
  // 这时不是"没填"，界面必须直说，否则用户只会反复重填。
  const credentialBroken = Boolean(
    activeCfg
      && activeCfg.credential_state
      && activeCfg.credential_state !== "ok"
      && activeCfg.has_stored_ciphertext
      && !activeCfg.api_key_configured,
  );

  const handleSwitch = async (provider: string) => {
    if (provider === active || switching) return;
    setSwitching(true);
    const providerName = providers.find((p) => p.provider === provider)?.name || provider;
    toast(t("modelSelector.switching", { name: providerName }), "info");
    try {
      const result = await api<{
        status: string;
        active: string;
        provider_name: string;
        model: string;
        model_name: string;
        context_window: number;
        supports: string[];
      }>(`/api/v1/models/${provider}/activate`, { method: "POST" });
      setActive(provider);
      const info = providers.find((p) => p.provider === provider);
      const modelInfo = result.model_name || info?.model || result.model;
      const ctx = result.context_window ? `${(result.context_window / 1000).toFixed(0)}K` : "";
      toast(
        t("modelSelector.switched", {
          name: result.provider_name || info?.name || provider,
          model: modelInfo,
          ctx: ctx || "",
        }),
        "success",
      );
    } catch {
      toast(t("modelSelector.switchFailed"), "error");
    }
    setSwitching(false);
    setOpen(false);
  };

  // 健康状态指示器——状态同时以无障碍文本给出，不能只靠颜色
  const healthDot = (status: string) => <HealthDot status={status} />;

  if (variant === "taskcenter") {
    // TaskCenter 风格 — 紧凑 native select
    return (
      <select
        value={active}
        onChange={(e) => handleSwitch(e.target.value)}
        title={t("modelSelector.currentModelTooltip", {
          name: activeProvider?.name || t("modelSelector.notSelected"),
          count: configuredProviders.length,
        })}
        className="text-[11px] pl-2 pr-5 py-0.5 border border-border rounded bg-background max-w-[110px] truncate"
        disabled={switching}
      >
        {visibleProviders.map((p) => (
          <option key={p.provider} value={p.provider}>
            {p.api_key_configured ? "● " : "○ "}
            {p.name}
          </option>
        ))}
      </select>
    );
  }

  // ChatView 风格 — shadcn-style dropdown
  return (
    <div className="relative">
      <button
        onClick={() => setOpen(!open)}
        disabled={switching}
        className="flex items-center gap-1 h-7 px-2 text-[11px] rounded-md bg-secondary/70 hover:bg-secondary transition-colors"
        title={t("modelSelector.currentTooltip", {
          name: activeProvider?.name || t("modelSelector.notSelected"),
          model: activeProvider?.model || "",
          configured: configuredProviders.length,
          total: providerList.length,
        })}
      >
        {switching ? (
          <LoadingCurve variant="lissajous" size={16} particleCount={6} />
        ) : (
          healthDot(activeProvider?.health_status || "unconfigured")
        )}
        <span className="max-w-[80px] truncate">{activeProvider?.name || t("modelSelector.selectModel")}</span>
        {/* 2026-09-17: 输入区只显示“模型名 + 思考档位”；开了 MAX 多一个标记 */}
        <span
          className={`text-[9px] shrink-0 ${reasoningEffortSource === "manual" ? "text-accent" : "text-muted-foreground"}`}
          data-testid="effort-inline"
        >
          {t(effortLabelKey(reasoningEffort))}
        </span>
        {isMax && (
          <span
            className="text-[8px] px-1 rounded bg-primary/10 text-primary shrink-0 font-medium"
            data-testid="max-chip"
          >
            MAX
          </span>
        )}
        <ChevronDown size={10} className={`transition-transform ${open ? "rotate-180" : ""}`} />
      </button>

      {credentialBroken && (
        <div
          data-testid="credential-warning"
          className="absolute top-full left-0 mt-1 z-dropdown w-64 rounded-md border border-destructive/40 bg-card px-2.5 py-2 shadow-lg"
        >
          <div className="text-[10px] font-medium text-destructive">
            {t("modelSelector.credentialBrokenTitle")}
          </div>
          <div className="mt-0.5 text-[10px] leading-relaxed text-muted-foreground">
            {t("modelSelector.credentialBrokenHint")}
          </div>
          <a
            href="#/settings"
            className="mt-1 inline-flex items-center gap-1 text-[10px] text-primary hover:underline"
          >
            <Settings size={10} />
            {t("modelSelector.openSettings")}
          </a>
        </div>
      )}

      {open && (
        <>
          <div className="fixed inset-0 z-backdrop" onClick={() => setOpen(false)} />
          <FocusTrap active={open}>
          {/* 向上弹：输入区在底部，向下会把输入框盖住 */}
          <div className="absolute bottom-full left-0 mb-1 z-dropdown w-64 bg-card border border-border rounded-lg shadow-lg overflow-hidden">
            {/* 当前模型信息 */}
            {activeProvider && (
              <div className="px-3 py-2 border-b border-border bg-secondary/30">
                <div className="flex items-center gap-1.5">
                  {healthDot(activeProvider.health_status || "unconfigured")}
                  <span className="text-[11px] font-semibold">{activeProvider.name}</span>
                  <span className="text-[9px] text-muted-foreground ml-auto">{activeProvider.model}</span>
                </div>
                {activeProvider.models.find((m) => m.id === activeProvider.model) && (
                  <div className="mt-1 text-[9px] text-muted-foreground">
                    {t("modelSelector.context", {
                      context: `${(activeProvider.models.find((m) => m.id === activeProvider.model)!.context / 1000).toFixed(0)}K`,
                    })}
                    {activeProvider.models.find((m) => m.id === activeProvider.model)!.supports?.length
                      ? ` · ${activeProvider.models.find((m) => m.id === activeProvider.model)!.supports!.join(" / ")}`
                      : ""}
                  </div>
                )}
              </div>
            )}

            {/* Provider 列表——默认只列已配置的（+ 当前激活项），其余折叠 */}
            {visibleProviders.length === 0 ? (
              <div className="px-3 py-3 text-[10px] leading-relaxed text-muted-foreground">
                <div className="text-foreground">{t("modelSelector.noConfiguredModel")}</div>
                <div className="mt-0.5">{t("modelSelector.noConfiguredHint")}</div>
              </div>
            ) : (
            <div className="max-h-64 overflow-y-auto py-1">
              {visibleProviders.map((p) => {
                const isActive = p.provider === active;
                return (
                  <button
                    key={p.provider}
                    onClick={() => {
                      if (!isActive) handleSwitch(p.provider);
                    }}
                    disabled={isActive}
                    className={`w-full flex items-center gap-2 px-3 py-1.5 text-[11px] transition-colors
                      ${isActive ? "bg-primary/10 text-primary cursor-default" : "hover:bg-secondary text-foreground"}
                      ${!p.api_key_configured && !isActive ? "opacity-50" : ""}`}
                  >
                    {healthDot(p.health_status || (p.api_key_configured ? "configured" : "unconfigured"))}
                    <span className="flex-1 text-left truncate">{p.name}</span>
                    <span className="text-[9px] text-muted-foreground shrink-0">
                      {p.api_key_configured
                        ? p.model || t("modelSelector.configured")
                        : t("modelSelector.notConfigured")}
                    </span>
                    {isActive && <Check size={12} className="text-primary shrink-0" />}
                  </button>
                );
              })}
            </div>
            )}

            {/* 未配置的供应商折叠入口——需要时再展开，不占默认视野 */}
            {unconfiguredCount > 0 && (
              <button
                onClick={() => setShowAll(!showAll)}
                className="w-full flex items-center justify-center gap-1 py-1.5 border-t border-border text-[10px] text-muted-foreground hover:text-foreground hover:bg-secondary/50 transition-colors"
                data-testid="toggle-unconfigured"
              >
                <span>{showAll
                  ? t("modelSelector.hideUnconfigured")
                  : t("modelSelector.showUnconfigured", { count: unconfiguredCount })}</span>
                <ChevronDown size={10} className={`transition-transform ${showAll ? "rotate-180" : ""}`} />
              </button>
            )}

            {/* 2026-09-17: 思考强度——只列当前模型真实支持的档位（对标 Codex 把模型和强度放在同一个选择里） */}
            <div className="border-t border-border" data-testid="model-effort-section">
              <div className="px-3 pt-1.5 pb-1 flex items-center justify-between text-[9px] text-muted-foreground">
                <span>{t("modelSelector.effortTitle")}</span>
                <span data-testid="effort-current">
                  {reasoningEffortSource === "auto"
                    ? t("chat.effortFollowCap")
                    : t(effortLabelKey(reasoningEffort))}
                </span>
              </div>
              {!reasoning.effortSupported || reasoning.efforts.length === 0 ? (
                <div className="px-3 pb-2 text-[10px] text-muted-foreground">
                  {t("modelSelector.effortUnsupported")}
                </div>
              ) : (
                <div className="pb-1">
                  <button
                    onClick={() => resetReasoningEffort()}
                    className={`w-full flex items-center gap-2 px-3 py-1 text-[11px] transition-colors hover:bg-secondary ${
                      reasoningEffortSource === "auto" ? "text-primary" : "text-foreground"
                    }`}
                    data-testid="effort-follow"
                  >
                    <span className="flex-1 text-left">{t("chat.effortFollowCap")}</span>
                    {reasoningEffortSource === "auto" && <Check size={12} className="text-primary shrink-0" />}
                  </button>
                  {reasoning.efforts.map((level) => {
                    const selected = reasoningEffortSource === "manual" && reasoningEffort === level;
                    return (
                      <button
                        key={level}
                        onClick={() => {
                          setReasoningEffort(level as ReasoningEffort);
                          setOpen(false);
                        }}
                        className={`w-full flex items-center gap-2 px-3 py-1 text-[11px] transition-colors hover:bg-secondary ${
                          selected ? "text-primary" : "text-foreground"
                        }`}
                        data-testid={`effort-option-${level}`}
                      >
                        <span className="shrink-0">{t(effortLabelKey(level))}</span>
                        <span className="flex-1 text-left text-[9px] text-muted-foreground truncate">
                          {t(effortDescKey(level))}
                        </span>
                        {selected && <Check size={12} className="text-primary shrink-0" />}
                      </button>
                    );
                  })}
                </div>
              )}
            </div>

            {/* 2026-09-17: 能力档位只有两档——默认 / MAX（一个开关） */}
            <div className="border-t border-border" data-testid="capability-section">
              <div className="px-3 pt-1.5 text-[9px] text-muted-foreground">
                {t("modelSelector.capabilityTitle")}
              </div>
              <button
                onClick={() => {
                  setMaxMode(!isMax);
                  setOpen(false);
                }}
                className={`w-full flex items-center gap-2 px-3 py-1 text-[11px] transition-colors hover:bg-secondary ${
                  isMax ? "text-primary" : "text-foreground"
                }`}
                data-testid="capability-max-toggle"
              >
                <span className="flex-1 text-left">MAX</span>
                <span className="text-[9px] text-muted-foreground">
                  {isMax ? t("chat.maxOn") : t("chat.maxOff")}
                </span>
                {isMax && <Check size={12} className="text-primary shrink-0" />}
              </button>
              <div className="px-3 pb-2 text-[9px] leading-relaxed text-muted-foreground">
                {t("modelSelector.capabilityDesc")}
              </div>
            </div>

            {/* Footer */}
            <div className="px-3 py-1.5 border-t border-border bg-secondary/20">
              <a
                href="#/settings"
                className="flex items-center gap-1 text-[10px] text-muted-foreground hover:text-foreground transition-colors"
              >
                <Settings size={10} />
                {t("modelSelector.openSettings")}
              </a>
            </div>
          </div>
          </FocusTrap>
        </>
      )}
    </div>
  );
}
