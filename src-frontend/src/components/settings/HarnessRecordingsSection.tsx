import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Play, Video } from "lucide-react";
import { useApi } from "../../hooks/useApi";
import { emitGlobalToast } from "../common/toast-context";
import { LoadingCurve } from "../common/LoadingCurve";
import { EmptyState } from "../common/EmptyState";

interface RecordingRow {
  id: string;
  task_id: string;
  created_at: number;
}

interface RecordingDetail extends RecordingRow {
  turn_data: {
    tool_name?: string;
    arguments?: Record<string, unknown>;
    result?: Record<string, unknown>;
  };
}

interface ReplayResult {
  status: string;
  match?: boolean;
  diffs?: string[];
  original?: Record<string, unknown>;
  replayed?: Record<string, unknown>;
  message?: string;
}

function shortId(id: string): string {
  return id.length > 12 ? `${id.slice(0, 8)}…` : id;
}

function formatTime(ts: number): string {
  try {
    return new Date(ts * 1000).toLocaleString();
  } catch {
    return "—";
  }
}

/**
 * Harness 录制回放（设置 → 诊断）。
 *
 * 后端 list/get/replay 三个端点一直没有 UI，而且 `record_turn` 此前**从未被调用**——
 * 录制表永远是空的。现在工具执行完会把 (tool_name, arguments, result) 录下来
 *（仅 YUNCAT_HARNESS_RECORD=1 时），这里可以浏览并用相同参数重跑工具、看结果是否一致。
 */
export function HarnessRecordingsSection() {
  const { t } = useTranslation();
  const { api } = useApi();
  const [rows, setRows] = useState<RecordingRow[] | null>(null);
  const [detail, setDetail] = useState<RecordingDetail | null>(null);
  const [replay, setReplay] = useState<ReplayResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setError(null);
    try {
      const data = await api<RecordingRow[]>("/api/v1/harness/recordings?limit=50");
      setRows(Array.isArray(data) ? data : []);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setRows([]);
    }
  }, [api]);

  useEffect(() => {
    void load();
  }, [load]);

  async function openDetail(row: RecordingRow) {
    setReplay(null);
    try {
      const d = await api<RecordingDetail>(`/api/v1/harness/recordings/${row.id}`);
      setDetail(d);
    } catch (e) {
      emitGlobalToast(`${t("harness.loadDetailFailed")}: ${e instanceof Error ? e.message : String(e)}`, "error");
    }
  }

  async function runReplay() {
    if (!detail) return;
    setBusy(true);
    try {
      const result = await api<ReplayResult>(
        `/api/v1/harness/recordings/${detail.id}/replay`,
        { method: "POST" },
      );
      setReplay(result);
      if (result.status !== "ok") {
        emitGlobalToast(result.message || t("harness.replayFailed"), "error");
      }
    } catch (e) {
      emitGlobalToast(`${t("harness.replayFailed")}: ${e instanceof Error ? e.message : String(e)}`, "error");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-4">
      <div>
        <h2 className="text-xl font-semibold tracking-tight flex items-center gap-2">
          <Video size={18} />
          {t("harness.title")}
        </h2>
        <p className="text-sm text-muted-foreground mt-1">{t("harness.description")}</p>
      </div>

      {error && <div role="alert" className="text-sm text-destructive">{error}</div>}

      {rows === null ? (
        <LoadingCurve />
      ) : rows.length === 0 ? (
        <EmptyState title={t("harness.empty")} description={t("harness.emptyHint")} />
      ) : (
        <div className="divide-y rounded-lg border">
          {rows.map((row) => (
            <button
              key={row.id}
              type="button"
              onClick={() => void openDetail(row)}
              className={`flex w-full items-center gap-3 px-3 py-2 text-left text-xs hover:bg-secondary ${
                detail?.id === row.id ? "bg-secondary" : ""
              }`}
            >
              <span className="font-mono">{shortId(row.id)}</span>
              <span className="text-muted-foreground">task {shortId(row.task_id)}</span>
              <span className="ml-auto text-muted-foreground">{formatTime(row.created_at)}</span>
            </button>
          ))}
        </div>
      )}

      {detail && (
        <div className="space-y-2 rounded-lg border p-3">
          <div className="flex items-center gap-2 text-xs">
            <span className="font-mono font-semibold">{detail.turn_data.tool_name || "—"}</span>
            <span className="text-muted-foreground">
              {t("harness.argumentCount", { count: Object.keys(detail.turn_data.arguments || {}).length })}
            </span>
            <button
              type="button"
              onClick={() => void runReplay()}
              disabled={busy}
              className="ml-auto rounded bg-primary px-2 py-1 text-[11px] font-semibold text-primary-foreground disabled:opacity-50"
            >
              <Play size={11} className="inline" /> {t("harness.replay")}
            </button>
          </div>
          <pre className="max-h-40 overflow-auto rounded bg-muted p-2 text-[10px] font-mono whitespace-pre-wrap">
            {JSON.stringify(detail.turn_data.arguments || {}, null, 2)}
          </pre>

          {replay && (
            <div className="space-y-1 rounded border p-2 text-[11px]">
              <div className={replay.match ? "text-success" : "text-warning"}>
                {replay.status !== "ok"
                  ? replay.message || t("harness.replayFailed")
                  : replay.match
                    ? t("harness.replayMatch")
                    : t("harness.replayDiff")}
              </div>
              {(replay.diffs || []).map((d) => (
                <div key={d} className="font-mono text-[10px] text-muted-foreground">{d}</div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
