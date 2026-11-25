import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useTaskStore } from "../store/taskStore";
import { FocusTrap } from "./FocusTrap";

/**
 * ContextRing — Claude Code / Cursor 风格的上下文用量环形图。
 *
 * 当上下文用量 > 50% 时，在输入框上方显示。
 * 颜色分级（对应后端压缩阈值）：
 *   - 50-60%:  蓝灰（微型压缩触发线 60%）
 *   - 60-95%:  蓝灰（微型压缩运行中，0 API 调用）
 *   - 95-98%:  橙黄（全量 LLM 压缩触发线 95%）
 *   - 98%+:    红色急闪（紧急修剪）
 *
 * 点击可查看详情弹出框。
 */

interface ContextRingProps {
  onCompact?: () => void;
}

function getColorLevel(pct: number): "normal" | "warning" | "danger" | "critical" {
  if (pct >= 98) return "critical";
  if (pct >= 95) return "danger";
  if (pct >= 50) return "warning";
  return "normal";
}

const LEVEL_BADGE: Record<string, string> = {
  normal: "bg-primary/20 text-primary",
  warning: "bg-warning/20 text-warning",
  danger: "bg-destructive/20 text-destructive",
  critical: "bg-destructive/30 text-destructive animate-pulse",
};

const CIRCLE_R = 10;
const CIRCLE_C = 2 * Math.PI * CIRCLE_R;

export function ContextRing({ onCompact }: ContextRingProps) {
  const { t } = useTranslation();
  const cu = useTaskStore((s) => s.contextUsage);
  const pct = cu.pct;
  const tokens = cu.tokens;
  const contextWindow = cu.window || 131072;
  const level = getColorLevel(pct);
  const [detailOpen, setDetailOpen] = useState(false);

  // Only show when > 50%
  if (pct < 50) return null;

  const dashOffset = CIRCLE_C * (1 - Math.min(pct, 100) / 100);

  const strokeColor =
    level === "critical" || level === "danger"
      ? "var(--destructive)"
      : level === "warning"
        ? "var(--warning)"
        : "var(--primary)";

  return (
    <div className="relative">
      <div
        className={`context-ring ${level}`}
        role="button"
        tabIndex={0}
        aria-label={t("phase49.contextRing.title", { pct })}
        onClick={() => setDetailOpen(!detailOpen)}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            setDetailOpen(!detailOpen);
          }
        }}
        title={t("phase49.contextRing.title", { pct })}
      >
        <svg width="24" height="24" viewBox="0 0 24 24" className="shrink-0">
          <circle cx="12" cy="12" r={CIRCLE_R} fill="none" stroke="var(--border-default)" strokeWidth="2.5" />
          <circle
            cx="12"
            cy="12"
            r={CIRCLE_R}
            fill="none"
            stroke={strokeColor}
            strokeWidth="2.5"
            strokeLinecap="round"
            strokeDasharray={CIRCLE_C}
            strokeDashoffset={dashOffset}
            transform="rotate(-90 12 12)"
          />
        </svg>
        <span
          className={`text-[10px] font-semibold ${level === "critical" ? "text-destructive" : "text-muted-foreground"}`}
        >
          {pct}%
        </span>

        {detailOpen && (
          <FocusTrap active={detailOpen}>
          <div className="absolute bottom-full right-0 mb-2 bg-background border border-border rounded-lg shadow-lg p-3 min-w-[200px] z-dropdown animate-fade-in">
            <div className="text-xs font-semibold mb-2">{t("phase49.contextRing.detailsTitle")}</div>
            <div className="space-y-1.5 text-[11px] text-muted-foreground">
              <div className="flex justify-between">
                <span>{t("phase49.contextRing.used")}</span>
                <span className="font-mono text-foreground">{tokens.toLocaleString()} tokens</span>
              </div>
              <div className="flex justify-between">
                <span>{t("phase49.contextRing.window")}</span>
                <span className="font-mono text-foreground">{contextWindow.toLocaleString()} tokens</span>
              </div>
              <div className="flex justify-between">
                <span>{t("phase49.contextRing.ratio")}</span>
                <span className={`font-semibold ${LEVEL_BADGE[level].split(" ")[1]}`}>{pct}%</span>
              </div>
              {level === "danger" || level === "critical" ? (
                <div className="flex justify-between">
                  <span>{t("phase49.contextRing.status")}</span>
                  <span className={`px-1.5 py-0.5 rounded text-[10px] ${LEVEL_BADGE[level]}`}>
                    {level === "critical" ? t("phase49.contextRing.critical") : t("phase49.contextRing.nearLimit")}
                  </span>
                </div>
              ) : null}
            </div>
            {pct >= 50 && onCompact && (
              <button
                onClick={(e) => {
                  e.stopPropagation();
                  setDetailOpen(false);
                  onCompact();
                }}
                className="w-full mt-2 py-1 text-[11px] text-primary hover:bg-primary-muted rounded transition-colors"
              >
                {t("phase49.contextRing.compact")}
              </button>
            )}
          </div>
          </FocusTrap>
        )}
      </div>
    </div>
  );
}
