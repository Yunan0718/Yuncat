import { useEffect, useState, useCallback, useMemo, useRef } from "react";
import { useTranslation } from "react-i18next";
import { useApi, ApiError, isAbortedError } from "../../hooks/useApi";
import { useToast } from "../../hooks/use-toast";
import {
  Layers,
  Zap,
  BrainCircuit,
  ImageIcon,
  Wrench,
  Sparkles,
  RefreshCw,
  AlertTriangle,
  GripVertical,
  X,
  Loader2,
} from "lucide-react";
import { ProviderIcon } from "../ProviderIcon";
import { Button } from "../ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "../ui/card";
import { Badge } from "../ui/badge";
import { Switch } from "../ui/switch";
import { LoadingCurve } from "../common/LoadingCurve";
import { EmptyState } from "../common/EmptyState";

interface TierBinding {
  provider: string;
  model_id: string;
}

interface TierConfig {
  enabled: boolean;
  tiers: Record<string, TierBinding[]>;
}

interface ModelEntry {
  provider: string;
  model_id: string;
  name: string;
  context: number;
  supports: string[];
  supports_vision: boolean;
  thinking: boolean;
  pricing?: { input?: number; output?: number; currency?: string };
  recommended_tier: string;
}

interface ProviderModels {
  provider: string;
  name: string;
  models: ModelEntry[];
}

const TIER_ORDER = ["lite", "normal", "max", "vision"] as const;
type TierId = (typeof TIER_ORDER)[number];

const TIER_ICONS: Record<TierId, typeof Zap> = {
  lite: Zap,
  normal: Wrench,
  max: BrainCircuit,
  vision: ImageIcon,
};

const fmtCtx = (n: number): string => {
  if (n >= 1048576) return `${(n / 1048576).toFixed(n > 2097152 ? 0 : 1)}M`;
  if (n >= 1024) return `${Math.round(n / 1024)}K`;
  return String(n);
};

function modelKey(provider: string, modelId: string): string {
  return `${provider}::${modelId}`;
}

function parseModelKey(key: string): { provider: string; model_id: string } | null {
  const parts = key.split("::");
  if (parts.length !== 2) return null;
  return { provider: parts[0], model_id: parts[1] };
}

