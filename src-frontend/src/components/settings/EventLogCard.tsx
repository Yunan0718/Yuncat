import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { RefreshCw } from "lucide-react";
import { useApi } from "../../hooks/useApi";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../ui/card";

interface OutboxEvent {
  seq: number;
  id: string;
  topic: string;
  payload: Record<string, unknown> | string;
  created_at: number;
}

interface ReplayResponse {
  events: OutboxEvent[];
  last_seq: number;
}

/** 事件日志只保留最近 N 条——这只是诊断视图，不是全量归档。 */
const EVENT_LOG_LIMIT = 100;

function formatTime(ts: number): string {
  try {
    return new Date(ts * 1000).toLocaleTimeString();
  } catch {
    return "—";
  }
}

function payloadPreview(payload: OutboxEvent["payload"]): string {
  const raw = typeof payload === "string" ? payload : JSON.stringify(payload);
  return raw.length > 160 ? `${raw.slice(0, 160)}…` : raw;
}

/**
 * 事件日志（`GET /events/replay` 此前只有断线重连在用）。
 *
 * 显示 event_outbox 里最近的持久化事件（任务生命周期 / 审批），
 * 排查"任务卡在哪一步""审批有没有发出去"时比翻日志直观。
 * 取最近 N 条的办法：先拿 last_seq，再从 (last_seq - N) 之后重放。
 */
export function EventLogCard() {
  const { t } = useTranslation();
  const { api } = useApi();
  const [events, setEvents] = useState<OutboxEvent[] | null>(null);
  const [lastSeq, setLastSeq] = useState(0);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const head = await api<ReplayResponse>("/api/v1/events/replay?since_seq=0&limit=1");
      const since = Math.max(0, (head.last_seq || 0) - EVENT_LOG_LIMIT);
      const tail = await api<ReplayResponse>(
        `/api/v1/events/replay?since_seq=${since}&limit=${EVENT_LOG_LIMIT}`,
      );
      setEvents(tail.events || []);
      setLastSeq(tail.last_seq || 0);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setEvents([]);
    }
  }, [api]);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <Card>
      <CardHeader className="flex-row items-start justify-between space-y-0">
        <div>
          <CardTitle className="text-base">{t("diagnostics.eventLogTitle")}</CardTitle>
          <CardDescription>{t("diagnostics.eventLogDesc", { count: EVENT_LOG_LIMIT })}</CardDescription>
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
      <CardContent className="space-y-2">
        {error && <div role="alert" className="text-xs text-destructive">{error}</div>}
        {lastSeq > 0 && (
          <div className="text-[10px] text-muted-foreground">
            {t("diagnostics.eventLogLastSeq", { seq: lastSeq })}
          </div>
        )}
        {events === null ? (
          <div role="status" className="text-xs text-muted-foreground">{t("common.loading")}</div>
        ) : events.length === 0 ? (
          <div className="text-xs text-muted-foreground">{t("diagnostics.eventLogEmpty")}</div>
        ) : (
          <div className="max-h-64 divide-y overflow-y-auto rounded border">
            {[...events].reverse().map((ev) => (
              <div key={`${ev.seq}-${ev.id}`} className="px-2 py-1.5 text-[11px]">
                <div className="flex items-center gap-2">
                  <span className="font-mono text-muted-foreground">#{ev.seq}</span>
                  <span className="font-mono">{ev.topic}</span>
                  <span className="ml-auto text-muted-foreground">{formatTime(ev.created_at)}</span>
                </div>
                <div className="truncate font-mono text-[10px] text-muted-foreground" title={payloadPreview(ev.payload)}>
                  {payloadPreview(ev.payload)}
                </div>
              </div>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
