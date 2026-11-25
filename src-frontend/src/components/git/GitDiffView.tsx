import { useTranslation } from "react-i18next";
import { UnifiedDiffView } from "./UnifiedDiffView";
import { GitBlameView, type BlameLine } from "../GitBlameView";

export interface GitDiffViewProps {
  diffText: string;
  diffFile: string;
  blameOpen: boolean;
  blameLines: BlameLine[];
  onCloseBlame: () => void;
}

export function GitDiffView({ diffText, diffFile, blameOpen, blameLines, onCloseBlame }: GitDiffViewProps) {
  const { t } = useTranslation();
  return (
    <div className="p-1 space-y-2">
      {diffText ? (
        <UnifiedDiffView diffText={diffText} title={diffFile} />
      ) : (
        <p className="text-muted-foreground text-center py-4">{t("gitPanel.diff.hint")}</p>
      )}
      {blameOpen && (
        <div>
          <div className="flex items-center justify-between mb-1">
            <span className="text-[10px] font-semibold">{t("gitPanel.blame.title")}: {diffFile}</span>
            <button onClick={onCloseBlame} className="text-[10px] text-muted-foreground hover:text-foreground">
              {t("common.close")}
            </button>
          </div>
          <GitBlameView lines={blameLines} />
        </div>
      )}
    </div>
  );
}
