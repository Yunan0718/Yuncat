import { useEffect, useMemo, useRef, useState, useCallback } from "react";
import { useTranslation } from "react-i18next";
import {
  ReactFlow,
  Background,
  Controls,
  MiniMap,
  useNodesState,
  useEdgesState,
  BackgroundVariant,
  type Node,
  type Edge,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { useApi } from "../hooks/useApi";
import { useAppStore } from "../store/appStore";

interface GraphNode {
  id: string;
  title: string;
  score: number;
  source: string;
  tags: string[];
}
interface GraphEdge {
  id: string;
  source: string;
  target: string;
}

/** 评分 → 颜色映射 */
function scoreColor(score: number): string {
  if (score >= 2.0) return "var(--destructive)"; // 高 — 红色
  if (score >= 1.0) return "var(--warning)"; // 中 — 琥珀色
  if (score >= 0.5) return "var(--primary)"; // 低 — 靛蓝
  return "var(--muted-foreground)"; // 极低 — 灰色
}

/** 标题截断 */
function trunc(s: string, n: number) {
  if (s.length <= n) return s;
  return s.slice(0, n - 1) + "…";
}

/** Phase 18: 知识图谱——react-flow 可视化 knowledge_nodes 树形结构 */
export function KnowledgeGraph() {
  const { t } = useTranslation();
  const { api } = useApi();
  const backendReady = useAppStore((s) => s.backendReady);
  const [nodes, setNodes, onNodesChange] = useNodesState<Node>([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([]);
  const [selectedNode, setSelectedNode] = useState<{
    title: string;
    score: number;
    source: string;
    tags: string[];
  } | null>(null);
  const [error, setError] = useState("");
  // Phase 18: 搜索/筛选
  const [searchQuery, setSearchQuery] = useState("");
  const [minScore, setMinScore] = useState(0);
  const [allNodes, setAllNodes] = useState<GraphNode[]>([]);

  const loadGraph = useCallback(async () => {
    setError("");
    try {
      const data = await api<{ nodes: GraphNode[]; edges: GraphEdge[] }>("/api/v1/knowledge/graph");
      if (data.nodes.length === 0) {
        setError(t("knowledgeGraph.emptyError"));
        return;
      }
      // store all edges separately for filtering
      _edgesRef.current = data.edges;
      setAllNodes(data.nodes);
      applyFilter(data.nodes, data.edges, searchQuery, minScore);
    } catch {
      setError(t("knowledgeGraph.loadError"));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [api, t]);

  const applyFilter = useCallback(
    (_nodes: GraphNode[], _edges: GraphEdge[], query: string, score: number) => {
      const filtered = _nodes.filter((n) => {
        if (score > 0 && n.score < score) return false;
        if (
          query &&
          !n.title.toLowerCase().includes(query.toLowerCase()) &&
          !n.tags?.some((t) => t.toLowerCase().includes(query.toLowerCase()))
        )
          return false;
        return true;
      });
      const filteredIds = new Set(filtered.map((n) => n.id));
      const flowNodes: Node[] = filtered.map((n, i) => ({
        id: n.id,
        position: { x: 50 + (i % 5) * 220, y: 20 + Math.floor(i / 5) * 120 },
        data: { ...n },
        style: {
          background: scoreColor(n.score) + "10",
          border: `2px solid ${scoreColor(n.score)}`,
          borderRadius: "8px",
          padding: "8px 12px",
          width: 200,
          fontSize: 11,
        },
      }));
      const flowEdges: Edge[] = _edges
        .filter((e) => filteredIds.has(e.source) && filteredIds.has(e.target))
        .map((e) => ({
          id: e.id,
          source: e.source,
          target: e.target,
          animated: true,
          style: { stroke: "var(--primary)", strokeWidth: 1.5, opacity: 0.5 },
        }));
      setNodes(flowNodes);
      setEdges(flowEdges);
    },
    [setEdges, setNodes],
  );

  useEffect(() => {
    if (!backendReady) return;
    loadGraph();
  }, [backendReady, loadGraph]);

  const _edgesRef = useRef<GraphEdge[]>([]);

  // Phase 18: 搜索/筛选变化时重新应用过滤
  useEffect(() => {
    if (allNodes.length === 0) return;
    applyFilter(allNodes, _edgesRef.current, searchQuery, minScore);
  }, [allNodes, applyFilter, searchQuery, minScore]);

  // 自定义节点渲染
  const customNode = useCallback(({ data }: { data: GraphNode }) => {
    return (
      <div
        className="cursor-pointer"
        onClick={() => setSelectedNode({ title: data.title, score: data.score, source: data.source, tags: data.tags })}
      >
        <div className="text-xs font-semibold truncate" style={{ color: scoreColor(data.score) }}>
          {trunc(data.title, 28)}
        </div>
        <div className="flex items-center gap-1 mt-0.5">
          <div className="flex-1 h-1 bg-secondary rounded-full overflow-hidden">
            <div
              className="h-full rounded-full transition-all"
              style={{ width: `${Math.min(100, data.score * 50)}%`, backgroundColor: scoreColor(data.score) }}
            />
          </div>
          <span className="text-[9px] text-muted-foreground">{data.score.toFixed(1)}</span>
        </div>
        {data.tags?.length > 0 && (
          <div className="flex flex-wrap gap-0.5 mt-1">
            {data.tags.slice(0, 3).map((t) => (
              <span key={t} className="text-[8px] bg-secondary px-1 rounded text-muted-foreground">
                {t}
              </span>
            ))}
          </div>
        )}
      </div>
    );
  }, []);

  const nodeTypes = useMemo(() => ({ default: customNode }), [customNode]);

  return (
    <div className="flex flex-col h-full">
      {/* 操作栏 */}
      <div className="h-8 border-b flex items-center px-3 gap-2 shrink-0">
        <span className="text-xs font-semibold text-muted-foreground">{t("knowledgeGraph.title")}</span>
        {/* Phase 18: 搜索/筛选 */}
        <input
          value={searchQuery}
          onChange={(e) => setSearchQuery(e.target.value)}
          placeholder={t("knowledgeGraph.searchPlaceholder")}
          className="flex-1 max-w-[200px] px-2 py-0.5 text-[10px] border rounded bg-background"
        />
        <select
          value={minScore}
          onChange={(e) => setMinScore(Number(e.target.value))}
          className="px-1.5 py-0.5 text-[10px] border rounded bg-background"
          title={t("legacy.kg_5e3f")}
        >
          <option value={0}>{t("knowledgeGraph.allScores")}</option>
          <option value={0.5}>≥ 0.5</option>
          <option value={1}>≥ 1.0</option>
          <option value={2}>≥ 2.0</option>
        </select>
        <button onClick={loadGraph} className="text-[10px] text-muted-foreground hover:text-foreground">
          {t("knowledgeGraph.refresh")}
        </button>
        <div className="flex items-center gap-2 text-[9px] text-muted-foreground">
          <span className="flex items-center gap-1">
            <span className="w-2 h-2 rounded-full bg-destructive inline-block" />
            {t("knowledgeGraph.scoreHigh")}
          </span>
          <span className="flex items-center gap-1">
            <span className="w-2 h-2 rounded-full bg-warning inline-block" />
            {t("knowledgeGraph.scoreMedium")}
          </span>
          <span className="flex items-center gap-1">
            <span className="w-2 h-2 rounded-full bg-primary inline-block" />
            {t("knowledgeGraph.scoreLow")}
          </span>
        </div>
      </div>

      {error && <div className="px-3 py-8 text-center text-xs text-muted-foreground">{error}</div>}

      {/* 主画布 */}
      <div className="flex-1 min-h-0">
        <ReactFlow
          nodes={nodes}
          edges={edges}
          onNodesChange={onNodesChange}
          onEdgesChange={onEdgesChange}
          nodeTypes={nodeTypes}
          fitView
          attributionPosition="bottom-left"
        >
          <Background variant={BackgroundVariant.Dots} gap={20} size={1} />
          <Controls />
          <MiniMap
            nodeColor={(n) => {
              const d = n.data as unknown as GraphNode;
              return scoreColor(d?.score || 0);
            }}
            style={{ height: 120 }}
          />
        </ReactFlow>
      </div>

      {/* Phase 18: 选中节点详情面板 */}
      {selectedNode && (
        <div className="shrink-0 border-t px-3 py-2 bg-secondary space-y-1">
          <div className="flex items-center justify-between">
            <span className="text-xs font-semibold">{trunc(selectedNode.title, 60)}</span>
            <button
              onClick={() => setSelectedNode(null)}
              aria-label={t("common.close")}
              className="text-muted-foreground hover:text-foreground text-xs"
            >
              ×
            </button>
          </div>
          <div className="flex items-center gap-3 text-[10px] text-muted-foreground">
            <span>
              {t("knowledgeGraph.scoreLabel")}{" "}
              <span style={{ color: scoreColor(selectedNode.score) }}>{selectedNode.score.toFixed(2)}</span>
            </span>
            <span>
              {t("knowledgeGraph.sourceLabel")} {selectedNode.source || t("knowledgeGraph.unknownSource")}
            </span>
          </div>
          {selectedNode.tags.length > 0 && (
            <div className="flex flex-wrap gap-1">
              {selectedNode.tags.map((t) => (
                <span key={t} className="text-[9px] bg-background px-1.5 py-0.5 rounded border">
                  {t}
                </span>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
