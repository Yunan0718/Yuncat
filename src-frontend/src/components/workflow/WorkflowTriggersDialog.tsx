import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Play, Trash2, Zap } from "lucide-react";
import { useApi } from "../../hooks/useApi";
import { useConfirmDialog } from "../../hooks/useConfirmDialog";
import { emitGlobalToast } from "../common/toast-context";
import {
  Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle,
} from "../ui/dialog";

interface TriggerRow {
  id: string;
  workflow_id: string;
  trigger_type: "event" | "cron" | "manual" | string;
  config: Record<string, unknown>;
  enabled: boolean | number;
  last_run_at?: number | null;
  next_run_at?: number | null;
  run_count?: number;
  created_at?: number;
}

type TriggerType = "event" | "cron" | "manual";

function formatTime(ts?: number | null): string {
  if (!ts) return "—";
  try {
    return new Date(ts * 1000).toLocaleString();
  } catch {
    return "—";
  }
}

/** 配置摘要：event → 事件名，cron → 表达式，manual → 手动。 */
function configSummary(row: TriggerRow): string {
  const cfg = row.config || {};
  if (row.trigger_type === "event") return String(cfg.event || "");
  if (row.trigger_type === "cron") return String(cfg.cron_expr || "");
  return String(cfg.initial_input || "");
}

/**
 * 工作流触发器管理（Phase 101 的后端接口一直没接线）。
 *
 * 后端早就有 list/create/delete/fire 四个端点，但桌面端没有任何入口：
 * 建不了触发器、也删不掉已有的——"工作流可被事件/cron 触发"这件事
 * 在界面上完全不可用。这里补一个自包含的弹窗。
 */
