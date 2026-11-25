import { useTranslation } from "react-i18next";
import { Plus, Minus, Trash2, GitMerge } from "lucide-react";
import { type GitFile, STATUS_KEY_MAP } from "./gitTypes";

const statusColor = (s: string) => {
  if (s === "U") return "text-orange-500";
  if (s === "M") return "text-warning";
  if (s === "A" || s === "?") return "text-success";
  if (s === "D") return "text-destructive";
  return "text-muted-foreground";
};

export interface GitFileListProps {
  files: GitFile[];
  onOpenConflict: (path: string) => void;
  onLoadDiff: (path: string) => void;
  onStageToggle: (path: string, stage: boolean) => void;
  onDiscard: (path: string) => void;
}

export function GitFileList({ files, onOpenConflict, onLoadDiff, onStageToggle, onDiscard }: GitFileListProps) {
  const { t } = useTranslation();
  return (
    <div>
      {files.length === 0 ? (
        <p className="text-muted-foreground text-center py-4">{t("gitPanel.cleanWorkspace")}</p>
      ) : (
        files.map((f) => (
          <div
            key={f.path}
            className="w-full text-left px-2 py-1 flex items-center gap-2 hover:bg-secondary border-b border-border transition-colors"
          >
            <button
              onClick={() => (f.status === "U" ? onOpenConflict(f.path) : onLoadDiff(f.path))}
              className="flex items-center gap-2 flex-1 min-w-0 text-left"
            >
              <span className={`font-mono text-[10px] shrink-0 w-4 ${statusColor(f.status)}`}>
                {f.status === "U" ? "U" : f.status}
              </span>
              <span className="truncate text-xs">{f.path}</span>
              <span className="text-[10px] text-muted-foreground shrink-0">
                {t("gitPanel.status." + (STATUS_KEY_MAP[f.status] || f.status), { defaultValue: f.status })}
              </span>
            </button>
            <div className="flex items-center gap-0.5 shrink-0">
              {f.status === "U" && (
                <button
                  onClick={() => onOpenConflict(f.path)}
                  title={t("gitPanel.resolveConflict")}
                  aria-label={t("gitPanel.resolveConflict")}
                  className="p-0.5 hover:bg-orange-500/10 rounded text-orange-500"
                >
                  <GitMerge size={11} />
                </button>
              )}
              {f.status !== "?" && f.status !== "U" && (
                <button
                  onClick={() => onStageToggle(f.path, !f.staged)}
                  title={f.staged ? t("gitPanel.unstage") : t("gitPanel.stage.label")}
                  aria-label={f.staged ? t("gitPanel.unstage") : t("gitPanel.stage.label")}
                  className="p-0.5 hover:bg-secondary rounded text-muted-foreground"
                >
                  {f.staged ? <Minus size={11} /> : <Plus size={11} />}
                </button>
              )}
              {f.status !== "U" && (
                <button
                  onClick={() => onDiscard(f.path)}
                  title={t("gitPanel.discard.title")}
                  aria-label={t("gitPanel.discard.title")}
                  className="p-0.5 hover:bg-destructive/10 rounded text-destructive"
                >
                  <Trash2 size={11} />
                </button>
              )}
            </div>
          </div>
        ))
      )}
    </div>
  );
}
