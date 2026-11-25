import { useEffect, useState, useCallback, useMemo, useRef } from "react";
import { useTranslation } from "react-i18next";
import { useApi, ApiError } from "../../hooks/useApi";
import { useToast } from "../../hooks/use-toast";
import {
  Wifi,
  Search,
  CheckCircle2,
  AlertTriangle,
  RefreshCw,
  Save,
  Power,
  Download,
  Upload,
  TestTube,
  Eye,
  EyeOff,
  Sparkles,
  BrainCircuit,
  ImageIcon,
  Wrench,
  Zap,
  Loader2,
  Clock,
} from "lucide-react";
import { ProviderIcon } from "../ProviderIcon";
import { Button } from "../ui/button";
import { Input } from "../ui/input";
import { Badge } from "../ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "../ui/card";
import { LoadingCurve } from "../common/LoadingCurve";
import { EmptyState } from "../common/EmptyState";

interface ModelOption {
  id: string;
  name: string;
  context: number;
  supports?: string[];
}

// 定价编辑条目——字符串承载输入框值，保存时再转数字
interface PricingEdit {
  input: string;
  output: string;
  currency: string;
}

export interface ProviderInfo {
  provider: string;
  name: string;
  base_url: string;
  model: string;
  models: ModelOption[];
  api_key_configured: boolean;
  needs_proxy?: boolean;
  proxy_hint?: string;
  website_url?: string;
  api_key_url?: string;
  health_status?: string;
  configured?: boolean;
  pricing?: Record<string, { input?: number; output?: number; currency?: string }>;
  // H3: API key 轮换年龄追踪
  key_age_days?: number | null;
  rotation_status?: "ok" | "warn" | "expired" | "disabled" | null;
}

const fmtCtx = (n: number): string => {
  if (n >= 1048576) return `${(n / 1048576).toFixed(n > 2097152 ? 0 : 1)}M`;
  if (n >= 1024) return `${Math.round(n / 1024)}K`;
  return String(n);
};

const healthStatus = (p: ProviderInfo) => p.health_status || (p.api_key_configured ? "configured" : "unconfigured");

/** 定价编辑行 = 模型列表 ∪ 已有定价条目（去重，保持模型顺序，孤儿定价追加在后）。
 *
 *  只渲染「已有定价」条目会让零定价的厂商（如 hunyuan 的 5 个模型）整个定价区块被隐藏，
 *  用户从此**没有任何入口**为这些模型补价——而保存逻辑本身是支持新增 id 的，
 *  卡住的只是渲染。反过来也只渲染模型列表会丢掉历史定价（含已下线模型），故取并集。
 */
const pricingRowIds = (p: ProviderInfo): string[] => {
  const modelIds = (p.models || []).map((m) => m.id);
  const orphan = Object.keys(p.pricing || {}).filter((mid) => !modelIds.includes(mid));
  return [...modelIds, ...orphan];
};

