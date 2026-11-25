import { useTranslation } from "react-i18next";
import { EmptyState } from "./common/EmptyState";

export interface BlameLine {
  hash: string;
  content: string;
  author?: string;
  "author-time"?: string;
  summary?: string;
}

interface GitBlameViewProps {
  lines: BlameLine[];
}

/** Phase 103: 行级 Blame 展示 */
export function GitBlameView({ lines }: GitBlameViewProps) {
  const { t } = useTranslation();

  if (!lines.length) {
    return <EmptyState compact title={t("gitPanel.blame.empty")} />;
  }

  return (
    <div className="border rounded overflow-hidden text-[11px]">
      <div className="grid grid-cols-[40px_70px_1fr_80px] gap-1 px-2 py-1 bg-secondary border-b font-semibold text-[10px]">
        <span>{t("gitPanel.blame.line")}</span>
        <span>{t("gitPanel.blame.hash")}</span>
        <span>{t("gitPanel.blame.author")}</span>
        <span>{t("gitPanel.blame.time")}</span>
      </div>
      <div className="max-h-[200px] overflow-y-auto">
        {lines.map((line, index) => (
          <div
            key={index}
            className="grid grid-cols-[40px_70px_1fr_80px] gap-1 px-2 py-0.5 border-b border-border/50 hover:bg-secondary/50"
          >
            <span className="text-muted-foreground">{index + 1}</span>
            <code className="text-primary font-mono text-[10px]">{line.hash.slice(0, 7)}</code>
            <span className="truncate" title={line.summary}>
              {line.author || "—"}
            </span>
            <span className="text-muted-foreground text-[10px] truncate">{line["author-time"] || "—"}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
