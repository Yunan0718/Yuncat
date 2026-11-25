import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { RefreshCw } from "lucide-react";
import { useApi } from "../../hooks/useApi";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../ui/card";

interface PendingTrigger {
  id: string;
  loop_id: string;
  trigger_type: string;
  priority: number;
  status: string;
  created_at: number;
}

function formatTime(ts: number): string {
  try {
    return new Date(ts * 1000).toLocaleString();
  } catch {
    return "—";
  }
}

/** 待处理的 hook / 事件触发队列（`GET /hooks/triggers/pending` 一直没接线）。 */
export function PendingTriggersCard() {
  const { t } = useTranslation();
  const { api } = useApi();
  const [rows, setRows] = useState<PendingTrigger[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const data = await api<PendingTrigger[]>("/api/v1/hooks/triggers/pending");
      setRows(Array.isArray(data) ? data : []);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setRows([]);
    }
  }, [api]);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <Card>
      <CardHeader className="flex-row items-start justify-between space-y-0">
        <div>
          <CardTitle className="text-base">{t("diagnostics.pendingTriggersTitle")}</CardTitle>
          <CardDescription>{t("diagnostics.pendingTriggersDesc")}</CardDescription>
        </div>
        <button
          type="button"
          onClick={() => void load()}
          aria-label={t("diagnostics.refresh")}
          title={t("diagnostics.refresh")}
          className="rounded p-1 text-muted-foreground hover:text-foreground"
        >
          <RefreshCw size={14} />
        </button>
      </CardHeader>
      <CardContent>
        {error && <div role="alert" className="text-xs text-destructive">{error}</div>}
        {rows === null ? (
          <div role="status" className="text-xs text-muted-foreground">{t("common.loading")}</div>
        ) : rows.length === 0 ? (
          <div className="text-xs text-muted-foreground">{t("diagnostics.pendingTriggersEmpty")}</div>
        ) : (
          <div className="divide-y rounded border">
            {rows.map((row) => (
              <div key={row.id} className="flex flex-wrap items-center gap-2 px-2 py-1.5 text-[11px]">
                <span className="font-mono">{row.loop_id || "—"}</span>
                <span className="text-muted-foreground">{row.trigger_type}</span>
                <span className="text-muted-foreground">P{row.priority}</span>
                <span className="ml-auto text-muted-foreground">{formatTime(row.created_at)}</span>
              </div>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
