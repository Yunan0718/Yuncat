export interface VersionNode {
  id: string;
  label?: string;
  prompt?: string;
  role?: string;
  createdAt?: number;
}

export interface VersionEdge {
  source: string;
  target: string;
}

interface MessageVersionTreeProps {
  nodes: VersionNode[];
  edges: VersionEdge[];
  activeId?: string;
  onSelect: (id: string) => void;
}

function buildChildrenMap(edges: VersionEdge[]) {
  const children: Record<string, string[]> = {};
  for (const e of edges) {
    children[e.source] = children[e.source] || [];
    children[e.source].push(e.target);
  }
  return children;
}

function findRoots(nodes: VersionNode[], edges: VersionEdge[]) {
  const hasParent = new Set(edges.map((e) => e.target));
  return nodes.filter((n) => !hasParent.has(n.id));
}

function formatLabel(node: VersionNode) {
  if (node.label) return node.label;
  if (node.prompt) return node.prompt.slice(0, 24);
  return node.id.slice(0, 8);
}

function TreeNode({
  node,
  depth,
  childrenMap,
  nodeMap,
  activeId,
  onSelect,
}: {
  node: VersionNode;
  depth: number;
  childrenMap: Record<string, string[]>;
  nodeMap: Map<string, VersionNode>;
  activeId?: string;
  onSelect: (id: string) => void;
}) {
  const childrenIds = childrenMap[node.id] || [];
  const isActive = activeId === node.id;
  return (
    <div className="relative">
      <div className="flex items-center gap-2 py-1" style={{ marginLeft: depth * 20 }}>
        {depth > 0 && <div className="w-4 h-px bg-border shrink-0" />}
        <button
          type="button"
          onClick={() => onSelect(node.id)}
          className={`text-2xs px-2 py-1 rounded border transition-colors text-left max-w-[200px] truncate ${
            isActive
              ? "bg-primary text-primary-foreground border-primary"
              : "bg-card hover:bg-muted border-border text-foreground"
          }`}
          title={node.prompt || node.label || node.id}
        >
          {formatLabel(node)}
        </button>
      </div>
      {childrenIds.length > 0 && (
        <div className="border-l border-border ml-2 pl-2">
          {childrenIds.map((cid) => {
            const child = nodeMap.get(cid);
            if (!child) return null;
            return (
              <TreeNode
                key={cid}
                node={child}
                depth={0}
                childrenMap={childrenMap}
                nodeMap={nodeMap}
                activeId={activeId}
                onSelect={onSelect}
              />
            );
          })}
        </div>
      )}
    </div>
  );
}

export function MessageVersionTree({ nodes, edges, activeId, onSelect }: MessageVersionTreeProps) {
  if (!nodes.length) return null;
  const nodeMap = new Map(nodes.map((n) => [n.id, n]));
  const childrenMap = buildChildrenMap(edges);
  const roots = findRoots(nodes, edges);
  return (
    <div className="p-2 bg-card border rounded-lg overflow-x-auto">
      {roots.map((root) => (
        <TreeNode
          key={root.id}
          node={root}
          depth={0}
          childrenMap={childrenMap}
          nodeMap={nodeMap}
          activeId={activeId}
          onSelect={onSelect}
        />
      ))}
    </div>
  );
}
