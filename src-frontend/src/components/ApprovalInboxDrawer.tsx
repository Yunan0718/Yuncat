import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { CheckIcon, CrossIcon } from "./common/Icons";
import { DetailDrawer } from "./DetailDrawer";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "./ui/tabs";
import { Button } from "./ui/button";
import { LoadingCurve } from "./common/LoadingCurve";
import { EmptyState } from "./common/EmptyState";
import { useToast } from "../hooks/use-toast";
import { useAppStore } from "../store/appStore";
import { useApprovalInbox, type ApprovalItem } from "../hooks/useApprovalInbox";

const STATUS_LABEL_KEY = {
  resolved: "inbox.status.resolved",
  rejected: "inbox.status.rejected",
  expired: "inbox.status.expired",
} as const;

function formatTime(timestamp: number): string {
  try {
    return new Date(timestamp * 1000).toLocaleString();
  } catch {
    return "";
  }
}

function StatusBadge({ status }: { status: ApprovalItem["status"] }) {
  const { t } = useTranslation();
  const known = status === "resolved" || status === "rejected" || status === "expired";
  const className = status === "resolved"
    ? "bg-success/10 text-success"
    : status === "rejected"
      ? "bg-destructive/10 text-destructive"
      : "bg-muted text-muted-foreground";
  return (
    <span className={`rounded px-1.5 py-0.5 text-2xs ${className}`}>
      {t(known ? STATUS_LABEL_KEY[status] : "inbox.status.unknown")}
    </span>
  );
}

function ApprovalCard({
  item,
  onApprove,
  onReject,
  pending,
  acting,
  queueBusy,
}: {
  item: ApprovalItem;
  onApprove: (token: string, remember: boolean) => Promise<void>;
  onReject: (token: string) => Promise<void>;
  pending?: boolean;
  acting?: boolean;
  queueBusy?: boolean;
}) {
  const { t } = useTranslation();
  const payload = item.payload || {};
  const title = item.title || String(payload.path || payload.command || item.state_type || "");
  const [remember, setRemember] = useState(false);
  const [now, setNow] = useState(() => Date.now() / 1000);

  useEffect(() => {
    if (!pending) return;
    const timer = setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => clearInterval(timer);
  }, [pending]);

  const expired = Boolean(item.expires_at && item.expires_at <= now);
  const disabled = Boolean(acting || queueBusy || expired);

  return (
    <div className="rounded border border-border bg-card p-3 text-xs" aria-busy={acting}>
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0 flex-1">
          <div className="truncate font-medium">{title}</div>
          <div className="mt-0.5 text-muted-foreground">{item.state_type}</div>
          {item.task_id && (
            <div className="mt-0.5 text-2xs text-muted-foreground">
              {t("inbox.task")}: {item.task_id.slice(0, 8)}
            </div>
          )}
        </div>
        {item.status && !pending && <StatusBadge status={item.status} />}
      </div>

      <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-2xs text-muted-foreground">
        <span>{formatTime(item.created_at)}</span>
        {pending && item.expires_at > 0 && (
          <span className={expired ? "text-destructive" : ""}>
            {expired ? t("inbox.expired") : t("inbox.expiresAt", { at: formatTime(item.expires_at) })}
          </span>
        )}
      </div>

      {Object.keys(payload).length > 0 && (
        <details className="mt-2 rounded border bg-muted/20 px-2 py-1.5">
          <summary className="cursor-pointer text-2xs text-muted-foreground">{t("inbox.details")}</summary>
          <pre className="mt-2 max-h-40 overflow-auto whitespace-pre-wrap break-all font-mono text-[10px]">
            {JSON.stringify(payload, null, 2)}
          </pre>
        </details>
      )}

      {pending && (
        <div className="mt-3">
          <label className="mb-2 flex cursor-pointer select-none items-center gap-1.5 text-2xs text-muted-foreground">
            <input
              type="checkbox"
              checked={remember}
              onChange={(event) => setRemember(event.target.checked)}
              disabled={disabled}
              className="h-3 w-3 accent-primary"
            />
            {t("inbox.remember")}
          </label>
          <div className="flex items-center gap-2">
            <Button
              type="button"
              onClick={() => void onApprove(item.id, remember)}
              variant="default"
              size="xs"
              disabled={disabled}
              className="bg-success-solid text-success-foreground hover:bg-success-solid/90"
            >
              <CheckIcon size={10} />
              {acting ? t("toolConfirm.processing") : t("common.confirm")}
            </Button>
            <Button
              type="button"
              onClick={() => void onReject(item.id)}
              variant="default"
              size="xs"
              disabled={disabled}
              className="bg-destructive-solid text-destructive-foreground hover:bg-destructive-solid/90"
            >
              <CrossIcon size={10} />
              {t("common.reject")}
            </Button>
          </div>
        </div>
      )}

      {!pending && item.resolution && (
        <div className="mt-2 truncate text-2xs text-muted-foreground" title={item.resolution}>
          {item.resolution}
        </div>
      )}
    </div>
  );
}

