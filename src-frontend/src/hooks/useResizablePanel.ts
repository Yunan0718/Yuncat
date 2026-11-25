import { useCallback, useEffect, useRef, useState } from "react";

/**
 * P0-6.5: useResizablePanel——可拖拽调整面板尺寸的 hook
 *
 * 对标 VS Code 所有主要面板均可拖拽（活动栏/侧边栏/编辑器组/面板/辅助栏）。
 * 消除 ChatView/TerminalPanel 中重复的拖拽实现，并持久化到 localStorage。
 *
 * 用法：
 *   const { size, dragHandleProps } = useResizablePanel({
 *     storageKey: 'yuncat.leftPanel.width',
 *     initialSize: 220,
 *     minSize: 170,   // VS Code Sidebar 最小 170px
 *     maxSize: 512,   // VS Code Sidebar 最大 512px
 *     side: 'left',   // 面板位于父容器的哪一侧
 *   });
 *
 *   <div style={{ width: size }}>
 *     <PanelContent />
 *     <div {...dragHandleProps} className="w-1 cursor-col-resize bg-border hover:bg-primary/30" />
 *   </div>
 *
 * side 语义：
 *   'left'   - 面板在左侧，handle 在右边，向右拖减小宽度
 *   'right'  - 面板在右侧，handle 在左边，向右拖增大宽度
 *   'top'    - 面板在上侧，handle 在下边，向下拖减小高度
 *   'bottom' - 面板在下侧，handle 在上边，向下拖增大高度
 */
export interface UseResizablePanelOptions {
  storageKey: string;
  initialSize: number;
  minSize?: number;
  maxSize?: number;
  side?: "left" | "right" | "top" | "bottom";
}

export interface DragHandleProps {
  onMouseDown: (e: React.MouseEvent) => void;
}

export function useResizablePanel({
  storageKey,
  initialSize,
  minSize = 170,
  maxSize = 512,
  side = "left",
}: UseResizablePanelOptions): {
  size: number;
  setSize: (size: number) => void;
  dragHandleProps: DragHandleProps;
} {
  const [size, setSize] = useState(() => {
    try {
      const saved = localStorage.getItem(storageKey);
      const parsed = saved ? Number(saved) : NaN;
      // 校验保存的值在合法范围内
      return Number.isFinite(parsed) && parsed >= minSize && parsed <= maxSize
        ? parsed
        : initialSize;
    } catch {
      return initialSize;
    }
  });

  const dragRef = useRef<{ startPos: number; startSize: number } | null>(null);
  const sizeRef = useRef(size);
  sizeRef.current = size;

  const onMouseDown = useCallback(
    (e: React.MouseEvent) => {
      e.preventDefault();
      const isHorizontal = side === "left" || side === "right";
      dragRef.current = {
        startPos: isHorizontal ? e.clientX : e.clientY,
        startSize: sizeRef.current,
      };

      const onMouseMove = (ev: MouseEvent) => {
        if (!dragRef.current) return;
        const currentPos = isHorizontal ? ev.clientX : ev.clientY;
        const delta = currentPos - dragRef.current.startPos;
        // left/top: 向右/下拖(delta>0)减小 / right/bottom: 向右/下拖增大
        const sign = side === "left" || side === "top" ? -1 : 1;
        const newSize = Math.max(
          minSize,
          Math.min(maxSize, dragRef.current.startSize + delta * sign),
        );
        setSize(newSize);
      };

      const onMouseUp = () => {
        if (dragRef.current) {
          try {
            localStorage.setItem(storageKey, String(sizeRef.current));
          } catch {
            // localStorage 不可用时忽略
          }
        }
        dragRef.current = null;
        document.removeEventListener("mousemove", onMouseMove);
        document.removeEventListener("mouseup", onMouseUp);
        document.body.style.cursor = "";
        document.body.style.userSelect = "";
      };

      document.addEventListener("mousemove", onMouseMove);
      document.addEventListener("mouseup", onMouseUp);
      document.body.style.cursor = isHorizontal ? "col-resize" : "row-resize";
      document.body.style.userSelect = "none";
    },
    [side, minSize, maxSize, storageKey],
  );

  // 持久化到 localStorage（size 变化时）
  useEffect(() => {
    try {
      localStorage.setItem(storageKey, String(size));
    } catch {
      // localStorage 不可用时忽略
    }
  }, [size, storageKey]);

  return { size, setSize, dragHandleProps: { onMouseDown } };
}
