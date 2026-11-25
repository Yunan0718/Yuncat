/** components/common/ErrorState.tsx — Phase 50-J: 统一错误状态组件
 *
 * 用于 API 请求失败、加载异常、操作出错等场景。
 * 显示错误图标、标题、详情、重试按钮。
 */
import { ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Button } from "../ui/button";

interface ErrorStateProps {
  icon?: ReactNode;
  title: string;
  message?: string;
  onRetry?: () => void;
  retryLabel?: string;
  compact?: boolean;
}

export function ErrorState({ icon, title, message, onRetry, retryLabel, compact }: ErrorStateProps) {
  const { t } = useTranslation();
  return (
    <div className={`flex flex-col items-center justify-center text-center ${compact ? "py-6 px-4" : "py-10 px-6"}`}>
      {icon && (
        <div
          className={`rounded-full bg-destructive/10 flex items-center justify-center mb-3 ${compact ? "w-10 h-10" : "w-14 h-14"}`}
        >
          <span className={`text-destructive ${compact ? "scale-90" : ""}`}>{icon}</span>
        </div>
      )}
      <h3 className={`font-semibold text-foreground ${compact ? "text-sm" : "text-base"}`}>{title}</h3>
      {message && (
        <p className={`text-muted-foreground mt-1 max-w-xs break-words ${compact ? "text-xs" : "text-sm"}`}>
          {message}
        </p>
      )}
      {onRetry && (
        <Button
          variant="outline"
          size={compact ? "sm" : "default"}
          onClick={onRetry}
          className="mt-3 border-destructive text-destructive hover:bg-destructive/10"
        >
          {retryLabel || t("common.retry")}
        </Button>
      )}
    </div>
  );
}
