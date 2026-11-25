import { Dialog, DialogContent, DialogHeader, DialogTitle } from "../ui/dialog";
import { InlineDiff } from "./InlineDiff";
import { useTranslation } from "react-i18next";

interface FileCompareDialogProps {
  open: boolean;
  onClose: () => void;
  left: string;
  right: string;
  leftContent: string;
  rightContent: string;
}

export function FileCompareDialog({ open, onClose, left, right, leftContent, rightContent }: FileCompareDialogProps) {
  const { t } = useTranslation();
  return (
    <Dialog open={open} onOpenChange={(v) => !v && onClose()}>
      <DialogContent className="max-w-4xl w-[90vw] h-[80vh] flex flex-col p-0">
        <DialogHeader className="px-4 py-3 border-b shrink-0">
          <DialogTitle className="text-xs font-semibold">{t("fileCompareDialog.title")}</DialogTitle>
          <p className="text-[10px] text-muted-foreground truncate">
            {left} ↔ {right}
          </p>
        </DialogHeader>
        <div className="flex-1 overflow-hidden p-2">
          <InlineDiff oldContent={leftContent} newContent={rightContent} filePath={right} />
        </div>
      </DialogContent>
    </Dialog>
  );
}