export function ModelConfigPanel() {
  const { t } = useTranslation();
  const { api } = useApi();
  const { toast } = useToast();
  const [providers, setProviders] = useState<ProviderInfo[]>([]);
  const [active, setActive] = useState("");
  const [loading, setLoading] = useState(true);
  const [backendReady, setBackendReady] = useState(true);
  const [status, setStatus] = useState("");
  const [selectedProvider, setSelectedProvider] = useState<string>("");
  const [editingKey, setEditingKey] = useState<Record<string, string>>({});
  const [editingModel, setEditingModel] = useState<Record<string, string>>({});
  const [editingPricing, setEditingPricing] = useState<Record<string, Record<string, PricingEdit>>>({});
  const [showKey, setShowKey] = useState<Record<string, boolean>>({});
  const [saving, setSaving] = useState<string | null>(null);
  const [testing, setTesting] = useState<string | null>(null);
  const [syncingCatalog, setSyncingCatalog] = useState<string | null>(null);
  const [detecting, setDetecting] = useState(false);
  const [search, setSearch] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setBackendReady(true);
    try {
      const list = await api<ProviderInfo[]>("/api/v1/models");
      if (Array.isArray(list)) setProviders(list);
    } catch (e: unknown) {
      if (e instanceof ApiError && e.status === 0 && e.statusText === "Backend not ready") {
        setBackendReady(false);
      }
    }
    setLoading(false);
    try {
      const a = await api<{ provider: string }>("/api/v1/models/active");
      if (a?.provider) setActive(a.provider);
    } catch {
      // 激活状态失败不影响列表展示
    }
  }, [api]);

  useEffect(() => {
    load();
  }, [load]);

  // 默认选中第一个已配置或激活的 provider
  useEffect(() => {
    if (selectedProvider) return;
    const first =
      providers.find((p) => p.provider === active) || providers.find((p) => p.api_key_configured) || providers[0];
    if (first) setSelectedProvider(first.provider);
  }, [providers, active, selectedProvider]);

  const save = async (provider: string) => {
    setSaving(provider);
    try {
      const body: Record<string, unknown> = {};
      if (editingKey[provider]) body.api_key = editingKey[provider];
      if (editingModel[provider]) body.model = editingModel[provider];
      if (editingPricing[provider]) {
        const base = providers.find((p) => p.provider === provider)?.pricing || {};
        const pricing: Record<string, Record<string, unknown>> = {};
        // 以原始 pricing 为基础，保留 cached_input 等未编辑字段
        for (const [mid, p] of Object.entries(base)) {
          pricing[mid] = { ...(p as Record<string, unknown>) };
        }
        // 覆盖用户编辑的条目（含新增的模型 id）
        for (const [mid, v] of Object.entries(editingPricing[provider])) {
          const entry: Record<string, unknown> = { ...(pricing[mid] || {}) };
          if (v.input !== "") entry.input = Number(v.input);
          if (v.output !== "") entry.output = Number(v.output);
          if (v.currency) entry.currency = v.currency;
          pricing[mid] = entry;
        }
        body.pricing = pricing;
      }
      await api(`/api/v1/models/${provider}`, { method: "PUT", body: JSON.stringify(body) });
      // P0: 保存后自动激活，避免用户再点一次“启用”
      await api(`/api/v1/models/${provider}/activate`, { method: "POST" });
      setActive(provider);
      setStatus(t("settings.savedAndActivated"));
      toast(t("settings.savedAndActivated"), "success");
      const newKeys = { ...editingKey };
      delete newKeys[provider];
      setEditingKey(newKeys);
      await load();
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      setStatus(`${t("settings.saveFailed")}: ${msg}`);
      toast(`${t("settings.saveFailed")}: ${msg}`, "error");
    }
    setSaving(null);
  };

  const activateProvider = async (p: string) => {
    try {
      await api(`/api/v1/models/${p}/activate`, { method: "POST" });
      setActive(p);
      setStatus(t("settings.switchedTo", { provider: p }));
      toast(t("settings.switchedTo", { provider: p }), "success");
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      setStatus(`${t("settings.activateFailed")}: ${msg}`);
      toast(`${t("settings.activateFailed")}: ${msg}`, "error");
    }
  };

  const testConn = async (p: ProviderInfo) => {
    setTesting(p.provider);
    try {
      const res = await api<{ ok: boolean; status: number; error?: string }>("/api/v1/models/test", {
        method: "POST",
        body: JSON.stringify({
          base_url: p.base_url,
          provider: p.provider,
          api_key: editingKey[p.provider] || "",
        }),
      });
      if (res.ok) {
        // 测试通过后自动保存配置（包括可能的新 API Key），避免用户手动再点保存
        await save(p.provider);
        setStatus(t("settings.testedAndSaved"));
        toast(t("settings.testedAndSaved"), "success");
      } else {
        const detail = res.error || `${res.status}`;
        setStatus(`${t("settings.connectionFailed")}: ${detail}`);
        toast(`${t("settings.connectionFailed")}: ${detail}`, "error");
      }
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      setStatus(`${t("settings.connectionFailed")}: ${msg}`);
      toast(`${t("settings.connectionFailed")}: ${msg}`, "error");
    }
    setTesting(null);
  };

  const refreshModels = async (provider: string) => {
    try {
      await api(`/api/v1/models/${provider}/refresh`, { method: "POST" });
      await load();
      toast(t("settings.modelListRefreshed"), "success");
    } catch {
      toast(t("settings.refreshFailed"), "error");
    }
  };

  const syncCatalog = async (provider: string) => {
    setSyncingCatalog(provider);
    try {
      const res = await api<{ count: number }>(`/api/v1/models/${provider}/sync-catalog`, { method: "POST" });
      await load();
      setStatus(t("settings.catalogSynced", { count: res.count }));
      toast(t("settings.catalogSynced", { count: res.count }), "success");
    } catch (e: unknown) {
      const msg = e instanceof ApiError ? e.message : e instanceof Error ? e.message : String(e);
      setStatus(`${t("settings.catalogSyncFailed")}: ${msg}`);
      toast(`${t("settings.catalogSyncFailed")}: ${msg}`, "error");
    }
    setSyncingCatalog(null);
  };

  const detectAll = async () => {
    setDetecting(true);
    setStatus(t("settings.detectingAll"));
    const configured = providers.filter((p) => p.api_key_configured || editingKey[p.provider]);
    let ok = 0;
    for (const p of configured) {
      try {
        const res = await api<{ ok: boolean }>("/api/v1/models/test", {
          method: "POST",
          body: JSON.stringify({
            base_url: p.base_url,
            provider: p.provider,
            api_key: editingKey[p.provider] || "",
          }),
        });
        if (res.ok) ok += 1;
      } catch {
        /* ignore */
      }
    }
    setDetecting(false);
    setStatus(t("settings.detectDone", { ok, total: configured.length }));
    toast(t("settings.detectDone", { ok, total: configured.length }), ok === configured.length ? "success" : "info");
  };

  const exportConfig = () => {
    const data = {
      version: "yuncat-model-config-v1",
      exported_at: new Date().toISOString(),
      providers: providers.map((p) => ({
        provider: p.provider,
        model: p.model,
        api_key_configured: p.api_key_configured,
      })),
    };
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `yuncat-models-${new Date().toISOString().slice(0, 10)}.json`;
    a.click();
    URL.revokeObjectURL(url);
    toast(t("settings.exportModelsSuccess"), "success");
  };

  const importConfig = async (file: File) => {
    try {
      const text = await file.text();
      const data = JSON.parse(text);
      if (!data.providers || !Array.isArray(data.providers)) {
        toast(t("settings.invalidExport"), "error");
        return;
      }
      let count = 0;
      for (const item of data.providers) {
        if (!item.provider) continue;
        const body: Record<string, string> = {};
        if (item.api_key) body.api_key = item.api_key;
        if (item.model) body.model = item.model;
        if (Object.keys(body).length === 0) continue;
        try {
          await api(`/api/v1/models/${item.provider}`, { method: "PUT", body: JSON.stringify(body) });
          count += 1;
        } catch {
          /* ignore per provider */
        }
      }
      await load();
      toast(t("settings.importModelsSuccess", { count }), "success");
    } catch {
      toast(t("settings.invalidFile"), "error");
    }
  };

  const filtered = useMemo(() => {
    const list = providers
      .filter((p) => !search || p.name.toLowerCase().includes(search.toLowerCase()) || p.provider.includes(search))
      .sort((a, b) => {
        const aActive = a.provider === active ? 0 : 1;
        const bActive = b.provider === active ? 0 : 1;
        if (aActive !== bActive) return aActive - bActive;
        const aCfg = a.api_key_configured ? 2 : 3;
        const bCfg = b.api_key_configured ? 2 : 3;
        return aCfg - bCfg;
      });
    return list;
  }, [providers, active, search]);

  const selected = useMemo(() => providers.find((p) => p.provider === selectedProvider), [providers, selectedProvider]);

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64 gap-3 text-muted-foreground">
        <LoadingCurve variant="fourier" size={32} particleCount={8} />
        <span className="text-sm">{t("settings.loadingProviders")}</span>
      </div>
    );
  }

  if (!backendReady) {
    return (
      <EmptyState
        icon={<AlertTriangle size={24} />}
        title={t("settings.backendNotReady")}
        description={t("settings.backendNotReadyDesc")}
        action={{
          label: t("common.retry"),
          onClick: load,
        }}
      />
    );
  }

  if (providers.length === 0) {
    return (
      <EmptyState
        icon={<Wifi size={24} />}
        title={t("settings.noProviders")}
        description={t("settings.noProvidersDesc")}
      />
    );
  }

  return (
    <div className="space-y-3">
      {status && (
        <div
          className={`text-xs px-3 py-1.5 rounded ${
            status.includes(t("settings.saveFailed")) ||
            status.includes(t("settings.connectionFailed")) ||
            status.includes(t("settings.networkError")) ||
            status.includes(t("settings.activateFailed"))
              ? "bg-destructive/10 text-destructive"
              : "bg-success/10 text-success"
          }`}
        >
          {status}
        </div>
      )}

      <ConfigGuideBanner providers={providers} active={active} />

      <div className="flex gap-3 h-[560px] min-h-[400px]">
        {/* Left: Provider list */}
        <ProviderSidebar
          providers={filtered}
          active={active}
          selected={selectedProvider}
          search={search}
          onSearch={setSearch}
          onSelect={setSelectedProvider}
        />

        {/* Middle: Config form */}
        <div className="flex-1 min-w-0">
          {selected ? (
            <ModelConfigForm
              provider={selected}
              active={active}
              editingKey={editingKey[selected.provider] ?? ""}
              editingModel={editingModel[selected.provider] ?? selected.model}
              editingPricing={editingPricing[selected.provider] ?? {}}
              showKey={showKey[selected.provider] ?? false}
              saving={saving === selected.provider}
              testing={testing === selected.provider}
              syncingCatalog={syncingCatalog === selected.provider}
              onKeyChange={(v) => setEditingKey((k) => ({ ...k, [selected.provider]: v }))}
              onModelChange={(v) => setEditingModel((k) => ({ ...k, [selected.provider]: v }))}
              onPricingChange={(v) => setEditingPricing((k) => ({ ...k, [selected.provider]: v }))}
              onToggleShowKey={() => setShowKey((s) => ({ ...s, [selected.provider]: !s[selected.provider] }))}
              onSave={() => save(selected.provider)}
              onTest={() => testConn(selected)}
              onRefresh={() => refreshModels(selected.provider)}
              onSyncCatalog={() => syncCatalog(selected.provider)}
            />
          ) : (
            <EmptyState
              icon={<Search size={24} />}
              title={t("settings.selectProvider")}
              description={t("settings.selectProviderDesc")}
            />
          )}
        </div>

        {/* Right: Quick actions */}
        <ModelConfigActions
          selected={selected}
          active={active}
          detecting={detecting}
          onActivate={() => selected && activateProvider(selected.provider)}
          onDetect={detectAll}
          onExport={exportConfig}
          onImport={importConfig}
        />
      </div>
    </div>
  );
}

