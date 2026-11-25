import { useTranslation } from "react-i18next";
import { SHORTCUT_DEFS, getShortcutKeys } from "../../lib/shortcuts";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "../ui/dialog";

interface ShortcutReferenceDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

/**
 * IMPROVEMENTS P2: 全局快捷键参考弹窗
 *
 * 列出 lib/shortcuts.ts 注册表中所有可配置快捷键及其当前组合，
 * 可通过 Shift+? 全局唤起，也可在 ShortcutEditor 内点击打开。
 */
export function ShortcutReferenceDialog({ open, onOpenChange }: ShortcutReferenceDialogProps) {
  const { t } = useTranslation();

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>{t("shortcutEditor.referenceTitle")}</DialogTitle>
          <DialogDescription>{t("shortcutEditor.referenceDescription")}</DialogDescription>
        </DialogHeader>
        <div className="mt-2 space-y-1">
          {SHORTCUT_DEFS.map((s) => (
            <div
              key={s.id}
              className="flex items-center justify-between gap-4 border-b py-1.5 last:border-0"
            >
              <span className="text-sm text-muted-foreground">{t(s.label)}</span>
              <kbd className="rounded border bg-muted px-2 py-0.5 font-mono text-xs">
                {getShortcutKeys(s.id)}
              </kbd>
            </div>
          ))}
        </div>
      </DialogContent>
    </Dialog>
  );
}
