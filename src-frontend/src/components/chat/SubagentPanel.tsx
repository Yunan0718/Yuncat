import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Bot, ChevronDown, ChevronUp, Send, Square } from "lucide-react";
import { useApi } from "../../hooks/useApi";
import { emitGlobalToast } from "../common/toast-context";
import { Button } from "../ui/button";
import { Input } from "../ui/input";
import type { TaskState } from "../../store/taskStore";
import type { SubTaskNode } from "../../types/task";

interface SubagentPanelProps {
  taskId: string;
  status: TaskState["status"];
  initial?: SubTaskNode[];
}

const STATUS_CLASS: Record<string, string> = {
  running: "bg-primary/10 text-primary",
  completed: "bg-success/10 text-success",
  failed: "bg-destructive/10 text-destructive",
  cancelled: "bg-muted text-muted-foreground",
  interrupted: "bg-warning/10 text-warning",
  paused: "bg-warning/10 text-warning",
  pending: "bg-muted text-muted-foreground",
};

export function SubagentPanel({ taskId, status, initial = [] }: SubagentPanelProps) {
  const { t } = useTranslation();
  const { api } = useApi();
  const [items, setItems] = useState<SubTaskNode[]>(initial);
  const [expanded, setExpanded] = useState(status === "running" || initial.length > 0);
  const [steerTarget, setSteerTarget] = useState<string | null>(null);
  const [prompt, setPrompt] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const result = await api<{ items: SubTaskNode[] }>(`/api/v1/tasks/${taskId}/subagents`);
      setItems(result.items || []);
    } catch {
      if (status === "running") emitGlobalToast(t("subagents.loadFailed"), "error");
    }
  }, [api, status, t, taskId]);

  useEffect(() => {
    if (status !== "running" && initial.length === 0) return;
    void load();
    if (status !== "running") return;
    const timer = setInterval(() => void load(), 3000);
    return () => clearInterval(timer);
  }, [initial.length, load, status]);

  const stop = async (childId: string) => {
    setBusy(true);
    try {
      await api(`/api/v1/tasks/${taskId}/subagents/${childId}/stop`, { method: "POST" });
      await load();
    } catch (error) {
      emitGlobalToast(`${t("subagents.stopFailed")}: ${String(error)}`, "error");
    } finally {
      setBusy(false);
    }
  };

  const steer = async () => {
    if (!steerTarget || !prompt.trim()) return;
    setBusy(true);
    try {
      await api(`/api/v1/tasks/${taskId}/subagents/${steerTarget}/steer`, {
        method: "POST", body: JSON.stringify({ prompt: prompt.trim() }),
      });
      emitGlobalToast(t("subagents.steerQueued"), "info");
      setPrompt("");
      setSteerTarget(null);
    } catch (error) {
      emitGlobalToast(`${t("subagents.steerFailed")}: ${String(error)}`, "error");
    } finally {
      setBusy(false);
    }
  };

  if (items.length === 0) return null;

  return (
    <div className="rounded-lg border bg-card/60 overflow-hidden">
      <button
        type="button"
        className="w-full flex items-center gap-2 px-3 py-2 text-xs hover:bg-secondary/50"
        onClick={() => setExpanded((value) => !value)}
      >
        <Bot size={13} />
        <span className="font-medium">{t("subagents.title", { count: items.length })}</span>
        <span className="ml-auto">{expanded ? <ChevronUp size={13} /> : <ChevronDown size={13} />}</span>
      </button>
      {expanded && (
        <div className="border-t divide-y">
          {items.map((item) => (
            <div key={item.id} className="px-3 py-2.5 space-y-2">
              <div className="flex items-start gap-2">
                <span className={`shrink-0 rounded px-1.5 py-0.5 text-[10px] ${STATUS_CLASS[item.status] || STATUS_CLASS.pending}`}>
                  {t(`subagents.status.${item.status}`)}
                </span>
                <div className="min-w-0 flex-1">
                  <div className="text-xs truncate" title={item.input}>{item.input}</div>
                  <div className="text-[10px] text-muted-foreground mt-0.5">{item.agent_id} · {item.id.slice(0, 8)}</div>
                  {item.output_preview && <div className="text-2xs text-muted-foreground mt-1 line-clamp-3 whitespace-pre-wrap">{item.output_preview}</div>}
                </div>
              </div>
              {item.status === "running" && (
                <div className="flex items-center gap-1.5 pl-1">
                  <Button size="xs" variant="outline" disabled={busy} onClick={() => void stop(item.id)}>
                    <Square size={11} /> {t("subagents.stop")}
                  </Button>
                  <Button size="xs" variant="outline" disabled={busy} onClick={() => setSteerTarget(item.id)}>
                    <Bot size={11} /> {t("subagents.steer")}
                  </Button>
                </div>
              )}
              {steerTarget === item.id && (
                <div className="flex gap-1.5 pl-1">
                  <Input
                    value={prompt}
                    onChange={(event) => setPrompt(event.target.value)}
                    onKeyDown={(event) => { if (event.key === "Enter") void steer(); }}
                    placeholder={t("subagents.steerPlaceholder")}
                    className="h-8 text-xs"
                    autoFocus
                  />
                  <Button size="icon" className="h-8 w-8" aria-label={t("subagents.steerSend")} disabled={busy || !prompt.trim()} onClick={() => void steer()}>
                    <Send size={12} />
                  </Button>
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
