import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { useTranslation } from "react-i18next";
import { useApi } from "../hooks/useApi";
import { useAppStore } from "../store/appStore";
import { useLanguage, LANGUAGES } from "../i18n";
import { LoadingCurve } from "./common/LoadingCurve";
import { ProviderIcon } from "./ProviderIcon";
import { Button } from "./ui/button";
import { FocusTrap } from "./FocusTrap";
import { Input } from "./ui/input";
import {
  Check,
  ChevronLeft,
  ChevronRight,
  Folder,
  Languages,
  Moon,
  Palette,
  Puzzle,
  Rocket,
  Sparkles,
  Sun,
  Monitor,
  KeyRound,
  Cpu,
  Save,
  TestTube,
  AlertTriangle,
} from "lucide-react";

type Theme = "light" | "dark" | "system";

const STORAGE_COMPLETE = "yuncat-onboarding-complete";
const STORAGE_STEP = "yuncat-onboarding-step";
const STORAGE_DRAFT = "yuncat-onboarding-draft";

type Density = "compact" | "comfortable";

interface ProviderInfo {
  provider: string;
  name: string;
  base_url: string;
  model: string;
  api_key_configured: boolean;
}

const DENSITIES: { key: Density; icon: React.ReactNode; labelKey: string }[] = [
  { key: "compact", icon: <div className="w-4 h-4 border-y-2 border-current" />, labelKey: "onboard.densityCompact" },
  {
    key: "comfortable",
    icon: <div className="w-4 h-4 border-y-4 border-current" />,
    labelKey: "onboard.densityComfortable",
  },
];