export function ModelTieringPanel() {
  const { t } = useTranslation();
  const { api } = useApi();
  const { toast } = useToast();

  const [loading, setLoading] = useState(true);
  const [enabled, setEnabled] = useState(false);
  const [tiers, setTiers] = useState<Record<TierId, TierBinding[]>>({
    lite: [],
    normal: [],
    max: [],
    vision: [],
  });
  const [models, setModels] = useState<ModelEntry[]>([]);
  const [providers, setProviders] = useState<ProviderModels[]>([]);
  const [saving, setSaving] = useState<Set<string>>(new Set());
  const [dragOverTier, setDragOverTier] = useState<string | null>(null);
  const [draggingKey, setDraggingKey] = useState<string | null>(null);
  const [backendReady, setBackendReady] = useState(true);
  const dragSource = useRef<{ tier?: TierId; index?: number; key: string } | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setBackendReady(true);
    try {
      const [cfg, all] = await Promise.all([
        api<TierConfig>("/api/v1/models/tiers"),
        api<ModelEntry[]>("/api/v1/models/all"),
      ]);
      setEnabled(cfg.enabled);
      const nextTiers: Record<TierId, TierBinding[]> = {
        lite: [],
        normal: [],
        max: [],
        vision: [],
      };
      for (const tier of TIER_ORDER) {
        const list = cfg.tiers[tier];
        if (Array.isArray(list)) {
          nextTiers[tier] = list.filter(
            (b): b is TierBinding =>
              b && typeof b.provider === "string" && typeof b.model_id === "string",
          );
        }
      }
      setTiers(nextTiers);
      setModels(all);
      const grouped = new Map<string, ProviderModels>();
      for (const m of all) {
        if (!grouped.has(m.provider)) {
          grouped.set(m.provider, { provider: m.provider, name: m.provider, models: [] });
        }
        grouped.get(m.provider)!.models.push(m);
      }
      setProviders(Array.from(grouped.values()));
    } catch (e) {
      // StrictMode 双挂载会 abort 首次在途请求——忽略 abort（对标 React Query/SWR）
      if (isAbortedError(e)) return;
      if (e instanceof ApiError && e.status === 0 && e.statusText === "Backend not ready") {
        setBackendReady(false);
      } else {
        toast(t("settings.tieringLoadFailed"), "error");
      }
    }
    setLoading(false);
  }, [api, toast, t]);

  useEffect(() => {
    load();
  }, [load]);

  const assignmentMap = useMemo(() => {
    const map = new Map<string, { tier: TierId; index: number }>();
    for (const tier of TIER_ORDER) {
      tiers[tier].forEach((b, idx) => {
        map.set(modelKey(b.provider, b.model_id), { tier, index: idx });
      });
    }
    return map;
  }, [tiers]);

  const saveTier = useCallback(
    async (tier: TierId, bindings: TierBinding[]) => {
      setSaving((prev) => new Set(prev).add(tier));
      try {
        await api("/api/v1/models/tiers", {
          method: "PUT",
          body: JSON.stringify({ tier, bindings }),
        });
      } catch (e) {
        const msg = e instanceof Error ? e.message : String(e);
        toast(`${t("settings.tierSaveFailed")}: ${msg}`, "error");
        await load();
      } finally {
        setSaving((prev) => {
          const next = new Set(prev);
          next.delete(tier);
          return next;
        });
      }
    },
    [api, load, toast, t],
  );

  const toggleEnabled = useCallback(
    async (value: boolean) => {
      setEnabled(value);
      try {
        await api("/api/v1/models/tiering", {
          method: "POST",
          body: JSON.stringify({ enabled: value }),
        });
        if (value) await load();
      } catch (e) {
        const msg = e instanceof Error ? e.message : String(e);
        toast(`${t("settings.tieringToggleFailed")}: ${msg}`, "error");
        setEnabled((v) => !v);
      }
    },
    [api, load, toast, t],
  );

  const restoreRecommendations = useCallback(async () => {
    try {
      const recs = await api<Record<string, TierBinding[]>>("/api/v1/models/tiers/recommend");
      const nextTiers: Record<TierId, TierBinding[]> = {
        lite: [],
        normal: [],
        max: [],
        vision: [],
      };
      for (const tier of TIER_ORDER) {
        const list = recs[tier];
        nextTiers[tier] = Array.isArray(list)
          ? list.filter(
              (b): b is TierBinding =>
                b && typeof b.provider === "string" && typeof b.model_id === "string",
            )
          : [];
      }
      setTiers(nextTiers);
      await Promise.all(TIER_ORDER.map((tier) => saveTier(tier, nextTiers[tier])));
      toast(t("settings.tieringRestored"), "success");
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      toast(`${t("settings.tieringRestoreFailed")}: ${msg}`, "error");
    }
  }, [api, saveTier, toast, t]);

  const handleAddToTier = useCallback(
    (tier: TierId, key: string) => {
      const parsed = parseModelKey(key);
      if (!parsed) return;
      setTiers((prev) => {
        const current = prev[tier];
        if (current.some((b) => modelKey(b.provider, b.model_id) === key)) return prev;
        const next = { ...prev, [tier]: [...current, parsed] };
        saveTier(tier, next[tier]);
        return next;
      });
    },
    [saveTier],
  );

  const handleRemoveFromTier = useCallback(
    (tier: TierId, index: number) => {
      setTiers((prev) => {
        const nextList = prev[tier].filter((_, i) => i !== index);
        const next = { ...prev, [tier]: nextList };
        saveTier(tier, nextList);
        return next;
      });
    },
    [saveTier],
  );

  const handleMoveWithinTier = useCallback(
    (tier: TierId, fromIndex: number, toIndex: number) => {
      if (fromIndex === toIndex) return;
      setTiers((prev) => {
        const list = [...prev[tier]];
        const [moved] = list.splice(fromIndex, 1);
        list.splice(toIndex, 0, moved);
        const next = { ...prev, [tier]: list };
        saveTier(tier, list);
        return next;
      });
    },
    [saveTier],
  );

  const handleDragStart = useCallback(
    (e: React.DragEvent, key: string, source?: { tier: TierId; index: number }) => {
      e.dataTransfer.setData("text/plain", key);
      e.dataTransfer.effectAllowed = "move";
      dragSource.current = { key, ...source };
      setDraggingKey(key);
    },
    [],
  );

  const handleDragOver = useCallback((e: React.DragEvent, tier: TierId) => {
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    setDragOverTier(tier);
  }, []);

  const handleDragLeave = useCallback(() => {
    setDragOverTier(null);
  }, []);

  const handleDropOnTier = useCallback(
    (e: React.DragEvent, tier: TierId) => {
      e.preventDefault();
      setDragOverTier(null);
      setDraggingKey(null);
      const key = e.dataTransfer.getData("text/plain");
      if (!key) return;
      const source = dragSource.current;
      dragSource.current = null;
      if (source?.tier === tier && source.index !== undefined) {
        const parsed = parseModelKey(key);
        if (!parsed) return;
        const current = tiers[tier];
        const targetIndex = current.findIndex(
          (b, i) => modelKey(b.provider, b.model_id) === key && i === source.index,
        );
        if (targetIndex < 0) return;
        const dropIndex = resolveDropIndex(e, current.length);
        handleMoveWithinTier(tier, targetIndex, dropIndex);
        return;
      }
      handleAddToTier(tier, key);
    },
    [handleAddToTier, handleMoveWithinTier, tiers],
  );

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64 gap-3 text-muted-foreground">
        <LoadingCurve variant="fourier" size={32} particleCount={8} />
        <span className="text-sm">{t("settings.loadingTiering")}</span>
      </div>
    );
  }

  if (!backendReady) {
    return (
      <EmptyState
        icon={<AlertTriangle size={24} />}
        title={t("settings.backendNotReady")}
        description={t("settings.backendNotReadyDesc")}
        action={{ label: t("common.retry"), onClick: load }}
      />
    );
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-col sm:flex-row sm:items-center gap-3 justify-between">
        <TieringSwitch enabled={enabled} onToggle={toggleEnabled} t={t} />
        <Button
          variant="outline"
          size="sm"
          onClick={restoreRecommendations}
          disabled={!enabled}
          className="gap-1.5 text-xs"
        >
          <RefreshCw size={13} />
          {t("settings.restoreRecommendations")}
        </Button>
      </div>

      {!enabled ? (
        <EmptyState
          icon={<Layers size={24} />}
          title={t("settings.tieringDisabledTitle")}
          description={t("settings.tieringDisabledDesc")}
        />
      ) : (
        <div className="grid grid-cols-1 lg:grid-cols-12 gap-4 h-[600px] min-h-[400px]">
          <div className="lg:col-span-5 space-y-3 overflow-y-auto pr-1">
            {TIER_ORDER.map((tier) => (
              <TierSlot
                key={tier}
                tier={tier}
                bindings={tiers[tier]}
                models={models}
                saving={saving.has(tier)}
                dragOver={dragOverTier === tier}
                draggingKey={draggingKey}
                onDragStart={handleDragStart}
                onDragOver={handleDragOver}
                onDragLeave={handleDragLeave}
                onDrop={handleDropOnTier}
                onRemove={handleRemoveFromTier}
                t={t}
              />
            ))}
          </div>
          <div className="lg:col-span-7 min-h-0 overflow-hidden">
            <ProviderModelList
              providers={providers}
              assignmentMap={assignmentMap}
              onDragStart={handleDragStart}
              t={t}
            />
          </div>
        </div>
      )}
    </div>
  );
}

