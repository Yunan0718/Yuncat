import { useState, type ReactNode } from "react";
import { Brain, ChevronDown, ChevronRight, CircleAlert, ListChecks, Loader2, ShieldCheck, Wrench } from "lucide-react";

import type { TrajectoryItem, TrajectoryKind } from "../../lib/trajectory";

/**
 * 轨迹卡片（2026-09-16，大改动第二步）
 *
 * 一条回复里的过程信息（思考 / 计划 / 工具调用）收进**一张卡片**，每条一行：
 * 图标 + 一行摘要 + 状态 + 展开箭头，点开才看到细节。
 *
 * 为什么这么做：之前思考块、执行时间线、结果正文是三个各自带边框的区块，
 * 于是"框套框"、细线多、状态散。现在过程信息在一张卡里成行排布，
 * 正文（最终回答）作为 children 挂在卡片底部，仍然是展开可见的。
 *
 * 折叠状态只在这里实现一遍；以后新增事件类型（比如 MCP 调用）只需在
 * lib/trajectory.ts 里多投影一种 kind，这里加一个图标即可。
 */
const KIND_ICON: Record<TrajectoryKind, typeof Brain> = {
  thinking: Brain,
  plan: ListChecks,
  tool: Wrench,
  approval: ShieldCheck,
  message: Wrench,
  error: CircleAlert,
};

function StatusDot({ status }: { status: TrajectoryItem["status"] }) {
  if (status === "running") {
    return <Loader2 size={11} className="animate-spin text-primary shrink-0" aria-label="running" />;
  }
  if (status === "failed") {
    return <span className="h-1.5 w-1.5 rounded-full bg-destructive-solid shrink-0" aria-label="failed" />;
  }
  return <span className="h-1.5 w-1.5 rounded-full bg-success shrink-0" aria-label="done" />;
}

/** 一行：折叠时只显示摘要；展开后显示细节（内嵌块，不再画边框） */
export function TrajectoryRow({ item, defaultOpen }: { item: TrajectoryItem; defaultOpen?: boolean }) {
  const [open, setOpen] = useState(defaultOpen ?? false);
  const Icon = KIND_ICON[item.kind] ?? Wrench;
  const expandable = item.collapsible && item.detail.trim().length > 0;
  return (
    <div className="rounded-lg overflow-hidden">
      <button
        type="button"
        disabled={!expandable}
        onClick={() => setOpen((v) => !v)}
        aria-expanded={expandable ? open : undefined}
        data-testid={`trajectory-row-${item.kind}`}
        className={`w-full flex items-center gap-2 px-2 py-1 text-left rounded-lg transition-colors ${
          expandable ? "hover:bg-surface-3" : "cursor-default"
        }`}
      >
        {expandable ? (
          open ? <ChevronDown size={12} className="text-muted-foreground shrink-0" /> : <ChevronRight size={12} className="text-muted-foreground shrink-0" />
        ) : (
          <span className="w-3 shrink-0" />
        )}
        <Icon size={12} className="text-muted-foreground shrink-0" />
        <span className="flex-1 min-w-0 truncate text-[11px] text-foreground-secondary">{item.title}</span>
        {typeof item.durationMs === "number" && (
          <span className="text-2xs text-muted-foreground shrink-0 font-mono">
            {item.durationMs >= 1000 ? `${(item.durationMs / 1000).toFixed(1)}s` : `${item.durationMs}ms`}
          </span>
        )}
        <StatusDot status={item.status} />
      </button>
      {expandable && open && (
        <pre
          data-testid={`trajectory-detail-${item.kind}`}
          className="mt-0.5 mx-2 mb-1 rounded-lg bg-surface-3 px-2.5 py-2 text-[11px] leading-relaxed text-foreground-secondary whitespace-pre-wrap break-words max-h-72 overflow-y-auto font-mono"
        >
          {item.detail}
        </pre>
      )}
    </div>
  );
}

export function TrajectoryCard({
  items,
  children,
  agentId,
}: {
  items: TrajectoryItem[];
  /** 正文（最终回答）挂在卡片底部——正文永远展开 */
  children?: ReactNode;
  agentId?: string;
}) {
  const process = items.filter((item) => item.kind !== "message" && item.kind !== "error");
  if (process.length === 0 && !children) return null;
  return (
    <div className="rounded-xl bg-surface-1 p-1.5 space-y-0.5" data-testid="trajectory-card">
      {agentId && process.length > 0 && (
        <div className="px-2 pt-0.5 pb-1 text-2xs text-muted-foreground">{agentId}</div>
      )}
      {process.map((item) => (
        <TrajectoryRow key={item.id} item={item} />
      ))}
      {children && <div className="px-1.5 pt-1.5">{children}</div>}
    </div>
  );
}