export function WorkflowTriggersDialog({
  workflowId,
  open,
  onClose,
}: {
  workflowId: string;
  open: boolean;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const { api } = useApi();
  const { confirm, confirmDialog } = useConfirmDialog();
  const [rows, setRows] = useState<TriggerRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [type, setType] = useState<TriggerType>("event");
  const [spec, setSpec] = useState("");
  const [initialInput, setInitialInput] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    if (!workflowId) return;
    setError(null);
    try {
      const data = await api<TriggerRow[]>(`/api/v1/workflows/${workflowId}/triggers`);
      setRows(Array.isArray(data) ? data : []);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setRows([]);
    }
  }, [api, workflowId]);

  useEffect(() => {
    if (open) void load();
  }, [open, load]);

  const create = async () => {
    if (!workflowId) return;
    const config: Record<string, unknown> = {};
    if (type === "event") {
      if (!spec.trim()) {
        emitGlobalToast(t("workflowTriggers.eventRequired"), "error");
        return;
      }
      config.event = spec.trim();
    } else if (type === "cron") {
      if (!spec.trim()) {
        emitGlobalToast(t("workflowTriggers.cronRequired"), "error");
        return;
      }
      config.cron_expr = spec.trim();
    }
    if (initialInput.trim()) config.initial_input = initialInput.trim();
    setBusy(true);
    try {
      await api(`/api/v1/workflows/${workflowId}/triggers`, {
        method: "POST",
        body: JSON.stringify({ trigger_type: type, config }),
      });
      emitGlobalToast(t("workflowTriggers.created"), "success");
      setSpec("");
      setInitialInput("");
      await load();
    } catch (e) {
      emitGlobalToast(`${t("workflowTriggers.failed")}: ${e instanceof Error ? e.message : String(e)}`, "error");
    } finally {
      setBusy(false);
    }
  };

  const remove = async (row: TriggerRow) => {
    const ok = await confirm({
      title: t("workflowTriggers.deleteTitle"),
      description: t("workflowTriggers.deleteConfirm", { type: row.trigger_type }),
      variant: "destructive",
      confirmText: t("workflowTriggers.delete"),
    });
    if (!ok) return;
    try {
      await api(`/api/v1/workflows/${workflowId}/triggers/${row.id}`, { method: "DELETE" });
      emitGlobalToast(t("workflowTriggers.deleted"), "success");
      await load();
    } catch (e) {
      emitGlobalToast(`${t("workflowTriggers.failed")}: ${e instanceof Error ? e.message : String(e)}`, "error");
    }
  };

  const fire = async (row: TriggerRow) => {
    try {
      await api(`/api/v1/workflows/${workflowId}/triggers/${row.id}/fire`, {
        method: "POST",
        body: JSON.stringify({}),
      });
      emitGlobalToast(t("workflowTriggers.fired"), "success");
      await load();
    } catch (e) {
      emitGlobalToast(`${t("workflowTriggers.failed")}: ${e instanceof Error ? e.message : String(e)}`, "error");
    }
  };

  return (
    <Dialog open={open} onOpenChange={(v) => { if (!v) onClose(); }}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Zap size={16} />
            {t("workflowTriggers.title")}
          </DialogTitle>
          <DialogDescription>{t("workflowTriggers.description")}</DialogDescription>
        </DialogHeader>

        {error && <div role="alert" className="text-xs text-destructive">{error}</div>}

        <div className="flex flex-wrap items-center gap-2 rounded-lg border p-2">
          <select
            value={type}
            onChange={(e) => { setType(e.target.value as TriggerType); setSpec(""); }}
            aria-label={t("workflowTriggers.typeLabel")}
            className="rounded border bg-background px-2 py-1 text-xs"
          >
            <option value="event">{t("workflowTriggers.typeEvent")}</option>
            <option value="cron">{t("workflowTriggers.typeCron")}</option>
            <option value="manual">{t("workflowTriggers.typeManual")}</option>
          </select>
          {type !== "manual" && (
            <input
              value={spec}
              onChange={(e) => setSpec(e.target.value)}
              placeholder={type === "event" ? t("workflowTriggers.eventPlaceholder") : t("workflowTriggers.cronPlaceholder")}
              aria-label={type === "event" ? t("workflowTriggers.eventPlaceholder") : t("workflowTriggers.cronPlaceholder")}
              className="min-w-40 flex-1 rounded border bg-background px-2 py-1 text-xs font-mono"
            />
          )}
          <input
            value={initialInput}
            onChange={(e) => setInitialInput(e.target.value)}
            placeholder={t("workflowTriggers.initialInput")}
            aria-label={t("workflowTriggers.initialInput")}
            className="min-w-40 flex-1 rounded border bg-background px-2 py-1 text-xs"
          />
          <button
            type="button"
            onClick={() => void create()}
            disabled={busy}
            className="rounded bg-primary px-3 py-1 text-xs font-semibold text-primary-foreground disabled:opacity-50"
          >
            {t("workflowTriggers.create")}
          </button>
        </div>

        {rows === null ? (
          <div role="status" className="py-4 text-center text-xs text-muted-foreground">{t("common.loading")}</div>
        ) : rows.length === 0 ? (
          <div className="py-6 text-center text-xs text-muted-foreground">{t("workflowTriggers.empty")}</div>
        ) : (
          <div className="divide-y rounded-lg border">
            {rows.map((row) => (
              <div key={row.id} className="flex items-start gap-2 px-3 py-2">
                <div className="min-w-0 flex-1 space-y-1">
                  <div className="flex items-center gap-2">
                    <span className="rounded bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">
                      {t(`workflowTriggers.types.${row.trigger_type}`, { defaultValue: row.trigger_type })}
                    </span>
                    <span className="truncate font-mono text-xs">{configSummary(row) || "—"}</span>
                    <span className="text-[10px] text-muted-foreground">
                      {row.enabled ? t("workflowTriggers.enabled") : t("workflowTriggers.disabled")}
                    </span>
                  </div>
                  <div className="flex flex-wrap gap-x-3 text-[10px] text-muted-foreground">
                    <span>{t("workflowTriggers.runCount", { count: row.run_count ?? 0 })}</span>
                    <span>{t("workflowTriggers.nextRun", { at: formatTime(row.next_run_at) })}</span>
                  </div>
                </div>
                <button
                  type="button"
                  onClick={() => void fire(row)}
                  aria-label={t("workflowTriggers.fire")}
                  title={t("workflowTriggers.fire")}
                  className="shrink-0 rounded p-1 text-muted-foreground hover:text-foreground"
                >
                  <Play size={13} />
                </button>
                <button
                  type="button"
                  onClick={() => void remove(row)}
                  aria-label={t("workflowTriggers.delete")}
                  title={t("workflowTriggers.delete")}
                  className="shrink-0 rounded p-1 text-muted-foreground hover:text-destructive"
                >
                  <Trash2 size={13} />
                </button>
              </div>
            ))}
          </div>
        )}
        {confirmDialog}
      </DialogContent>
    </Dialog>
  );
}
