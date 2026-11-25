import { Sparkles, Loader2 } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Button } from "../ui/button";

interface PromptOptimizeButtonProps {
  disabled?: boolean;
  loading?: boolean;
  onClick: () => void;
}

/**
 * Phase 73-A: ✨ 优化按钮
 *
 * 行为：
 * - 空输入时 disabled（由父组件控制 disabled prop）
 * - loading 时显示 spinner + 不可点击
 * - 正常态显示 Sparkles ✨ icon
 * - aria-label 保证无障碍
 */
export function PromptOptimizeButton({ disabled, loading, onClick }: PromptOptimizeButtonProps) {
  const { t } = useTranslation();
  return (
    <Button
      variant="ghost"
      size="icon"
      onClick={onClick}
      disabled={disabled || loading}
      title={t("chat.optimizePrompt.button")}
      aria-label={t("chat.optimizePrompt.button")}
      data-testid="prompt-optimize-button"
    >
      {loading ? (
        <Loader2 className="h-4 w-4 animate-spin" data-testid="optimize-loading" />
      ) : (
        <Sparkles className="h-4 w-4" />
      )}
    </Button>
  );
}
