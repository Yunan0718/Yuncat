import { useTranslation } from "react-i18next";
import { Layers } from "lucide-react";
import { EmptyState } from "../common/EmptyState";

export interface RunLogNodeResult {
  status?: string;
  output?: string;
  error?: string;
}

export interface RunLog {
  run_id: string;
  status: string;
  error_message?: string;
  node_results?: Record<string, RunLogNodeResult>;
  started_at?: number;
  finished_at?: number;
}

/** DAG 运行历史列表——从 WorkflowEditor 抽出来，避免超大页面继续变长。 */
export function WorkflowRunsList({ runs }: { runs: RunLog[] }) {
  const { t } = useTranslation();
  if (runs.length === 0) {
    return <EmptyState icon={<Layers size={20} />} title={t("workflows.noRunRecords")} compact />;
  }
  return (
    <div className="space-y-3">
      {runs.map((r) => (
        <div key={r.run_id} className="border rounded-lg p-3 text-xs">
          <div className="flex items-center justify-between mb-2">
            <span className="font-mono text-[10px]">{r.run_id.slice(0, 12)}</span>
            <span
              className={`px-1.5 py-0.5 rounded text-[10px] font-medium ${r.status === "completed" ? "bg-success/10 text-success" : r.status === "failed" ? "bg-destructive/10 text-destructive" : "bg-warning/10 text-warning"}`}
            >
              {r.status}
            </span>
          </div>
          {r.error_message && (
            <div className="text-destructive mb-2 bg-destructive/10 p-2 rounded">{r.error_message}</div>
          )}
          {Object.entries(r.node_results || {}).map(([nid, nr]) => (
            <div key={nid} className="mb-2 last:mb-0">
              <div className="flex items-center gap-2 mb-0.5">
                <span className="font-mono text-[10px] font-semibold">{nid}</span>
                <span
                  className={`text-[9px] px-1 rounded ${nr?.status === "done" ? "bg-success/10 text-success" : nr?.status === "failed" ? "bg-destructive/10 text-destructive" : "bg-muted text-foreground-secondary"}`}
                >
                  {nr?.status || "pending"}
                </span>
              </div>
              {nr?.output && (
                <pre className="text-[10px] bg-muted p-2 rounded max-h-24 overflow-y-auto whitespace-pre-wrap font-mono break-all">
                  {nr.output}
                </pre>
              )}
              {nr?.error && (
                <div className="text-[10px] text-destructive bg-destructive/10 p-2 rounded mt-0.5">
                  {nr.error}
                </div>
              )}
            </div>
          ))}
          <div className="text-[9px] text-muted-foreground mt-1.5 pt-1.5 border-t">
            {r.started_at ? new Date(r.started_at * 1000).toLocaleString() : "?"} →{" "}
            {r.finished_at ? new Date(r.finished_at * 1000).toLocaleString() : t("workflows.running")}
          </div>
        </div>
      ))}
    </div>
  );
}
