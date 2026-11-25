import { useTranslation } from "react-i18next";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "./ui/dialog";
import { Button } from "./ui/button";
import { Input } from "./ui/input";
import { AlertTriangle } from "lucide-react";

interface GitOperationDialogProps {
  open: boolean;
  title: string;
  message: string;
  confirmLabel?: string;
  cancelLabel?: string;
  input?: {
    value: string;
    onChange: (value: string) => void;
    placeholder: string;
  };
  onConfirm: () => void;
  onCancel: () => void;
}

/** Phase 103: Git 危险操作二次确认弹窗 */
export function GitOperationDialog({
  open,
  title,
  message,
  confirmLabel,
  cancelLabel,
  input,
  onConfirm,
  onCancel,
}: GitOperationDialogProps) {
  const { t } = useTranslation();

  return (
    <Dialog open={open} onOpenChange={(v) => !v && onCancel()}>
      <DialogContent className="max-w-sm">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2 text-sm">
            <AlertTriangle size={14} className="text-warning" />
            {title}
          </DialogTitle>
        </DialogHeader>
        <p className="text-xs text-muted-foreground py-2">{message}</p>
        {input && (
          <Input
            value={input.value}
            onChange={(e) => input.onChange(e.target.value)}
            placeholder={input.placeholder}
            className="h-8 text-[11px]"
            autoFocus
          />
        )}
        <div className="flex justify-end gap-2">
          <Button variant="outline" size="sm" onClick={onCancel} className="text-[10px]">
            {cancelLabel || t("common.cancel")}
          </Button>
          <Button size="sm" onClick={onConfirm} className="text-[10px]">
            {confirmLabel || t("common.confirm")}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
