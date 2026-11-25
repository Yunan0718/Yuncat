import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter } from "./ui/dialog";
import { Button } from "./ui/button";

/**
 * P1 (2026-07-04): 可复用确认对话框组件——替代原生 window.confirm()。
 *
 * 原生 confirm() 的问题：
 * 1. 阻塞主线程，无法样式化
 * 2. 不支持主题（dark mode 下刺眼）
 * 3. 无法 FocusTrap / Esc 关闭（浏览器行为不一致）
 * 4. AI 写前端的典型反模式
 *
 * 对标 VS Code / Linear / Notion 的确认对话框样式。
 */
interface ConfirmDialogProps {
  open: boolean;
  title: string;
  description?: string;
  confirmText?: string;
  cancelText?: string;
  variant?: "default" | "destructive";
  onConfirm: () => void;
  onCancel: () => void;
}

export function ConfirmDialog({
  open,
  title,
  description,
  confirmText,
  cancelText,
  variant = "default",
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  return (
    <Dialog open={open} onOpenChange={(v) => (!v ? onCancel() : undefined)}>
      <DialogContent showCloseButton={false}>
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          {description ? <DialogDescription>{description}</DialogDescription> : null}
        </DialogHeader>
        <DialogFooter>
          <Button variant="outline" size="sm" onClick={onCancel}>
            {cancelText}
          </Button>
          <Button variant={variant === "destructive" ? "destructive" : "default"} size="sm" onClick={onConfirm}>
            {confirmText}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