export function OnboardingWizard() {
  const { t } = useTranslation();
  const { api } = useApi();
  const { current: lang, setLanguage } = useLanguage();
  const store = useAppStore();

  const [dismissed, setDismissed] = useState(false);
  const [step, setStep] = useState(0);
  const [transitioning, setTransitioning] = useState(false);

  // Step 0: appearance
  const [theme, setThemeState] = useState<Theme>(store.theme);
  const [density, setDensityState] = useState<Density>(store.uiDensity);

  // Step 1: workspace
  const [workspace, setWorkspace] = useState("");

  // Step 2: model
  const [providers, setProviders] = useState<ProviderInfo[]>([]);
  const [selectedProvider, setSelectedProvider] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [model, setModel] = useState("");
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<string | null>(null);
  const [testOk, setTestOk] = useState<boolean | null>(null);
  // P2: 已成功保存过的 key 快照（apiKey 与之不同即视为未保存）；保存中标记
  const [savedKey, setSavedKey] = useState("");
  const [savingKey, setSavingKey] = useState(false);
  // P2: 完成步骤展示的“无可用模型”警告
  const [noModelConfigured, setNoModelConfigured] = useState(false);

  // 2026-09-10: 移除 Agent 选择步骤——默认原生 Yuncat（外部 Agent 在 Agent 管理页按需启用）。
  // 不再向后端写 default_agent；后端默认即 yuncat，外部 Agent 也被 EXTERNAL_AGENT_IDS 门控拦截。
  const totalSteps = 4;
  const isLast = step === totalSteps - 1;
  // 各步骤名称 key——进度条旁展示"第 n / 4 步 · 模型配置"式的主流克制感
  const stepNameKeys = [
    "onboard.welcome",
    "onboard.workspace",
    "onboard.configKey",
    "onboard.ready",
  ];

  // 恢复进度
  useEffect(() => {
    try {
      const savedStep = localStorage.getItem(STORAGE_STEP);
      if (savedStep) setStep(Math.max(0, Math.min(totalSteps - 1, Number(savedStep))));
      const draft = localStorage.getItem(STORAGE_DRAFT);
      if (draft) {
        const d = JSON.parse(draft);
        if (d.theme) setThemeState(d.theme);
        if (d.density) setDensityState(d.density);
        if (d.workspace) setWorkspace(d.workspace);
        if (d.selectedProvider) setSelectedProvider(d.selectedProvider);
        // 安全：API Key 不写入 localStorage，避免明文泄露
        if (d.model) setModel(d.model);
      }
    } catch {
      /* ignore */
    }
  }, []);

  // 持久化当前进度
  useEffect(() => {
    try {
      localStorage.setItem(STORAGE_STEP, String(step));
      // 注意：apiKey 故意不保存到 localStorage，由后端加密存储
      localStorage.setItem(
        STORAGE_DRAFT,
        JSON.stringify({
          theme,
          density,
          workspace,
          selectedProvider,
          model,
        }),
      );
    } catch {
      /* ignore */
    }
  }, [step, theme, density, workspace, selectedProvider, model]);

  // 加载模型供应商列表
  useEffect(() => {
    if (step !== 2) return;
    api<ProviderInfo[]>("/api/v1/models")
      .then((list) => {
        if (!Array.isArray(list)) return;
        setProviders(list);
        const active = list.find((p) => p.api_key_configured) || list[0];
        if (active) {
          setSelectedProvider(active.provider);
          setModel(active.model);
        }
      })
      .catch((e) => console.warn("OnboardingWizard: models load failed:", e));
  }, [step, api]);

  // P2: 完成步骤重新拉取模型列表，判断是否存在已配置可用的模型
  useEffect(() => {
    if (step !== totalSteps - 1) return;
    api<ProviderInfo[]>("/api/v1/models")
      .then((list) => {
        if (Array.isArray(list)) setNoModelConfigured(!list.some((p) => p.api_key_configured));
      })
      .catch((e) => console.warn("OnboardingWizard: models recheck failed:", e));
  }, [step, api]);

  const setTheme = (v: Theme) => {
    setThemeState(v);
    store.setTheme(v);
  };
  const setDensity = (v: Density) => {
    setDensityState(v);
    store.setUiDensity(v);
  };

  const saveModel = async (): Promise<boolean> => {
    if (!selectedProvider || !apiKey) return false;
    const body: Record<string, string> = { api_key: apiKey };
    if (model) body.model = model;
    try {
      await api(`/api/v1/models/${selectedProvider}`, { method: "PUT", body: JSON.stringify(body) });
      await api(`/api/v1/models/${selectedProvider}/activate`, { method: "POST" });
      setSavedKey(apiKey);
      setTestOk(true);
      setTestResult(t("onboard.keySaved"));
      return true;
    } catch (e: unknown) {
      setTestOk(false);
      setTestResult(`${t("settings.saveFailed")}: ${e instanceof Error ? e.message : String(e)}`);
      return false;
    }
  };

  const testConnection = async () => {
    const p = providers.find((x) => x.provider === selectedProvider);
    if (!p) return;
    setTesting(true);
    setTestResult(null);
    setTestOk(null);
    try {
      const res = await api<{ ok: boolean; status: number; error?: string }>("/api/v1/models/test", {
        method: "POST",
        body: JSON.stringify({ base_url: p.base_url, provider: p.provider, api_key: apiKey }),
      });
      setTestOk(res.ok);
      setTestResult(
        res.ok ? t("onboard.connectionOk") : `${t("onboard.connectionFailed")}: ${res.status} ${res.error || ""}`,
      );
    } catch {
      setTestOk(false);
      setTestResult(t("onboard.connectionFailed"));
    }
    setTesting(false);
  };

  const pickWorkspace = async () => {
    try {
      const res = await window.yuncat?.openFileDialog?.({ selectFolder: true, title: t("onboard.chooseWorkspace") });
      if (res?.ok && !res.canceled && res.path) {
        const path = Array.isArray(res.path) ? res.path[0] : res.path;
        setWorkspace(path);
        await api("/api/v1/workspace/files/root", { method: "POST", body: JSON.stringify({ path }) });
      }
    } catch {
      /* ignore */
    }
  };

  const finish = async () => {
    try {
      // 2026-09-10: Agent 选择步骤已移除——默认原生 Yuncat，不再写 default_agent。
      localStorage.setItem(STORAGE_COMPLETE, "true");
      localStorage.removeItem(STORAGE_STEP);
      localStorage.removeItem(STORAGE_DRAFT);
    } catch {
      /* ignore */
    }
    setDismissed(true);
  };

  const changeStep = (next: number) => {
    if (next === step || next < 0 || next >= totalSteps) return;
    setTransitioning(true);
    setTimeout(() => {
      setStep(next);
      setTransitioning(false);
    }, 360);
  };

  // P2: Next 前置校验——Step2 输入了未保存的 API Key 时先自动保存，失败则阻止切步
  const handleNext = async () => {
    if (isLast) {
      finish();
      return;
    }
    if (step === 2 && apiKey && apiKey !== savedKey) {
      setSavingKey(true);
      const ok = await saveModel();
      setSavingKey(false);
      if (!ok) return; // 保存失败：停留在当前步，错误信息由 testResult 展示
    }
    changeStep(step + 1);
  };

  const shouldShow =
    !dismissed &&
    (() => {
      try {
        return localStorage.getItem(STORAGE_COMPLETE) !== "true";
      } catch {
        return true;
      }
    })();

  if (!shouldShow) return null;

  return createPortal(
    <FocusTrap active>
      <div
        className="fixed inset-0 bg-black/60 z-modal flex items-center justify-center p-4"
        role="dialog"
        aria-modal="true"
        aria-label={t("onboard.welcome")}
      >
      <div className="bg-background rounded-2xl shadow-xl w-full max-w-[560px] max-h-[90vh] overflow-hidden flex flex-col">
        {/* Header progress */}
        <div className="px-6 pt-6 pb-2">
          <div className="flex items-center justify-between mb-4">
            <h1 className="text-lg font-bold flex items-center gap-2">
              <Sparkles size={18} className="text-primary" />
              Yuncat
            </h1>
            <span className="text-xs text-muted-foreground">
              {t("onboard.step", { n: step + 1, total: totalSteps })}
              <span className="ml-1.5 text-muted-foreground/70">· {t(stepNameKeys[step])}</span>
            </span>
          </div>
          <div className="flex gap-1.5">
            {/* eslint-disable react/no-array-index-key */}
            {Array.from({ length: totalSteps }).map((_, i) => (
              <div
                key={`onboard-bar-${i}`}
                className={`h-1.5 flex-1 rounded-full transition-colors ${i <= step ? "bg-primary" : "bg-muted"}`}
              />
            ))}
            {/* eslint-enable react/no-array-index-key */}
          </div>
        </div>

        {/* Transition overlay */}
        <div className="relative flex-1 overflow-y-auto px-6 py-4 min-h-[280px]">
          {transitioning && (
            <div className="absolute inset-0 flex items-center justify-center bg-background/90 z-10">
              <LoadingCurve variant="rose" size={96} speed={1.5} />
            </div>
          )}

          <div className={`transition-opacity duration-300 ${transitioning ? "opacity-0" : "opacity-100"}`}>
            {step === 0 && (
              <StepWelcome
                t={t}
                lang={lang}
                setLanguage={setLanguage}
                theme={theme}
                setTheme={setTheme}
                density={density}
                setDensity={setDensity}
              />
            )}
            {step === 1 && <StepWorkspace t={t} workspace={workspace} onPick={pickWorkspace} />}
            {step === 2 && (
              <StepModel
                t={t}
                providers={providers}
                selectedProvider={selectedProvider}
                setSelectedProvider={setSelectedProvider}
                apiKey={apiKey}
                setApiKey={setApiKey}
                model={model}
                setModel={setModel}
                testing={testing}
                testResult={testResult}
                testOk={testOk}
                onTest={testConnection}
                onSave={saveModel}
              />
            )}
            {step === 3 && <StepComplete t={t} warning={noModelConfigured} />}
          </div>
        </div>

        {/* Footer */}
        <div className="px-6 py-4 border-t flex items-center justify-between bg-muted/20">
          <Button
            variant="ghost"
            size="sm"
            onClick={() => changeStep(step - 1)}
            disabled={step === 0}
            className="text-muted-foreground hover:text-foreground disabled:opacity-30"
          >
            <ChevronLeft size={14} />
            {t("onboard.prev")}
          </Button>
          <div className="flex gap-2">
            <Button variant="ghost" size="sm" onClick={finish}>
              {t("onboard.skip")}
            </Button>
            <Button onClick={() => handleNext()} disabled={savingKey}>
              {savingKey ? <LoadingCurve size={14} particleCount={8} /> : null}
              {isLast ? t("onboard.start") : t("onboard.next")}
              {!isLast && !savingKey && <ChevronRight size={14} />}
            </Button>
          </div>
        </div>
      </div>
    </div>
    </FocusTrap>,
    document.getElementById("overlay-root")!,
  );
}

