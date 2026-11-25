import { useEffect, useRef } from "react";
import { FocusScope } from "@radix-ui/react-focus-scope";

interface FocusTrapProps {
  active: boolean;
  children: React.ReactNode;
  initialFocusRef?: React.RefObject<HTMLElement | null>;
  restoreFocus?: boolean;
}

/**
 * Phase 49-B: 焦点陷阱包装器
 *
 * 基于 @radix-ui/react-focus-scope 实现：
 * - 打开时可选自动聚焦到指定元素
 * - 关闭后将焦点恢复到触发前的元素
 * - Tab/Shift+Tab 在作用域内循环
 * - **锁定背景滚动**（2026-09-15 补充：自建模态此前漏了这项，弹窗打开时页面仍可滚动。
 *   放在这里而不是各组件里，是因为这里已是所有自建模态的公共外壳——一处修好全都受益，
 *   与 Base UI 的 Dialog/Sheet 行为对齐。）
 */
export function FocusTrap({ active, children, initialFocusRef, restoreFocus = true }: FocusTrapProps) {
  const lastFocusedRef = useRef<Element | null>(null);
  const wasActiveRef = useRef(false);

  useEffect(() => {
    if (active && restoreFocus) {
      lastFocusedRef.current = document.activeElement;
      wasActiveRef.current = true;
    }
  }, [active, restoreFocus]);

  // 锁定背景滚动；卸载/失活时恢复**原值**（而非硬写 ""），
  // 避免与其它同样操作 body 样式的代码互相覆盖。
  useEffect(() => {
    if (!active) return;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = previousOverflow;
    };
  }, [active]);

  useEffect(() => {
    return () => {
      if (restoreFocus && wasActiveRef.current && lastFocusedRef.current instanceof HTMLElement) {
        lastFocusedRef.current.focus();
      }
      wasActiveRef.current = false;
    };
  }, [restoreFocus]);

  if (!active) return <>{children}</>;

  return (
    <FocusScope
      trapped
      onMountAutoFocus={(e) => {
        if (initialFocusRef?.current) {
          e.preventDefault();
          initialFocusRef.current.focus();
        }
      }}
    >
      {children}
    </FocusScope>
  );
}