function TieringSwitch({
  enabled,
  onToggle,
  t,
}: {
  enabled: boolean;
  onToggle: (v: boolean) => void;
  t: (k: string) => string;
}) {
  return (
    <div className="flex items-center gap-3">
      <Switch checked={enabled} onCheckedChange={onToggle} id="tiering-toggle" />
      <label htmlFor="tiering-toggle" className="text-sm font-medium cursor-pointer">
        {t("settings.enableTiering")}
      </label>
    </div>
  );
}

function TierSlot({
  tier,
  bindings,
  models,
  saving,
  dragOver,
  draggingKey,
  onDragStart,
  onDragOver,
  onDragLeave,
  onDrop,
  onRemove,
  t,
}: {
  tier: TierId;
  bindings: TierBinding[];
  models: ModelEntry[];
  saving: boolean;
  dragOver: boolean;
  draggingKey: string | null;
  onDragStart: (e: React.DragEvent, key: string, source?: { tier: TierId; index: number }) => void;
  onDragOver: (e: React.DragEvent, tier: TierId) => void;
  onDragLeave: () => void;
  onDrop: (e: React.DragEvent, tier: TierId) => void;
  onRemove: (tier: TierId, index: number) => void;
  t: (k: string) => string;
}) {
  const Icon = TIER_ICONS[tier];
  const modelMap = useMemo(() => {
    const map = new Map<string, ModelEntry>();
    for (const m of models) map.set(modelKey(m.provider, m.model_id), m);
    return map;
  }, [models]);

  return (
    <Card
      className={`transition-colors ${dragOver ? "border-primary ring-1 ring-primary/30" : ""}`}
      onDragOver={(e) => onDragOver(e, tier)}
      onDragLeave={onDragLeave}
      onDrop={(e) => onDrop(e, tier)}
    >
      <CardHeader className="pb-2">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <div
              className={`p-1.5 rounded-md ${
                tier === "lite"
                  ? "bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400"
                  : tier === "normal"
                    ? "bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-400"
                    : tier === "max"
                      ? "bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400"
                      : "bg-purple-100 text-purple-700 dark:bg-purple-900/30 dark:text-purple-400"
              }`}
            >
              <Icon size={14} />
            </div>
            <CardTitle className="text-sm">{t(`settings.tier_${tier}`)}</CardTitle>
          </div>
          {saving && <Loader2 size={14} className="animate-spin text-muted-foreground" />}
        </div>
        <p className="text-[10px] text-muted-foreground">{t(`settings.tier_${tier}_desc`)}</p>
      </CardHeader>
      <CardContent className="pt-0">
        {bindings.length === 0 ? (
          <div className="text-xs text-muted-foreground border border-dashed border-border rounded-lg py-4 text-center">
            {t("settings.dragModelsHere")}
          </div>
        ) : (
          <div className="space-y-1.5">
            {bindings.map((b, idx) => {
              const key = modelKey(b.provider, b.model_id);
              const model = modelMap.get(key);
              return (
                <TierModelCard
                  // 不拼 idx：下面 isDragging 用 `draggingKey === key` 判断，本来就把
                  // key 当唯一标识；key 再拼 idx 与这套判断不一致，且删中间项时
                  // 会让后续卡片错位复用状态。idx 仍作为 index prop 传递。
                  key={key}
                  index={idx}
                  tier={tier}
                  binding={b}
                  model={model}
                  isDragging={draggingKey === key}
                  onDragStart={onDragStart}
                  onRemove={onRemove}
                  t={t}
                />
              );
            })}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function TierModelCard({
  index,
  tier,
  binding,
  model,
  isDragging,
  onDragStart,
  onRemove,
  t,
}: {
  index: number;
  tier: TierId;
  binding: TierBinding;
  model?: ModelEntry;
  isDragging: boolean;
  onDragStart: (e: React.DragEvent, key: string, source?: { tier: TierId; index: number }) => void;
  onRemove: (tier: TierId, index: number) => void;
  t: (k: string) => string;
}) {
  const key = modelKey(binding.provider, binding.model_id);
  return (
    <div
      draggable
      onDragStart={(e) => onDragStart(e, key, { tier, index })}
      className={`flex items-center gap-2 px-2.5 py-2 rounded-lg border text-xs group transition-opacity ${
        isDragging ? "opacity-50" : "opacity-100"
      } bg-background hover:border-primary/50`}
    >
      <span className="font-mono text-[10px] text-muted-foreground w-4 text-center">{index + 1}</span>
      <GripVertical size={12} className="text-muted-foreground cursor-grab" />
      <ProviderIcon provider={binding.provider} size={16} />
      <span className="flex-1 truncate">{model?.name || binding.model_id}</span>
      {model && (
        <span className="text-[10px] text-muted-foreground hidden sm:inline">{fmtCtx(model.context)}</span>
      )}
      <button
        type="button"
        onClick={() => onRemove(tier, index)}
        className="opacity-0 group-hover:opacity-100 transition-opacity text-muted-foreground hover:text-destructive"
        title={t("settings.removeFromTier")}
      >
        <X size={12} />
      </button>
    </div>
  );
}

function ProviderModelList({
  providers,
  assignmentMap,
  onDragStart,
  t,
}: {
  providers: ProviderModels[];
  assignmentMap: Map<string, { tier: TierId; index: number }>;
  onDragStart: (e: React.DragEvent, key: string) => void;
  t: (k: string) => string;
}) {
  if (providers.length === 0) {
    return (
      <Card className="h-full">
        <CardContent className="flex items-center justify-center h-full">
          <EmptyState icon={<Sparkles size={24} />} title={t("settings.noProviders")} description={t("settings.noProvidersDesc")} />
        </CardContent>
      </Card>
    );
  }

  return (
    <Card className="h-full flex flex-col">
      <CardHeader className="pb-2">
        <CardTitle className="text-sm">{t("settings.availableModels")}</CardTitle>
      </CardHeader>
      <CardContent className="flex-1 overflow-y-auto space-y-4">
        {providers.map((p) => (
          <ProviderModelGroup
            key={p.provider}
            provider={p}
            assignmentMap={assignmentMap}
            onDragStart={onDragStart}
            t={t}
          />
        ))}
      </CardContent>
    </Card>
  );
}

function ProviderModelGroup({
  provider,
  assignmentMap,
  onDragStart,
  t,
}: {
  provider: ProviderModels;
  assignmentMap: Map<string, { tier: TierId; index: number }>;
  onDragStart: (e: React.DragEvent, key: string) => void;
  t: (k: string) => string;
}) {
  return (
    <div>
      <div className="flex items-center gap-2 mb-2">
        <ProviderIcon provider={provider.provider} size={16} />
        <span className="text-xs font-medium">{provider.name}</span>
      </div>
      <div className="flex flex-wrap gap-1.5">
        {provider.models.map((m) => {
          const key = modelKey(m.provider, m.model_id);
          const assigned = assignmentMap.get(key);
          return (
            <ModelCapsule
              key={key}
              model={m}
              assigned={assigned}
              onDragStart={onDragStart}
              t={t}
            />
          );
        })}
      </div>
    </div>
  );
}

function ModelCapsule({
  model,
  assigned,
  onDragStart,
  t,
}: {
  model: ModelEntry;
  assigned?: { tier: TierId; index: number };
  onDragStart: (e: React.DragEvent, key: string) => void;
  t: (k: string) => string;
}) {
  const key = modelKey(model.provider, model.model_id);
  return (
    <div
      draggable
      onDragStart={(e) => onDragStart(e, key)}
      className={`inline-flex items-center gap-1.5 px-2 py-1 rounded-full border text-[10px] cursor-grab transition-colors hover:border-primary/60 ${
        assigned ? "bg-primary/10 border-primary/40" : "bg-secondary border-transparent"
      }`}
      title={`${model.name} · ${fmtCtx(model.context)}${assigned ? ` · ${t(`settings.tier_${assigned.tier}`)} #${assigned.index + 1}` : ""}`}
    >
      <span className="truncate max-w-[140px]">{model.name}</span>
      <span className="text-muted-foreground">{fmtCtx(model.context)}</span>
      {assigned && (
        <Badge variant="secondary" className="text-[9px] h-auto px-1 py-0">
          {t(`settings.tier_${assigned.tier}_short`)}#{assigned.index + 1}
        </Badge>
      )}
    </div>
  );
}

function resolveDropIndex(e: React.DragEvent, length: number): number {
  const rect = (e.currentTarget as HTMLElement).getBoundingClientRect();
  const y = e.clientY - rect.top;
  const itemHeight = 40;
  return Math.max(0, Math.min(length, Math.floor(y / itemHeight)));
}
