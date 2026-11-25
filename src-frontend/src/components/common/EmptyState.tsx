/** components/common/EmptyState.tsx — Phase 50-J: 统一空状态组件
 *
 * 用于列表为空、搜索结果为空、未选择对象等场景。
 * 保持图标、标题、描述、操作按钮的视觉一致性。
 */
import { ReactNode } from "react";
import { Button } from "../ui/button";

interface EmptyStateProps {
  icon?: ReactNode;
  title: string;
  description?: string;
  action?: { label: string; onClick: () => void };
  compact?: boolean;
}

export function EmptyState({ icon, title, description, action, compact }: EmptyStateProps) {
  return (
    <div className={`flex w-full min-w-0 flex-col items-center justify-center text-center ${compact ? "py-6 px-4" : "py-10 px-6"}`}>
      {icon && (
        <div
          className={`rounded-full bg-muted flex items-center justify-center mb-3 ${compact ? "w-10 h-10" : "w-14 h-14"}`}
        >
          <span className={`text-muted-foreground ${compact ? "scale-90" : ""}`}>{icon}</span>
        </div>
      )}
      <h3 className={`font-semibold text-foreground break-words ${compact ? "text-sm" : "text-base"}`}>{title}</h3>
      {description && (
        <p className={`text-muted-foreground mt-1 max-w-xs break-words ${compact ? "text-xs" : "text-sm"}`}>{description}</p>
      )}
      {action && (
        <Button onClick={action.onClick} size={compact ? "sm" : "default"} className="mt-3">
          {action.label}
        </Button>
      )}
    </div>
  );
}
