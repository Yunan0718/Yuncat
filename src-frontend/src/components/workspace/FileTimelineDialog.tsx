import { Dialog, DialogContent, DialogHeader, DialogTitle } from "../ui/dialog";
import { GitCommit, User } from "lucide-react";
import { useTranslation } from "react-i18next";

interface TimelineData {
  blame: Array<{ author: string; count: number }>;
  commits: Array<{ hash: string; message: string; time: string; author: string }>;
}

interface FileTimelineDialogProps {
  open: boolean;
  onClose: () => void;
  filePath: string | null;
  data: TimelineData;
}

export function FileTimelineDialog({ open, onClose, filePath, data }: FileTimelineDialogProps) {
  const { t } = useTranslation();
  return (
    <Dialog open={open} onOpenChange={(v) => !v && onClose()}>
      <DialogContent className="max-w-lg w-[90vw] max-h-[80vh] flex flex-col p-0">
        <DialogHeader className="px-4 py-3 border-b shrink-0">
          <DialogTitle className="text-xs font-semibold">{t("fileTimelineDialog.title")}</DialogTitle>
          <p className="text-[10px] text-muted-foreground truncate">{filePath}</p>
        </DialogHeader>
        <div className="flex-1 overflow-y-auto p-3 space-y-3">
          {data.commits.length === 0 ? (
            <p className="text-[11px] text-muted-foreground text-center py-6">{t("fileTimelineDialog.noGitHistory")}</p>
          ) : (
            <div className="space-y-2">
              {data.commits.map((c) => (
                <div key={c.hash} className="flex gap-2 text-[11px] border rounded-md p-2">
                  <GitCommit size={14} className="text-primary shrink-0 mt-0.5" />
                  <div className="flex-1 min-w-0">
                    <p className="font-medium truncate">{c.message}</p>
                    <div className="flex items-center gap-2 text-[10px] text-muted-foreground mt-1">
                      <span className="font-mono">{c.hash.slice(0, 7)}</span>
                      <span className="flex items-center gap-0.5">
                        <User size={10} /> {c.author}
                      </span>
                      <span>{c.time}</span>
                    </div>
                  </div>
                </div>
              ))}
            </div>
          )}
          {data.blame.length > 0 && (
            <div>
              <p className="text-[10px] font-semibold text-muted-foreground mb-1.5">{t("fileTimelineDialog.recentAuthors")}</p>
              <div className="flex flex-wrap gap-1.5">
                {data.blame.slice(0, 10).map((b) => (
                  <span key={`${b.author}-${b.count}`} className="text-[10px] bg-secondary px-1.5 py-0.5 rounded">
                    {b.author}
                  </span>
                ))}
              </div>
            </div>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}