function StepWelcome({
  t,
  lang,
  setLanguage,
  theme,
  setTheme,
  density,
  setDensity,
}: {
  t: (k: string, opts?: Record<string, unknown>) => string;
  lang: string;
  setLanguage: (v: string) => void;
  theme: Theme;
  setTheme: (v: Theme) => void;
  density: Density;
  setDensity: (v: Density) => void;
}) {
  return (
    <div className="space-y-5">
      <div className="text-center space-y-2">
        <h2 className="text-xl font-bold">{t("onboard.welcome")}</h2>
        <p className="text-sm text-muted-foreground">{t("onboard.welcomeDesc")}</p>
      </div>

      <div className="space-y-3">
        <label className="text-xs font-medium text-muted-foreground flex items-center gap-1.5">
          <Languages size={12} />
          {t("onboard.language")}
        </label>
        <div className="grid grid-cols-2 gap-2">
          {LANGUAGES.map((l) => (
            <Button
              key={l.code}
              variant={lang === l.code ? "default" : "outline"}
              size="sm"
              onClick={() => setLanguage(l.code)}
              className="justify-center"
            >
              {l.label}
            </Button>
          ))}
        </div>
      </div>

      <div className="space-y-3">
        <label className="text-xs font-medium text-muted-foreground flex items-center gap-1.5">
          <Palette size={12} />
          {t("onboard.theme")}
        </label>
        <div className="grid grid-cols-3 gap-2">
          {(["light", "dark", "system"] as Theme[]).map((th) => (
            <Button
              key={th}
              variant={theme === th ? "default" : "outline"}
              size="sm"
              onClick={() => setTheme(th)}
              className="justify-center capitalize"
            >
              {th === "light" ? <Sun size={14} /> : th === "dark" ? <Moon size={14} /> : <Monitor size={14} />}
              {t(`onboard.theme_${th}`)}
            </Button>
          ))}
        </div>
      </div>

      <div className="space-y-3">
        <label className="text-xs font-medium text-muted-foreground flex items-center gap-1.5">
          <Puzzle size={12} />
          {t("onboard.density")}
        </label>
        <div className="grid grid-cols-3 gap-2">
          {DENSITIES.map((d) => (
            <Button
              key={d.key}
              variant={density === d.key ? "default" : "outline"}
              size="sm"
              onClick={() => setDensity(d.key)}
              className="flex-col h-auto py-2"
            >
              {d.icon}
              {t(d.labelKey)}
            </Button>
          ))}
        </div>
      </div>
    </div>
  );
}