/* ── Provider sidebar ─────────────────────────────── */
function ProviderSidebar({
  providers,
  active,
  selected,
  search,
  onSearch,
  onSelect,
}: {
  providers: ProviderInfo[];
  active: string;
  selected: string;
  search: string;
  onSearch: (v: string) => void;
  onSelect: (v: string) => void;
}) {
  const { t } = useTranslation();
  return (
    <Card className="w-56 shrink-0 flex flex-col">
      <CardHeader className="pb-2">
        <CardTitle className="text-sm">{t("settings.providers")}</CardTitle>
      </CardHeader>
      <CardContent className="flex-1 min-h-0 flex flex-col gap-2">
        <div className="relative">
          <Search size={12} className="absolute left-2 top-1/2 -translate-y-1/2 text-muted-foreground" />
          <Input
            value={search}
            onChange={(e) => onSearch(e.target.value)}
            placeholder={t("settings.searchProviders")}
            className="pl-7 h-8 text-xs"
          />
        </div>
        <div className="flex-1 overflow-y-auto space-y-1 pr-1 -mr-1">
          {providers.map((p) => {
            const isActive = active === p.provider;
            const isSelected = selected === p.provider;
            const status = healthStatus(p);
            return (
              <button
                key={p.provider}
                onClick={() => onSelect(p.provider)}
                className={`w-full flex items-center gap-2 px-2.5 py-2 rounded-lg text-left text-xs transition-colors ${
                  isSelected ? "bg-primary text-primary-foreground" : "hover:bg-secondary text-foreground"
                }`}
              >
                <ProviderIcon provider={p.provider} size={20} />
                <span className="flex-1 truncate">{p.name}</span>
                <HealthDot status={status} active={isActive} />
              </button>
            );
          })}
        </div>
      </CardContent>
    </Card>
  );
}

