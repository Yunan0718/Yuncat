import { useEffect, useRef } from "react";
import { createPortal } from "react-dom";
import i18next from "i18next";
import { FocusTrap } from "./FocusTrap";

interface DetailDrawerProps {
  open: boolean;
  title: string;
  onClose: () => void;
  children: React.ReactNode;
  /** Phase 82: 抽屉宽度（Tailwind class），默认 w-96（384px） */
  width?: string;
  /** Task 6.6: header 右侧额外操作（如刷新按钮） */
  headerAction?: React.ReactNode;
}

/**
 * Phase 7: 通用右侧抽屉组件
 *
 * 用于任务详情、工作流时间线等侧边栏面板。
 * Phase 82: 加 width prop 支持 Supervisor 驾驶舱 480px 宽度。
 * P1 (2026-07-04): 加 Esc 关闭 + FocusTrap——WAI-ARIA 模态对话框规范要求。
 */
export function DetailDrawer({ open, title, onClose, children, width = "w-96", headerAction }: DetailDrawerProps) {
  const drawerRef = useRef<HTMLDivElement>(null);

  // P1: Esc 键关闭
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onClose();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  return createPortal(
    <div className="fixed inset-0 z-drawer" role="dialog" aria-modal="true" aria-label={title}>
      {/* 遮罩 */}
      <div className="absolute inset-0 bg-black/30" onClick={onClose} />
      {/* 抽屉 */}
      <FocusTrap active={open} initialFocusRef={drawerRef}>
        <div
          ref={drawerRef}
          tabIndex={-1}
          className={`absolute right-0 top-0 h-full ${width} bg-background border-l shadow-sm flex flex-col outline-none animate-drawer-in`}
        >
          <div className="flex items-center justify-between px-4 py-3 border-b shrink-0">
            <h2 className="text-sm font-semibold">{title}</h2>
            <div className="flex items-center gap-2">
              {headerAction}
              <button
                onClick={onClose}
                className="text-muted-foreground hover:text-foreground text-lg leading-none"
                aria-label={i18next.t("common.close")}
              >
                &times;
              </button>
            </div>
          </div>
          <div className="flex-1 overflow-y-auto p-4">{children}</div>
        </div>
      </FocusTrap>
    </div>,
    document.getElementById("overlay-root")!,
  );
}
