import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Pause, Play, Square, ExternalLink } from "lucide-react";
import { useApi } from "../hooks/useApi";
import { emitGlobalToast } from "./common/toast-context";
import { Button } from "./ui/button";

interface JobItem {
  id: string;
  prompt: string;
  status: string;
  agent_id: string;
  output_preview: string;
  round?: number;
  tool_round?: number;
  model_used?: { provider?: string; model?: string };
  session_id: string;
}

const FILTERS = ["active", "terminal", "all"] as const;
type JobFilter = typeof FILTERS[number];

const STATUS_CLASS: Record<string, string> = {
  running: "bg-primary/10 text-primary",
  pending: "bg-muted text-muted-foreground",
  paused: "bg-warning/10 text-warning",
  paused_budget: "bg-warning/10 text-warning",
  interrupted: "bg-warning/10 text-warning",
  completed: "bg-success/10 text-success",
  failed: "bg-destructive/10 text-destructive",
  cancelled: "bg-muted text-muted-foreground",
};

export function JobsPanel({ onOpenTask, sessionId }: { onOpenTask?: (taskId: string) => void; sessionId?: string | null }) {
  const { t } = useTranslation();
  const { api } = useApi();
  const [filter, setFilter] = useState<JobFilter>("active");
  const [jobs, setJobs] = useState<JobItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);

  const load = useCallback(async (nextFilter: JobFilter = filter) => {
    setLoading(true);
    try {
      const scope = sessionId ? `&session_id=${encodeURIComponent(sessionId)}` : "";
      const result = await api<{ items: JobItem[] }>(`/api/v1/jobs?status=${nextFilter}&limit=40${scope}`);
      setJobs(result.items || []);
    } catch (error) {
      emitGlobalToast(`${t("jobs.loadFailed")}: ${String(error)}`, "error");
    } finally {
      setLoading(false);
    }
  }, [api, filter, sessionId, t]);

  useEffect(() => {
    void load(filter);
    const timer = setInterval(() => void load(filter), 3000);
    return () => clearInterval(timer);
  }, [filter, load]);

  const act = async (jobId: string, action: "pause" | "resume" | "cancel") => {
    setBusyId(jobId);
    try {
      await api(`/api/v1/tasks/${jobId}/${action}`, { method: "POST" });
      await load();
    } catch (error) {
      emitGlobalToast(`${t("jobs.actionFailed")}: ${String(error)}`, "error");
    } finally {
      setBusyId(null);
    }
  };

  return (
    <div className="max-h-72 overflow-y-auto">
      <div className="flex items-center gap-1 mb-2">
        {FILTERS.map((item) => (
          <button
            key={item}
            type="button"
            onClick={() => setFilter(item)}
            className={`px-2 py-0.5 rounded text-[10px] ${filter === item ? "bg-secondary text-foreground" : "text-muted-foreground"}`}
          >
            {t(`jobs.filter.${item}`)}
          </button>
        ))}
        {loading && <span className="text-[10px] text-muted-foreground ml-auto">{t("common.loading")}</span>}
      </div>
      {!loading && jobs.length === 0 && <p className="text-[10px] text-muted-foreground">{t("jobs.empty")}</p>}
      <div className="space-y-1">
        {jobs.map((job) => {
          const terminal = ["completed", "failed", "cancelled"].includes(job.status);
          return (
            <div key={job.id} className="rounded border bg-background/60 px-2 py-1.5">
              <div className="flex items-start gap-2">
                <span className={`shrink-0 rounded px-1.5 py-0.5 text-[9px] ${STATUS_CLASS[job.status] || STATUS_CLASS.pending}`}>
                  {t(`jobs.status.${job.status}`, { defaultValue: job.status })}
                </span>
                <button type="button" className="min-w-0 flex-1 text-left" onClick={() => onOpenTask?.(job.id)}>
                  <span className="block text-[11px] truncate">{job.prompt || job.id.slice(0, 8)}</span>
                  <span className="block text-[9px] text-muted-foreground">
                    {job.agent_id}{job.round ? ` · round ${job.round}` : ""}{job.model_used?.model ? ` · ${job.model_used.model}` : ""}
                  </span>
                  {job.output_preview && <span className="block text-[10px] text-muted-foreground truncate mt-0.5">{job.output_preview}</span>}
                </button>
                <Button size="icon" variant="ghost" className="h-6 w-6" aria-label={t("jobs.open")} onClick={() => onOpenTask?.(job.id)}>
                  <ExternalLink size={11} />
                </Button>
              </div>
              {!terminal && (
                <div className="flex gap-1 mt-1">
                  {job.status === "running" && (
                    <Button size="xs" variant="outline" disabled={busyId === job.id} onClick={() => void act(job.id, "pause")}>
                      <Pause size={10} /> {t("jobs.pause")}
                    </Button>
                  )}
                  {["paused", "paused_budget", "interrupted"].includes(job.status) && (
                    <Button size="xs" variant="outline" disabled={busyId === job.id} onClick={() => void act(job.id, "resume")}>
                      <Play size={10} /> {t("jobs.resume")}
                    </Button>
                  )}
                  <Button size="xs" variant="outline" disabled={busyId === job.id} onClick={() => void act(job.id, "cancel")}>
                    <Square size={10} /> {t("jobs.stop")}
                  </Button>
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