/** Phase 107: 统一审批 Inbox 抽屉。 */
export function ApprovalInboxDrawer() {
  const { t } = useTranslation();
  const { toast } = useToast();
  const open = useAppStore((state) => state.inboxOpen);
  const setOpen = useAppStore((state) => state.setInboxOpen);
  const { pending, history, loading, error, refresh, approve, reject } = useApprovalInbox();
  const [activeTab, setActiveTab] = useState("pending");
  const [actingId, setActingId] = useState<string | null>(null);

  useEffect(() => {
    if (open) void refresh();
  }, [open, refresh]);

  const showActionError = (actionError: unknown) => {
    const detail = actionError instanceof Error ? actionError.message : String(actionError);
    toast(t("inbox.actionFailed", { error: detail }), "error");
  };

  const handleApprove = async (token: string, remember = false) => {
    if (actingId) return;
    setActingId(token);
    try {
      await approve(token, remember);
    } catch (actionError) {
      showActionError(actionError);
    } finally {
      setActingId(null);
    }
  };

  const handleReject = async (token: string) => {
    if (actingId) return;
    setActingId(token);
    try {
      await reject(token);
    } catch (actionError) {
      showActionError(actionError);
    } finally {
      setActingId(null);
    }
  };

  const loadingState = (
    <div role="status" className="flex items-center justify-center gap-2 py-4 text-xs text-muted-foreground">
      <LoadingCurve variant="lissajous" size={14} particleCount={6} />
      <span>{t("common.loading")}</span>
    </div>
  );

  return (
    <DetailDrawer open={open} title={t("inbox.title")} onClose={() => setOpen(false)} width="w-full sm:w-[420px]">
      <Tabs value={activeTab} onValueChange={setActiveTab} className="w-full">
        <TabsList className="mb-3 w-full">
          <TabsTrigger value="pending" className="flex-1 text-xs">
            {t("inbox.pending")} ({pending.length})
          </TabsTrigger>
          <TabsTrigger value="history" className="flex-1 text-xs">
            {t("inbox.history")} ({history.length})
          </TabsTrigger>
        </TabsList>

        <TabsContent value="pending" className="mt-0">
          {loading ? loadingState : error ? (
            <p role="alert" className="py-4 text-center text-xs text-destructive">{error}</p>
          ) : pending.length === 0 ? (
            <EmptyState compact title={t("inbox.emptyPending")} />
          ) : (
            <div className="flex flex-col gap-2">
              {pending.map((item) => (
                <ApprovalCard
                  key={item.id}
                  item={item}
                  onApprove={handleApprove}
                  onReject={handleReject}
                  acting={actingId === item.id}
                  queueBusy={Boolean(actingId)}
                  pending
                />
              ))}
            </div>
          )}
        </TabsContent>

        <TabsContent value="history" className="mt-0">
          {loading ? loadingState : error ? (
            <p role="alert" className="py-4 text-center text-xs text-destructive">{error}</p>
          ) : history.length === 0 ? (
            <EmptyState compact title={t("inbox.emptyHistory")} />
          ) : (
            <div className="flex flex-col gap-2">
              {history.map((item) => (
                <ApprovalCard key={item.id} item={item} onApprove={handleApprove} onReject={handleReject} />
              ))}
            </div>
          )}
        </TabsContent>
      </Tabs>
    </DetailDrawer>
  );
}
