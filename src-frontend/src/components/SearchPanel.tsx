import { useState, useEffect, useRef } from "react";
import { useTranslation } from "react-i18next";
import { useApi } from "../hooks/useApi";
import { Search, X, FileText, ListTree } from "lucide-react";
import { JobsPanel } from "./JobsPanel";

interface Props {
  visible: boolean;
  onClose: () => void;
  onOpenFile: (path: string, name: string) => void;
  sessionId?: string | null;
  onJumpToTask?: (taskId: string) => void;
}

interface OutlineTurn {
  index: number;
  task_id: string;
  title: string;
  preview: string;
  message_count: number;
  tokens: number;
}

export function SearchPanel({ visible, onClose, onOpenFile, sessionId, onJumpToTask }: Props) {
  const { t } = useTranslation();
  const { api } = useApi();
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<Array<{ path: string; line: number; text: string }>>([]);
  const [searching, setSearching] = useState(false);
  const [error, setError] = useState("");
  const [tab, setTab] = useState<"files" | "outline" | "jobs">("files");
  const [outline, setOutline] = useState<OutlineTurn[]>([]);
  const [outlineLoading, setOutlineLoading] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (visible) inputRef.current?.focus();
  }, [visible]);

  const doSearch = async () => {
    if (!query.trim()) return;
    setSearching(true);
    setError("");
    try {
      const r = await api<{ results: Array<{ path: string; line: number; text: string }> }>(
        `/api/v1/workspace/files/search?q=${encodeURIComponent(query)}`,
      );
      setResults(r.results || []);
    } catch (e: unknown) {
      setError(t("phase49.search.failed", { error: e instanceof Error ? e.message : String(e) }));
    }
    setSearching(false);
  };

  const loadOutline = async () => {
    if (!sessionId) return;
    setOutlineLoading(true);
    setError("");
    try {
      const result = await api<{ turns: OutlineTurn[] }>(`/api/v1/sessions/${sessionId}/outline`);
      setOutline(result.turns || []);
    } catch (e: unknown) {
      setError(t("phase49.search.outlineFailed", { error: e instanceof Error ? e.message : String(e) }));
    } finally {
      setOutlineLoading(false);
    }
  };

  const selectTab = (next: "files" | "outline" | "jobs") => {
    setTab(next);
    if (next === "outline") void loadOutline();
  };

  if (!visible) return null;

  return (
    <div className="absolute top-full left-0 right-0 z-dropdown bg-card border-b shadow-lg pt-2 pb-3 px-4">
      <div className="flex items-center gap-1 mb-2">
        <button type="button" onClick={() => selectTab("files")} className={`px-2 py-0.5 rounded text-[11px] ${tab === "files" ? "bg-secondary text-foreground" : "text-muted-foreground"}`}>
          {t("phase49.search.filesTab")}
        </button>
        <button type="button" onClick={() => selectTab("outline")} disabled={!sessionId} className={`px-2 py-0.5 rounded text-[11px] disabled:opacity-40 ${tab === "outline" ? "bg-secondary text-foreground" : "text-muted-foreground"}`}>
          {t("phase49.search.outlineTab")}
        </button>
        <button type="button" onClick={() => selectTab("jobs")} className={`px-2 py-0.5 rounded text-[11px] ${tab === "jobs" ? "bg-secondary text-foreground" : "text-muted-foreground"}`}>
          {t("jobs.tab")}
        </button>
      </div>
      {tab === "files" ? (
        <>
      <div className="flex items-center gap-2 mb-2">
        <Search size={14} className="text-muted-foreground shrink-0" />
        <input
          ref={inputRef}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") doSearch();
            else if (e.key === "Escape") onClose();
          }}
          placeholder={t("phase49.search.placeholder")}
          className="flex-1 px-2 py-1 text-xs border rounded bg-background outline-none focus:border-primary"
        />
        <button onClick={doSearch} className="text-xs px-2 py-1 bg-primary text-white rounded font-medium">
          {searching ? "…" : t("phase49.search.button")}
        </button>
        <button
          onClick={onClose}
          aria-label={t("common.close")}
          className="text-muted-foreground hover:text-foreground"
        >
          <X size={14} />
        </button>
      </div>
      {error && <p className="text-[10px] text-destructive mb-1">{error}</p>}
      {results.length > 0 && (
        <div className="max-h-64 overflow-y-auto space-y-0.5">
          {results.slice(0, 15).map((r) => (
            <button
              key={`${r.path}:${r.line}`}
              className="w-full text-left px-2 py-1 hover:bg-secondary rounded flex items-center gap-2 text-[11px]"
              onClick={() => onOpenFile(r.path, r.path.split(/[/\\]/).pop() || r.path)}
            >
              <FileText size={12} className="text-muted-foreground shrink-0" />
              <span className="font-mono text-muted-foreground truncate max-w-[180px]">{r.path}</span>
              <span className="text-muted-foreground/50">:{r.line}</span>
              <span className="truncate text-muted-foreground/70 flex-1">{r.text.slice(0, 80)}</span>
            </button>
          ))}
        </div>
      )}
      {!searching && results.length === 0 && query.length > 0 && (
        <p className="text-[10px] text-muted-foreground">{t("phase49.search.noMatches", { query })}</p>
      )}
        </>
      ) : tab === "outline" ? (
        <div className="max-h-64 overflow-y-auto space-y-1">
          {outlineLoading && <p className="text-[10px] text-muted-foreground">{t("common.loading")}</p>}
          {!outlineLoading && outline.length === 0 && <p className="text-[10px] text-muted-foreground">{t("phase49.search.outlineEmpty")}</p>}
          {outline.map((turn) => (
            <button
              key={`${turn.index}-${turn.title}`}
              type="button"
              disabled={!turn.task_id}
              onClick={() => { if (turn.task_id) onJumpToTask?.(turn.task_id); }}
              className="w-full text-left px-2 py-1.5 rounded hover:bg-secondary disabled:hover:bg-transparent flex gap-2"
            >
              <span className="text-[10px] text-muted-foreground w-5 shrink-0">{turn.index}</span>
              <ListTree size={12} className="text-muted-foreground shrink-0 mt-0.5" />
              <span className="min-w-0 flex-1">
                <span className="block text-[11px] truncate">{turn.title}</span>
                {turn.preview && <span className="block text-[10px] text-muted-foreground truncate">{turn.preview}</span>}
                <span className="block text-[9px] text-muted-foreground/70">{t("phase49.search.turnMeta", { count: turn.message_count, tokens: turn.tokens })}</span>
              </span>
            </button>
          ))}
        </div>
      ) : <JobsPanel onOpenTask={onJumpToTask} sessionId={sessionId} />}
    </div>
  );
}
