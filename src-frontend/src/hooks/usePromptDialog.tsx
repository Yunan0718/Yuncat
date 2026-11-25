import { useCallback, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { PromptDialog } from "../components/PromptDialog";

/**
 * P1 (2026-07-04): Promise-based prompt dialog hook——替代原生 window.prompt()。
 *
 * 用法：
 * ```tsx
 * const { prompt, promptDialog } = usePromptDialog();
 *
 * const handleRename = async () => {
 *   const newName = await prompt({
 *     title: t("rename"),
 *     defaultValue: oldName,
 *     placeholder: t("enterNewName"),
 *   });
 *   if (!newName) return;  // 用户取消
 *   // 执行重命名
 * };
 *
 * return (
 *   <>
 *     <button onClick={handleRename}>Rename</button>
 *     {promptDialog}
 *   </>
 * );
 * ```
 *
 * 对标 React 主流 prompt 模式（promise-based + controlled dialog）。
 * 返回值：string 表示用户输入（已 trim），null 表示取消。
 */
interface PromptOptions {
  title: string;
  description?: string;
  placeholder?: string;
  defaultValue?: string;
  confirmText?: string;
  cancelText?: string;
  validate?: (v: string) => string;
}

interface PromptState extends PromptOptions {
  open: boolean;
  resolve?: (v: string | null) => void;
}

export function usePromptDialog(): {
  prompt: (opts: PromptOptions) => Promise<string | null>;
  promptDialog: ReactNode;
} {
  const { t } = useTranslation();
  const [state, setState] = useState<PromptState>({ open: false, title: "" });

  const prompt = useCallback((opts: PromptOptions) => {
    return new Promise<string | null>((resolve) => {
      setState({ ...opts, open: true, resolve });
    });
  }, []);

  // 解构出 resolve 再进依赖数组：exhaustive-deps 不认 `state.resolve` 这种
  // 「依赖的成员」写法，只认独立绑定。写成 [state.resolve] 虽然此刻行为等价
  // （resolve 总是和 state 一起变），但规则校验不到，后续改动容易漏。
  const { resolve } = state;

  const handleConfirm = useCallback(
    (value: string) => {
      resolve?.(value);
      setState((s) => ({ ...s, open: false, resolve: undefined }));
    },
    [resolve],
  );

  const handleCancel = useCallback(() => {
    resolve?.(null);
    setState((s) => ({ ...s, open: false, resolve: undefined }));
  }, [resolve]);

  const promptDialog = (
    <PromptDialog
      open={state.open}
      title={state.title}
      description={state.description}
      placeholder={state.placeholder}
      defaultValue={state.defaultValue}
      confirmText={state.confirmText ?? t("common.confirm")}
      cancelText={state.cancelText ?? t("common.cancel")}
      validate={state.validate}
      onConfirm={handleConfirm}
      onCancel={handleCancel}
    />
  );

  return { prompt, promptDialog };
}