/* ── Config form ──────────────────────────────────── */
function ModelConfigForm({
  provider,
  active,
  editingKey,
  editingModel,
  editingPricing,
  showKey,
  saving,
  testing,
  syncingCatalog,
  onKeyChange,
  onModelChange,
  onPricingChange,
  onToggleShowKey,
  onSave,
  onTest,
  onRefresh,
  onSyncCatalog,
}: {
  provider: ProviderInfo;
  active: string;
  editingKey: string;
  editingModel: string;
  editingPricing: Record<string, PricingEdit>;
  showKey: boolean;
  saving: boolean;
  testing: boolean;
  syncingCatalog: boolean;
  onKeyChange: (v: string) => void;
  onModelChange: (v: string) => void;
  onPricingChange: (v: Record<string, PricingEdit>) => void;
  onToggleShowKey: () => void;
  onSave: () => void;
  onTest: () => void;
  onRefresh: () => void;
  onSyncCatalog: () => void;
}) {
  const { t } = useTranslation();
  const isActive = active === provider.provider;
  const currentModel = provider.models.find((m) => m.id === (editingModel || provider.model));

  return (
    <Card className="h-full flex flex-col overflow-hidden">
      <CardHeader className="pb-2">
        <div className="flex items-center gap-3">
          <ProviderIcon provider={provider.provider} size={36} />
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2">
              <CardTitle className="text-base">{provider.name}</CardTitle>
              {isActive && (
                <Badge variant="default" className="text-[10px] h-5 px-1.5">
                  <CheckCircle2 size={10} className="mr-1" />
                  {t("settings.active")}
                </Badge>
              )}
            </div>
            <p className="text-xs text-muted-foreground truncate">
              {provider.base_url?.replace("https://", "").split("/")[0] || t("settings.notConfigured")}
            </p>
          </div>
        </div>
        {provider.needs_proxy && (
          <div className="flex items-start gap-2 px-3 py-2 bg-warning/10 border border-warning/20 rounded-lg text-xs text-warning mt-2">
            <AlertTriangle size={14} className="text-warning shrink-0 mt-0.5" />
            <span>{provider.proxy_hint || t("settings.proxyHint")}</span>
          </div>
        )}
        {/* H3: API key 轮换提醒——仅 warn/expired 状态显示软横幅 */}
        {provider.api_key_configured && provider.rotation_status === "warn" && provider.key_age_days != null && (
          <div className="flex items-start gap-2 px-3 py-2 bg-warning/10 border border-warning/20 rounded-lg text-xs text-warning mt-2">
            <AlertTriangle size={14} className="text-warning shrink-0 mt-0.5" />
            <span>{t("settings.keyRotation.warn", { days: provider.key_age_days })}</span>
          </div>
        )}
        {provider.api_key_configured && provider.rotation_status === "expired" && provider.key_age_days != null && (
          <div className="flex items-start gap-2 px-3 py-2 bg-destructive/10 border border-destructive/20 rounded-lg text-xs text-destructive mt-2">
            <AlertTriangle size={14} className="text-destructive shrink-0 mt-0.5" />
            <span>{t("settings.keyRotation.expired", { days: provider.key_age_days })}</span>
          </div>
        )}
      </CardHeader>

      <CardContent className="flex-1 overflow-y-auto space-y-4">
        {/* Base URL + get key link */}
        <div className="space-y-1.5">
          <label className="text-xs font-medium text-muted-foreground">{t("settings.baseUrl")}</label>
          <div className="flex items-center gap-2">
            <Input value={provider.base_url} disabled className="font-mono text-xs bg-muted/50" />
            {provider.api_key_url && (
              <a
                href={provider.api_key_url}
                target="_blank"
                rel="noopener noreferrer"
                className="text-xs text-primary hover:underline shrink-0 whitespace-nowrap"
              >
                {t("settings.getKey")} →
              </a>
            )}
          </div>
        </div>

        {/* API Key */}
        <div className="space-y-1.5">
          <div className="flex items-center gap-2">
            <label className="text-xs font-medium text-muted-foreground">{t("settings.apiKey")}</label>
            {/* H3: key 年龄徽章——ok 绿/warn 黄/expired 红/disabled 不显示 */}
            {provider.api_key_configured && provider.rotation_status === "ok" && provider.key_age_days != null && (
              <Badge variant="secondary" className="text-[10px] h-4 px-1 gap-0.5">
                <Clock size={9} />
                {t("settings.keyRotation.days", { days: provider.key_age_days })}
              </Badge>
            )}
            {provider.api_key_configured && provider.rotation_status === "warn" && provider.key_age_days != null && (
              <Badge className="bg-warning/20 text-warning border-warning/30 text-[10px] h-4 px-1 gap-0.5">
                <Clock size={9} />
                {t("settings.keyRotation.days", { days: provider.key_age_days })}
              </Badge>
            )}
            {provider.api_key_configured && provider.rotation_status === "expired" && provider.key_age_days != null && (
              <Badge variant="destructive" className="text-[10px] h-4 px-1 gap-0.5">
                <Clock size={9} />
                {t("settings.keyRotation.days", { days: provider.key_age_days })}
              </Badge>
            )}
          </div>
          <div className="flex gap-2">
            <div className="relative flex-1">
              <Input
                type={showKey ? "text" : "password"}
                value={editingKey}
                onChange={(e) => onKeyChange(e.target.value)}
                placeholder={
                  provider.api_key_configured ? t("settings.apiKeySavedPlaceholder") : t("settings.apiKeyPlaceholder")
                }
                className="pr-9 font-mono text-xs"
              />
              <button
                type="button"
                onClick={onToggleShowKey}
                className="absolute right-2 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
                title={showKey ? t("settings.hideKey") : t("settings.showKey")}
              >
                {showKey ? <EyeOff size={13} /> : <Eye size={13} />}
              </button>
            </div>
            <Button variant="outline" size="sm" onClick={onTest} disabled={testing} className="gap-1 text-xs">
              {testing ? <Loader2 size={12} className="animate-spin" /> : <Wifi size={12} />}
              {t("settings.test")}
            </Button>
          </div>
        </div>

        <div className="h-6" />

        {/* Model picker */}
        <div className="space-y-1.5">
          <div className="flex items-center justify-between">
            <label className="text-xs font-medium text-muted-foreground">{t("settings.model")}</label>
            <div className="flex items-center gap-1">
              <Button
                variant="ghost"
                size="xs"
                onClick={onSyncCatalog}
                disabled={syncingCatalog}
                className="h-auto py-0 px-1 text-[10px] gap-1"
                title={t("settings.syncCatalogHint")}
              >
                {syncingCatalog ? <Loader2 size={10} className="animate-spin" /> : <Download size={10} />}
                {t("settings.syncCatalog")}
              </Button>
              {provider.api_key_configured && (
                <Button variant="ghost" size="xs" onClick={onRefresh} className="h-auto py-0 px-1 text-[10px] gap-1">
                  <RefreshCw size={10} />
                  {t("settings.refreshModels")}
                </Button>
              )}
            </div>
          </div>
          <input
            list={`model-options-${provider.provider}`}
            value={editingModel || provider.model}
            onChange={(e) => onModelChange(e.target.value)}
            placeholder={provider.models.length ? t("settings.selectOrEnterModel") : t("settings.enterModelName")}
            className="w-full px-3 py-2 border border-border rounded-md text-xs bg-background font-mono text-foreground outline-none transition-colors focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring/30"
          />
          <datalist id={`model-options-${provider.provider}`}>
            {provider.models.map((m) => (
              <option key={m.id} value={m.id}>
                {m.name} · {fmtCtx(m.context)}
              </option>
            ))}
          </datalist>
          <p className="text-[10px] text-muted-foreground">
            {t("settings.customModelHint")}
          </p>
        </div>

        {/* Model capabilities */}
        {currentModel && (
          <div className="flex flex-wrap gap-1.5">
            <CapabilityBadge
              active={(currentModel.supports || []).includes("vision")}
              icon={ImageIcon}
              label={t("settings.capVision")}
            />
            <CapabilityBadge
              active={(currentModel.supports || []).includes("tools")}
              icon={Wrench}
              label={t("settings.capTools")}
            />
            <CapabilityBadge
              active={(currentModel.supports || []).includes("streaming")}
              icon={Zap}
              label={t("settings.capStreaming")}
            />
            <CapabilityBadge
              active={(currentModel.supports || []).includes("reasoning")}
              icon={BrainCircuit}
              label={t("settings.capReasoning")}
            />
            <Badge variant="secondary" className="text-[10px] h-5 gap-1">
              <Sparkles size={10} />
              {t("settings.contextWindow", { ctx: fmtCtx(currentModel.context) })}
            </Badge>
          </div>
        )}

        {/* Pricing — 可编辑，覆盖厂商过时的定价快照 */}
        {pricingRowIds(provider).length > 0 && (
          <div className="space-y-1.5">
            <label className="text-xs font-medium text-muted-foreground">{t("settings.pricingSummary")}</label>
            <div className="text-xs bg-muted/30 rounded-lg p-2 border space-y-1.5">
              {pricingRowIds(provider).map((mid) => {
                const price = provider.pricing?.[mid] || {};
                const edit = editingPricing[mid];
                const inputVal = edit?.input ?? (price.input != null ? String(price.input) : "");
                const outputVal = edit?.output ?? (price.output != null ? String(price.output) : "");
                const currencyVal = edit?.currency ?? price.currency ?? "USD";
                const setField = (field: keyof PricingEdit, value: string) => {
                  const next = { ...editingPricing, [mid]: { input: inputVal, output: outputVal, currency: currencyVal, [field]: value } };
                  onPricingChange(next);
                };
                return (
                  <div key={mid} className="flex items-center gap-2">
                    <span className="font-mono text-[10px] truncate flex-1 min-w-0" title={mid}>
                      {mid}
                    </span>
                    <input
                      value={inputVal}
                      onChange={(e) => setField("input", e.target.value)}
                      placeholder="in"
                      inputMode="decimal"
                      className="w-16 px-1.5 py-1 border rounded text-[10px] font-mono bg-background text-right tabular-nums"
                      aria-label={`${mid} input price`}
                    />
                    <input
                      value={outputVal}
                      onChange={(e) => setField("output", e.target.value)}
                      placeholder="out"
                      inputMode="decimal"
                      className="w-16 px-1.5 py-1 border rounded text-[10px] font-mono bg-background text-right tabular-nums"
                      aria-label={`${mid} output price`}
                    />
                    <input
                      value={currencyVal}
                      onChange={(e) => setField("currency", e.target.value)}
                      className="w-12 px-1.5 py-1 border rounded text-[10px] font-mono bg-background uppercase"
                      aria-label={`${mid} currency`}
                    />
                  </div>
                );
              })}
              <p className="text-[10px] text-muted-foreground">
                {t("settings.pricingEditHint")}
              </p>
            </div>
          </div>
        )}

        {/* Save */}
        <div className="pt-2">
          <Button
            variant="capsule"
            onClick={onSave}
            disabled={saving || (!editingKey && !editingModel && Object.keys(editingPricing).length === 0)}
            className="gap-1.5 text-xs"
          >
            {saving ? <Loader2 size={12} className="animate-spin" /> : <Save size={12} />}
            {t("common.save")}
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

/* ── Capability badge ─────────────────────────────── */
function CapabilityBadge({ active, icon: Icon, label }: { active: boolean; icon: typeof ImageIcon; label: string }) {
  return (
    <Badge
      variant={active ? "default" : "outline"}
      className={`text-[10px] h-5 gap-1 ${active ? "" : "text-muted-foreground opacity-60"}`}
    >
      <Icon size={10} />
      {label}
    </Badge>
  );
}

/* ── Quick actions ────────────────────────────────── */
function ModelConfigActions({
  selected,
  active,
  detecting,
  onActivate,
  onDetect,
  onExport,
  onImport,
}: {
  selected?: ProviderInfo;
  active: string;
  detecting: boolean;
  onActivate: () => void;
  onDetect: () => void;
  onExport: () => void;
  onImport: (file: File) => void;
}) {
  const { t } = useTranslation();
  const fileRef = useRef<HTMLInputElement>(null);
  const isActive = selected?.provider === active;
  const canActivate = selected && !isActive && (selected.api_key_configured || false);

  return (
    <Card className="w-52 shrink-0 flex flex-col">
      <CardHeader className="pb-2">
        <CardTitle className="text-sm">{t("settings.quickActions")}</CardTitle>
      </CardHeader>
      <CardContent className="flex-1 overflow-y-auto space-y-2">
        <Button
          variant={isActive ? "outline" : "default"}
          size="sm"
          className="w-full justify-start text-xs gap-2"
          onClick={onActivate}
          disabled={!canActivate}
        >
          <Power size={13} />
          {isActive ? t("settings.alreadyActive") : t("settings.activate")}
        </Button>

        <Button
          variant="outline"
          size="sm"
          className="w-full justify-start text-xs gap-2"
          onClick={onDetect}
          disabled={detecting}
        >
          {detecting ? <Loader2 size={13} className="animate-spin" /> : <TestTube size={13} />}
          {t("settings.detectAll")}
        </Button>

        <div className="h-6" />

        <Button variant="outline" size="sm" className="w-full justify-start text-xs gap-2" onClick={onExport}>
          <Download size={13} />
          {t("settings.exportModels")}
        </Button>

        <input
          ref={fileRef}
          type="file"
          accept=".json"
          className="hidden"
          onChange={(e) => {
            const f = e.target.files?.[0];
            if (f) onImport(f);
            e.currentTarget.value = "";
          }}
        />
        <Button
          variant="outline"
          size="sm"
          className="w-full justify-start text-xs gap-2"
          onClick={() => fileRef.current?.click()}
        >
          <Upload size={13} />
          {t("settings.importModels")}
        </Button>
      </CardContent>
    </Card>
  );
}

/* ── Health dot ───────────────────────────────────── */
function HealthDot({ status, active }: { status: string; active?: boolean }) {
  const color =
    status === "active"
      ? "bg-success ring-2 ring-success/30"
      : status === "configured"
        ? "bg-success/60"
        : status === "failed"
          ? "bg-destructive"
          : "bg-muted-foreground/30";
  return <span className={`w-1.5 h-1.5 rounded-full ${color}`} title={active ? "active" : status} />;
}

/* ── Config guide banner ──────────────────────────── */
function ConfigGuideBanner({ providers, active }: { providers: ProviderInfo[]; active: string }) {
  const { t } = useTranslation();
  const configured = providers.filter((p) => p.api_key_configured);
  const activeName = providers.find((p) => p.provider === active)?.name || "";

  if (configured.length >= 2) return null;

  return (
    <div className="rounded-xl border border-primary/20 bg-primary/5 p-4 space-y-2">
      <div className="flex items-center gap-2">
        <Sparkles size={14} className="text-primary" />
        <span className="text-xs font-semibold">{t("settings.guideTitle")}</span>
      </div>
      <div className="grid grid-cols-4 gap-1.5">
        {providers.slice(0, 4).map((p) => {
          const isActive = p.provider === active;
          const isConfigured = p.api_key_configured;
          return (
            <div
              key={p.provider}
              className={`flex items-center gap-1 text-[10px] px-2 py-1 rounded ${
                isActive
                  ? "bg-primary text-primary-foreground"
                  : isConfigured
                    ? "bg-success/10 text-success"
                    : "bg-muted text-muted-foreground"
              }`}
            >
              <HealthDot status={isActive ? "active" : isConfigured ? "configured" : "unconfigured"} />
              <span className="truncate">{p.name}</span>
            </div>
          );
        })}
      </div>
      <div className="text-[10px] text-muted-foreground leading-relaxed">
        {t("settings.configuredActive", { active: activeName || t("settings.none") })}
        <br />
        {t("settings.guideAdvantages")}
        {configured.length === 0 && <span className="text-warning ml-1">{t("settings.guidePrompt")}</span>}
      </div>
    </div>
  );
}