function StepWorkspace({
  t,
  workspace,
  onPick,
}: {
  t: (k: string, opts?: Record<string, unknown>) => string;
  workspace: string;
  onPick: () => void;
}) {
  return (
    <div className="space-y-5">
      <div className="text-center space-y-2">
        <h2 className="text-xl font-bold">{t("onboard.workspace")}</h2>
        <p className="text-sm text-muted-foreground">{t("onboard.workspaceDesc")}</p>
      </div>

      <Button
        variant="outline"
        onClick={onPick}
        className="w-full h-auto border-2 border-dashed border-border hover:border-primary/50 rounded-xl p-6 flex flex-col items-center gap-2"
      >
        <Folder size={32} className="text-primary" />
        <span className="text-sm font-medium">{t("onboard.chooseWorkspace")}</span>
        <span className="text-xs text-muted-foreground">{t("onboard.chooseWorkspaceHint")}</span>
      </Button>

      {workspace && (
        <div className="text-xs text-muted-foreground bg-muted/50 rounded-lg p-3 break-all flex items-start gap-2">
          <Check size={14} className="text-success shrink-0 mt-0.5" />
          {workspace}
        </div>
      )}
    </div>
  );
}

function StepModel({
  t,
  providers,
  selectedProvider,
  setSelectedProvider,
  apiKey,
  setApiKey,
  model,
  setModel,
  testing,
  testResult,
  testOk,
  onTest,
  onSave,
}: {
  t: (k: string, opts?: Record<string, unknown>) => string;
  providers: ProviderInfo[];
  selectedProvider: string;
  setSelectedProvider: (v: string) => void;
  apiKey: string;
  setApiKey: (v: string) => void;
  model: string;
  setModel: (v: string) => void;
  testing: boolean;
  testResult: string | null;
  testOk: boolean | null;
  onTest: () => void;
  onSave: () => void;
}) {
  const active = providers.find((p) => p.provider === selectedProvider);
  return (
    <div className="space-y-4">
      <div className="text-center space-y-2">
        <h2 className="text-xl font-bold">{t("onboard.configKey")}</h2>
        <p className="text-sm text-muted-foreground">{t("onboard.configKeyDesc")}</p>
      </div>

      <div className="space-y-2 max-h-40 overflow-y-auto pr-1">
        {providers.map((p) => (
          <Button
            key={p.provider}
            variant={selectedProvider === p.provider ? "default" : "outline"}
            size="sm"
            onClick={() => {
              setSelectedProvider(p.provider);
              setModel(p.model);
            }}
            className="w-full justify-start h-auto px-3 py-2"
          >
            <ProviderIcon provider={p.provider} size={20} />
            <div className="flex-1 min-w-0 text-left">
              <div className="text-sm font-medium">{p.name}</div>
              <div className="text-2xs text-muted-foreground truncate">{p.model}</div>
            </div>
            {p.api_key_configured && <Check size={14} className="text-success shrink-0" />}
          </Button>
        ))}
      </div>

      {active && (
        <div className="space-y-3">
          <div className="space-y-1">
            <label className="text-xs font-medium text-muted-foreground flex items-center gap-1">
              <KeyRound size={11} />
              {t("onboard.apiKey", { provider: active.name })}
            </label>
            <Input
              type="password"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              placeholder={t("onboard.apiKeyPlaceholder")}
            />
          </div>

          <div className="space-y-1">
            <label className="text-xs font-medium text-muted-foreground flex items-center gap-1">
              <Cpu size={11} />
              {t("onboard.model")}
            </label>
            <Input type="text" value={model} onChange={(e) => setModel(e.target.value)} placeholder={active.model} />
          </div>

          <div className="flex gap-2">
            <Button variant="outline" size="sm" onClick={onTest} disabled={testing || !apiKey} className="flex-1">
              {testing ? <LoadingCurve size={16} particleCount={8} /> : <TestTube size={14} />}
              {t("onboard.testConnection")}
            </Button>
            <Button size="sm" onClick={onSave} disabled={!apiKey} className="flex-1">
              <Save size={14} />
              {t("onboard.saveKey")}
            </Button>
          </div>

          {testResult && (
            <div
              className={`text-xs px-3 py-2 rounded-lg ${testOk === true ? "bg-success/10 text-success" : "bg-destructive/10 text-destructive"}`}
            >
              {testResult}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function StepComplete({
  t,
  warning,
}: {
  t: (k: string, opts?: Record<string, unknown>) => string;
  warning: boolean;
}) {
  return (
    <div className="space-y-5 text-center">
      <div className="flex justify-center">
        <div className="w-16 h-16 rounded-full bg-primary/10 flex items-center justify-center">
          <Rocket size={32} className="text-primary" />
        </div>
      </div>
      <div className="space-y-2">
        <h2 className="text-xl font-bold">{t("onboard.ready")}</h2>
        <p className="text-sm text-muted-foreground">{t("onboard.readyDesc")}</p>
      </div>

      {warning && (
        <div className="text-left bg-amber-500/10 text-amber-700 dark:text-amber-300 rounded-lg px-3 py-2 text-xs flex items-start gap-2">
          <AlertTriangle size={14} className="shrink-0 mt-0.5" />
          <span>{t("onboard.noModelWarning")}</span>
        </div>
      )}

      <div className="text-left bg-muted/40 rounded-xl p-4 space-y-2 text-sm">
        <div className="font-medium text-muted-foreground mb-2">{t("onboard.shortcuts")}</div>
        <div className="flex justify-between">
          <span>Ctrl + K</span>
          <span className="text-muted-foreground">{t("onboard.shortcutCommand")}</span>
        </div>
        <div className="flex justify-between">
          <span>Ctrl + N</span>
          <span className="text-muted-foreground">{t("onboard.shortcutNewChat")}</span>
        </div>
        <div className="flex justify-between">
          <span>Ctrl + Shift + F</span>
          <span className="text-muted-foreground">{t("onboard.shortcutSearch")}</span>
        </div>
      </div>
    </div>
  );
}
