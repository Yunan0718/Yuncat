import { useEffect, useState, useCallback } from "react";
import { useTranslation } from "react-i18next";
import { useEvolutionPending, EvolutionPendingItem } from "../hooks/useEvolutionPending";
import { EvolutionPendingCard } from "../components/evolution/EvolutionPendingCard";
import { useAppStore } from "../store/appStore";
import { Button } from "../components/ui/button";
import { Tabs, TabsList, TabsTrigger } from "../components/ui/tabs";
import { LoadingCurve } from "../components/common/LoadingCurve";
import { EmptyState } from "../components/common/EmptyState";
import { Sparkles, RefreshCw } from "lucide-react";
import { emitGlobalToast } from "../components/common/toast-context";

type StatusFilter = "all" | "pending" | "approved" | "rejected";

export function EvolutionPendingPage() {
  const { t } = useTranslation();
  const { listPending, approve, reject } = useEvolutionPending();
  const backendReady = useAppStore((s) => s.backendReady);

  const [items, setItems] = useState<EvolutionPendingItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [actionId, setActionId] = useState<string | null>(null);
  const [filter, setFilter] = useState<StatusFilter>("all");

  const load = useCallback(async () => {
    if (!backendReady) return;
    setLoading(true);
    try {
      const status = filter === "all" ? undefined : filter;
      const data = await listPending(status);
      setItems(data.items || []);
    } catch {
      emitGlobalToast(t("evolution.pending.loadError"), "error");
    } finally {
      setLoading(false);
    }
  }, [backendReady, filter, listPending, t]);

  useEffect(() => {
    load();
  }, [load]);

  const handleApprove = async (id: string) => {
    setActionId(id);
    try {
      await approve(id);
      emitGlobalToast(t("evolution.pending.approveSuccess"), "success");
      await load();
    } catch {
      emitGlobalToast(t("evolution.pending.approveError"), "error");
    } finally {
      setActionId(null);
    }
  };

  const handleReject = async (id: string) => {
    setActionId(id);
    try {
      await reject(id);
      emitGlobalToast(t("evolution.pending.rejectSuccess"), "info");
      await load();
    } catch {
      emitGlobalToast(t("evolution.pending.rejectError"), "error");
    } finally {
      setActionId(null);
    }
  };

  return (
    <div className="h-full flex flex-col p-4 md:p-6 overflow-hidden">
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-2">
          <Sparkles size={20} className="text-primary" />
          <h1 className="text-xl font-semibold">{t("evolution.pending.title")}</h1>
        </div>
        <Button variant="ghost" size="sm" onClick={load} disabled={loading}>
          <RefreshCw size={14} className={`mr-1 ${loading ? "animate-spin" : ""}`} />
          {t("evolution.pending.refresh")}
        </Button>
      </div>

      <Tabs value={filter} onValueChange={(v) => setFilter(v as StatusFilter)} className="mb-4">
        <TabsList>
          <TabsTrigger value="all">{t("evolution.pending.filterAll")}</TabsTrigger>
          <TabsTrigger value="pending">{t("evolution.pending.filterPending")}</TabsTrigger>
          <TabsTrigger value="approved">{t("evolution.pending.filterApproved")}</TabsTrigger>
          <TabsTrigger value="rejected">{t("evolution.pending.filterRejected")}</TabsTrigger>
        </TabsList>
      </Tabs>

      <div className="flex-1 overflow-y-auto pr-1">
        {loading && items.length === 0 ? (
          <div className="flex items-center justify-center h-64">
            <LoadingCurve variant="lissajous" size={40} particleCount={10} />
          </div>
        ) : items.length === 0 ? (
          <div className="h-64">
            <EmptyState icon={<Sparkles size={20} />} title={t("evolution.pending.empty")} />
          </div>
        ) : (
          items.map((item) => (
            <EvolutionPendingCard
              key={item.id}
              item={item}
              onApprove={handleApprove}
              onReject={handleReject}
              loading={actionId === item.id}
            />
          ))
        )}
      </div>
    </div>
  );
}
