import { useTranslation } from "react-i18next";

/**
 * 状态圆点——**状态绝不能只靠颜色传达**（借鉴 deepseek-harness 的 StateDot）。
 *
 * dsh 的做法：颜色与扫光 `aria-hidden`，另渲染 `visuallyHidden` 的运行/失败/中断文本。
 * Yuncat 此前有多处 `w-2 h-2 rounded-full bg-success/destructive/…`，四档状态全靠
 * 颜色区分——色觉障碍用户与屏幕阅读器都无法分辨。这里统一为：
 *
 * - 圆点 `aria-hidden`（纯装饰）
 * - 状态以 `sr-only` 文本 + `title` 悬浮提示给出
 */
export interface StatusDotProps {
  /** 无障碍文本与悬浮提示——必填，强制调用方说明圆点含义 */
  label: string;
  /** 圆点外观类（颜色/边框） */
  className?: string;
  /** 尺寸类，默认 8px */
  sizeClassName?: string;
  /** 是否在跑脉冲动画（进行中） */
  pulse?: boolean;
}

export function StatusDot({
  label,
  className = "",
  sizeClassName = "w-2 h-2",
  pulse,
}: StatusDotProps) {
  return (
    <span className="inline-flex items-center" title={label}>
      <span
        aria-hidden="true"
        className={`rounded-full inline-block ${sizeClassName} ${pulse ? "animate-pulse" : ""} ${className}`}
      />
      <span className="sr-only">{label}</span>
    </span>
  );
}

/** 供应商 / 模型健康状态 dot 的外观与文案键。 */
const HEALTH: Record<string, { dot: string; key: string }> = {
  active: { dot: "bg-success", key: "health.active" },
  configured: { dot: "bg-success/60", key: "health.configured" },
  failed: { dot: "bg-destructive", key: "health.failed" },
};

/** 未配置 / 未知兜底 */
const HEALTH_UNKNOWN = { dot: "border border-muted-foreground/30", key: "health.unknown" };

/**
 * 供应商健康状态点（active / configured / failed / 未配置）。
 *
 * 原先 `healthDot()` 在 ModelSelector 与 AgentManager 各写一份且纯靠颜色，
 * 这里收拢为单一实现并补上无障碍文本。
 */
export function HealthDot({ status }: { status?: string }) {
  const { t } = useTranslation();
  const entry = (status && HEALTH[status]) || HEALTH_UNKNOWN;
  return <StatusDot label={t(entry.key)} className={entry.dot} />;
}
