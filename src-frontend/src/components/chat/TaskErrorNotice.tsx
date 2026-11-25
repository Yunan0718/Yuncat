import { useTranslation } from "react-i18next";

/**
 * 一条回复失败时的提示块（2026-09-16）。
 *
 * 从 MessageItem 里抽出来：那是个已经超长（>800 行）的文件，静态守卫不允许它继续变大；
 * 而且"失败长什么样"本来就该是一处实现——现在只有它渲染，将来轨迹卡片也复用它。
 *
 * 显示三件事：出了什么事、原始错误、以及用户能照着做的下一步。
 */
const HINT_KEYS: Record<string, string> = {
  AUTH: "chat.errorHintAuth",
  AUTH_ERROR: "chat.errorHintAuth",
  CONFIG_MISSING: "chat.errorHintConfig",
  RATE_LIMIT: "chat.errorHintRateLimit",
  BUDGET: "chat.errorHintBudget",
  TIMEOUT: "chat.errorHintTimeout",
  NETWORK: "chat.errorHintNetwork",
};

export function TaskErrorNotice({ message, code }: { message: string; code?: string }) {
  const { t } = useTranslation();
  const hintKey = (code && HINT_KEYS[code]) || "chat.errorHintDefault";
  return (
    <div
      className="rounded-xl bg-destructive/10 px-3 py-2 space-y-1"
      data-testid="task-error"
    >
      <p className="text-xs font-semibold text-destructive">{t("chat.errorTitle")}</p>
      <p className="text-xs text-destructive/90 break-words">{message}</p>
      <p className="text-2xs text-muted-foreground">{t(hintKey)}</p>
    </div>
  );
}
