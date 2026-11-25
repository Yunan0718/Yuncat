import { useEffect, useRef, useState } from "react";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter } from "./ui/dialog";
import { Button } from "./ui/button";
import { Input } from "./ui/input";

/**
 * P1 (2026-07-04): 可复用输入对话框组件——替代原生 window.prompt()。
 *
 * 原生 prompt() 的问题：
 * 1. 阻塞主线程，无法样式化
 * 2. 不支持主题（dark mode 下刺眼）
 * 3. 无法 FocusTrap / Esc 关闭（浏览器行为不一致）
 * 4. 不支持输入校验、默认值聚焦后选中、Enter 提交等 UX
 * 5. AI 写前端的典型反模式
 *
 * 对标 VS Code / Linear / Notion 的输入对话框样式。
 */
interface PromptDialogProps {
  open: boolean;
  title: string;
  description?: string;
  placeholder?: string;
  defaultValue?: string;
  confirmText?: string;
  cancelText?: string;
  /** 输入校验——返回 error string 表示校验失败，返回空串表示通过 */
  validate?: (v: string) => string;
  onConfirm: (value: string) => void;
  onCancel: () => void;
}

export function PromptDialog({
  open,
  title,
  description,
  placeholder,
  defaultValue = "",
  confirmText,
  cancelText,
  validate,
  onConfirm,
  onCancel,
}: PromptDialogProps) {
  const [value, setValue] = useState(defaultValue);
  const [error, setError] = useState("");
  const inputRef = useRef<HTMLInputElement | null>(null);

  // 打开时重置为 defaultValue 并聚焦选中
  useEffect(() => {
    if (open) {
      setValue(defaultValue);
      setError("");
      // 延迟一帧聚焦，确保 Dialog 已渲染
      requestAnimationFrame(() => {
        inputRef.current?.focus();
        inputRef.current?.select();
      });
    }
  }, [open, defaultValue]);

  const handleConfirm = () => {
    const trimmed = value.trim();
    if (validate) {
      const err = validate(trimmed);
      if (err) {
        setError(err);
        return;
      }
    }
    onConfirm(trimmed);
  };

  return (
    <Dialog open={open} onOpenChange={(v) => (!v ? onCancel() : undefined)}>
      <DialogContent showCloseButton={false}>
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          {description ? <DialogDescription>{description}</DialogDescription> : null}
        </DialogHeader>
        <Input
          ref={inputRef}
          value={value}
          onChange={(e) => {
            setValue(e.target.value);
            if (error) setError("");
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              handleConfirm();
            }
          }}
          placeholder={placeholder}
          aria-invalid={!!error}
        />
        {error ? <p className="text-xs text-destructive">{error}</p> : null}
        <DialogFooter>
          <Button variant="outline" size="sm" onClick={onCancel}>
            {cancelText}
          </Button>
          <Button size="sm" onClick={handleConfirm} disabled={!value.trim()}>
            {confirmText}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
