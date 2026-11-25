import { useCallback, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { ConfirmDialog } from "../components/ConfirmDialog";

/**
 * P1 (2026-07-04): Promise-based confirm dialog hook——替代原生 window.confirm()。
 *
 * 用法：
 * ```tsx
 * const { confirm, confirmDialog } = useConfirmDialog();
 *
 * const handleDelete = async () => {
 *   const ok = await confirm({ title: t("delete"), description: t("confirmDelete"), variant: "destructive" });
 *   if (!ok) return;
 *   // 执行删除
 * };
 *
 * return (
 *   <>
 *     <button onClick={handleDelete}>Delete</button>
 *     {confirmDialog}
 *   </>
 * );
 * ```
 *
 * 对标 React 主流 confirm 模式（promise-based + controlled dialog）。
 * 原生 confirm() 阻塞主线程、无法样式化、不支持主题——是 AI 写前端的典型反模式。
 */
interface ConfirmOptions {
  title: string;
  description?: string;
  confirmText?: string;
  cancelText?: string;
  variant?: "default" | "destructive";
}

interface ConfirmState extends ConfirmOptions {
  open: boolean;
  resolve?: (v: boolean) => void;
}

export function useConfirmDialog(): {
  confirm: (opts: ConfirmOptions) => Promise<boolean>;
  confirmDialog: ReactNode;
} {
  const { t } = useTranslation();
  const [state, setState] = useState<ConfirmState>({ open: false, title: "" });

  const confirm = useCallback((opts: ConfirmOptions) => {
    return new Promise<boolean>((resolve) => {
      setState({ ...opts, open: true, resolve });
    });
  }, []);

  // 解构出 resolve 再进依赖数组：exhaustive-deps 不认 `state.resolve` 这种
  // 「依赖的成员」写法，只认独立绑定。写成 [state.resolve] 虽然此刻行为等价
  // （resolve 总是和 state 一起变），但规则校验不到，后续改动容易漏。
  const { resolve } = state;

  const handleConfirm = useCallback(() => {
    resolve?.(true);
    setState((s) => ({ ...s, open: false, resolve: undefined }));
  }, [resolve]);

  const handleCancel = useCallback(() => {
    resolve?.(false);
    setState((s) => ({ ...s, open: false, resolve: undefined }));
  }, [resolve]);

  const confirmDialog = (
    <ConfirmDialog
      open={state.open}
      title={state.title}
      description={state.description}
      confirmText={state.confirmText ?? t("common.confirm")}
      cancelText={state.cancelText ?? t("common.cancel")}
      variant={state.variant}
      onConfirm={handleConfirm}
      onCancel={handleCancel}
    />
  );

  return { confirm, confirmDialog };
}
