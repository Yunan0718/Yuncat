import { useTranslation } from "react-i18next";
import { Card, CardContent, CardHeader, CardTitle } from "../ui/card";
import { Button } from "../ui/button";
import { Badge } from "../ui/badge";
import { EvolutionPendingItem } from "../../hooks/useEvolutionPending";
import { Check, X, ChevronDown, ChevronUp, FlaskConical, Swords } from "lucide-react";
import { useState } from "react";

interface EvolutionPendingCardProps {
  item: EvolutionPendingItem;
  onApprove?: (id: string) => void;
  onReject?: (id: string) => void;
  loading?: boolean;
}

export function EvolutionPendingCard({ item, onApprove, onReject, loading }: EvolutionPendingCardProps) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState(false);

  const score = item.evaluation?.score ?? 0;
  const status = item.status;
  const isPending = status === "pending";

  const statusVariant: Record<string, "default" | "secondary" | "destructive" | "outline"> = {
    pending: "default",
    approved: "secondary",
    rejected: "destructive",
    auto_rejected: "outline",
  };

  const statusLabel: Record<string, string> = {
    pending: t("evolution.pending.statusPending"),
    approved: t("evolution.pending.statusApproved"),
    rejected: t("evolution.pending.statusRejected"),
    auto_rejected: t("evolution.pending.statusAutoRejected"),
  };

  const formatTime = (ts: number) => {
    try {
      return new Date(ts * 1000).toLocaleString();
    } catch {
      return "";
    }
  };

  return (
    <Card className="mb-3">
      <CardHeader className="pb-2">
        <div className="flex items-start justify-between gap-3">
          <div className="flex-1 min-w-0">
            <CardTitle className="text-base truncate">
              {t("evolution.pending.titleItem", { taskType: item.task_type })}
            </CardTitle>
            <p className="text-xs text-muted-foreground mt-0.5">
              {item.workflow_id} · {formatTime(item.created_at)}
            </p>
          </div>
          <Badge variant={statusVariant[status] ?? "default"}>{statusLabel[status] ?? status}</Badge>
        </div>
      </CardHeader>
      <CardContent className="pt-0">
        <div className="flex flex-wrap items-center gap-3 text-sm mb-3">
          <div className="flex items-center gap-1.5">
            <FlaskConical size={14} className="text-muted-foreground" />
            <span>
              {t("evolution.pending.sandboxSummary", {
                completion: Math.round((item.sandbox_result?.completion_rate ?? 0) * 100),
                success: Math.round((item.sandbox_result?.node_success_rate ?? 0) * 100),
              })}
            </span>
          </div>
          <div className="flex items-center gap-1.5">
            <Swords size={14} className="text-muted-foreground" />
            <span>
              {t("evolution.pending.abSummary", {
                winRate: Math.round((item.ab_result?.win_rate ?? 0) * 100),
              })}
            </span>
          </div>
          <div className="flex items-center gap-1.5">
            <span className="text-muted-foreground">{t("evolution.pending.score")}</span>
            <span className="font-medium">{score.toFixed(2)}</span>
          </div>
        </div>

        {expanded && (
          <div className="text-sm space-y-2 border-t pt-2">
            <p className="text-muted-foreground">{item.evaluation?.reason ?? ""}</p>
            {item.examples.length > 0 && (
              <div>
                <span className="font-medium">{t("evolution.pending.examples")}</span>
                <ul className="list-disc list-inside text-muted-foreground">
                  {item.examples.map((ex, idx) => (
                    <li key={idx} className="truncate">
                      {ex}
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        )}

        <div className="flex items-center justify-between mt-3">
          <Button variant="ghost" size="sm" onClick={() => setExpanded((v) => !v)}>
            {expanded ? <ChevronUp size={14} className="mr-1" /> : <ChevronDown size={14} className="mr-1" />}
            {expanded ? t("evolution.pending.collapse") : t("evolution.pending.expand")}
          </Button>
          {isPending && (
            <div className="flex gap-2">
              <Button
                variant="outline"
                size="sm"
                onClick={() => onReject?.(item.id)}
                disabled={loading}
                aria-label={t("evolution.pending.reject")}
              >
                <X size={14} className="mr-1" />
                {t("evolution.pending.reject")}
              </Button>
              <Button
                size="sm"
                onClick={() => onApprove?.(item.id)}
                disabled={loading}
                aria-label={t("evolution.pending.approve")}
              >
                <Check size={14} className="mr-1" />
                {t("evolution.pending.approve")}
              </Button>
            </div>
          )}
        </div>
      </CardContent>
    </Card>
  );
}
