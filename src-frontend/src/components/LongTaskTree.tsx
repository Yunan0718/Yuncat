import { RefreshCw } from "lucide-react";
import { useTranslation } from "react-i18next";
import type { PlanTreeNode } from "../store/taskStore";

interface LongTaskTreeProps {
  nodes: PlanTreeNode[];
  currentStepId: string | null;
  onSelect?: (nodeId: string) => void;
  /** 重分解某个节点的子树（删除 pending 子孙后重新分解） */
  onDecompose?: (node: PlanTreeNode) => void;
}

/** 按 parent_id 分组子节点 */
function buildChildrenMap(nodes: PlanTreeNode[]): Record<string, PlanTreeNode[]> {
  const children: Record<string, PlanTreeNode[]> = {};
  for (const n of nodes) {
    if (n.parent_id) (children[n.parent_id] ||= []).push(n);
  }
  return children;
}

/** 节点状态 → Tailwind 色标 class */
const STATUS_CLASS: Record<PlanTreeNode["status"], string> = {
  pending: "bg-muted text-muted-foreground",
  running: "bg-primary/10 text-primary animate-pulse",
  completed: "bg-success/10 text-success",
  failed: "bg-destructive/10 text-destructive",
  blocked: "bg-warning/10 text-warning",
  skipped: "bg-muted text-muted-foreground/50 line-through",
};

function TreeNode({
  node,
  depth,
  childrenMap,
  currentStepId,
  onSelect,
  onDecompose,
}: {
  node: PlanTreeNode;
  depth: number;
  childrenMap: Record<string, PlanTreeNode[]>;
  currentStepId: string | null;
  onSelect?: (nodeId: string) => void;
  onDecompose?: (node: PlanTreeNode) => void;
}) {
  const { t } = useTranslation();
  const children = childrenMap[node.id] || [];
  const isCurrent = currentStepId === node.id;
  return (
    <div className="relative group" role="treeitem" aria-level={depth + 1} aria-selected={isCurrent} aria-expanded={children.length > 0 ? true : undefined}>
      <div className="flex items-center gap-2 py-1" style={{ marginLeft: depth * 20 }}>
        {depth > 0 && <div className="w-4 h-px bg-border shrink-0" />}
        <button
          type="button"
          onClick={() => onSelect?.(node.id)}
          aria-pressed={isCurrent}
          className={`text-2xs px-2 py-1 rounded border transition-colors text-left max-w-[280px] truncate ${
            STATUS_CLASS[node.status] || STATUS_CLASS.pending
          } ${isCurrent ? "ring-2 ring-primary border-primary" : "border-border"}`}
          title={node.description || node.title}
        >
          {node.title}
        </button>
        {onDecompose && (
          <button
            type="button"
            onClick={(e) => {
              e.stopPropagation();
              onDecompose(node);
            }}
            aria-label={t("supervisor.decompose")}
            title={t("supervisor.decomposeHint")}
            className="rounded p-0.5 text-muted-foreground opacity-60 transition-opacity hover:text-foreground group-hover:opacity-100 focus-visible:opacity-100 sm:opacity-0"
          >
            <RefreshCw size={11} />
          </button>
        )}
      </div>
      {children.length > 0 && (
        <div role="group" className="border-l border-border ml-2 pl-2">
          {children.map((c) => (
            <TreeNode
              key={c.id}
              node={c}
              depth={depth + 1}
              childrenMap={childrenMap}
              currentStepId={currentStepId}
              onSelect={onSelect}
              onDecompose={onDecompose}
            />
          ))}
        </div>
      )}
    </div>
  );
}

/** Phase 82: 长任务分解树——递归渲染 plan_steps 节点，状态色标 + 当前节点高亮 */
export function LongTaskTree({ nodes, currentStepId, onSelect, onDecompose }: LongTaskTreeProps) {
  const { t } = useTranslation();
  if (!nodes.length) return null;
  const childrenMap = buildChildrenMap(nodes);
  const roots = nodes.filter((n) => !n.parent_id);
  return (
    <div role="tree" aria-label={t("supervisor.tree")} className="p-2 bg-card border rounded-lg overflow-x-auto">
      {roots.map((root) => (
        <TreeNode
          key={root.id}
          node={root}
          depth={0}
          childrenMap={childrenMap}
          currentStepId={currentStepId}
          onSelect={onSelect}
          onDecompose={onDecompose}
        />
      ))}
    </div>
  );
}
