import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  ReactFlow,
  ReactFlowProvider,
  Background,
  Controls,
  MiniMap,
  addEdge,
  useNodesState,
  useEdgesState,
  type Connection,
  type Node,
  type Edge,
  type NodeMouseHandler,
  type ReactFlowInstance,
  BackgroundVariant,
  MarkerType,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { useApi } from "../hooks/useApi";
import { useAppStore } from "../store/appStore";
import { WorkflowTimeline } from "../components/workflow/WorkflowTimeline";
import { WorkflowTriggersButton } from "../components/workflow/WorkflowTriggersButton";
import { WorkflowRunsList } from "../components/workflow/WorkflowRunsList";
import { saveWorkflowGraph } from "../lib/workflowSave";
import { DetailDrawer } from "../components/DetailDrawer";
import { useConfirmDialog } from "../hooks/useConfirmDialog";
import { EmptyState } from "../components/common/EmptyState";
import { matchShortcut } from "../lib/shortcuts";
import { dataOf, graphNodeToFlowNode, createFlowNode, flowNodesToGraph, type WorkflowGraph } from "./graphUtils";
import { Layers } from "lucide-react";

const CTX_MENU_STYLE: React.CSSProperties = {
  position: "fixed",
  zIndex: 1000,
  background: "var(--background, #fff)",
  border: "1px solid var(--border, #e5e7eb)",
  borderRadius: 8,
  boxShadow: "0 4px 12px rgba(0,0,0,0.15)",
  padding: "4px 0",
  minWidth: 150,
  fontSize: 13,
};

// ---- 类型 ----
interface WorkflowInfo {
  id: string;
  name: string;
  description?: string;
  display_name?: string;
  graph: string;
  is_template: number;
}

// H12 修复：提取 NodeResult 接口——原代码 6 处 `as Record<string, string>` 绕过类型检查。
interface NodeResult {
  output?: string;
  status?: string;
  error?: string;
}

/**
 * Phase 4 WorkflowEditor: ReactFlow 可视化工作流编辑器
 *
 * 功能: 节点拖拽画布 | 连线 | 编辑 prompt | JSON↔画布 | 保存/运行
 */
export function WorkflowEditor() {
  const { t } = useTranslation();
  const { api } = useApi();
  const { confirm, confirmDialog } = useConfirmDialog();
  const backendReady = useAppStore((s) => s.backendReady);

  const [workflows, setWorkflows] = useState<WorkflowInfo[]>([]);
  const [templates, setTemplates] = useState<WorkflowInfo[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [workflowName, setWorkflowName] = useState(t("workflows.defaultName"));
  // 是否从模板加载——决定"保存"是原地更新还是另存为新工作流
  const [loadedTemplate, setLoadedTemplate] = useState(false);
  const [statusMsg, setStatusMsg] = useState<string | null>(null);
  const [lastRunId, setLastRunId] = useState<string | null>(null);
  const [showTimeline, setShowTimeline] = useState(false);
  const [showRunLogs, setShowRunLogs] = useState(false); // P2: DAG run log panel
  const [runLogs, setRunLogs] = useState<
    Array<{
      run_id: string;
      status: string;
      error_message: string;
      node_results: Record<string, NodeResult>;
      started_at: number;
      finished_at: number;
    }>
  >([]);
  const [logLoading, setLogLoading] = useState(false);
  const [nodes, setNodes, onNodesChange] = useNodesState<Node>([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([]);
  const [selectedNode, setSelectedNode] = useState<Node | null>(null);
  const [nodePrompt, setNodePrompt] = useState("");
  const [nodeToolName, setNodeToolName] = useState("");
  const [nodeToolArguments, setNodeToolArguments] = useState("{}");
  const [jsonPanel, setJsonPanel] = useState(false);
  const [jsonText, setJsonText] = useState("");
  // 右键菜单状态（type: node = 右键节点, pane = 右键画布空白）
  const [ctxMenu, setCtxMenu] = useState<{
    x: number;
    y: number;
    type: "node" | "pane";
    node?: Node;
    pos?: { x: number; y: number };
  } | null>(null);
  const idCounter = useRef(0);
  const nextId = () => `node_${Date.now()}_${idCounter.current++}`;
  const [sidebarTab, setSidebarTab] = useState<"workflows" | "templates">("workflows");
  const flowRef = useRef<ReactFlowInstance | null>(null);

  // ---- Task 7.20: Undo/Redo history stack（对标 n8n / ComfyUI / LangFlow） ----
  // 快照式历史：每次结构性变更前 push 当前 {nodes, edges} 到 past 栈，清空 future 栈。
  // 撤销：past 栈顶弹出 → 当前状态推入 future → 恢复弹出的快照。
  // 重做：future 栈顶弹出 → 当前状态推入 past → 恢复弹出的快照。
  // 限制 100 步——行业标准（VS Code undo stack 同量级），单工作流快照体积小（~KB 级）。
  const HISTORY_LIMIT = 100;
  const historyPast = useRef<{ nodes: Node[]; edges: Edge[] }[]>([]);
  const historyFuture = useRef<{ nodes: Node[]; edges: Edge[] }[]>([]);
  const [canUndo, setCanUndo] = useState(false);
  const [canRedo, setCanRedo] = useState(false);

  const syncHistoryFlags = useCallback(() => {
    setCanUndo(historyPast.current.length > 0);
    setCanRedo(historyFuture.current.length > 0);
  }, []);

  const pushHistory = useCallback(() => {
    historyPast.current.push({
      nodes: structuredClone(nodes),
      edges: structuredClone(edges),
    });
    historyFuture.current = [];
    if (historyPast.current.length > HISTORY_LIMIT) {
      historyPast.current.shift();
    }
    syncHistoryFlags();
  }, [nodes, edges, syncHistoryFlags]);

  const undo = useCallback(() => {
    if (historyPast.current.length === 0) return;
    const prev = historyPast.current.pop()!;
    historyFuture.current.push({ nodes: structuredClone(nodes), edges: structuredClone(edges) });
    setNodes(prev.nodes);
    setEdges(prev.edges);
    setSelectedNode(null);
    syncHistoryFlags();
  }, [nodes, edges, setNodes, setEdges, syncHistoryFlags]);

  const redo = useCallback(() => {
    if (historyFuture.current.length === 0) return;
    const next = historyFuture.current.pop()!;
    historyPast.current.push({ nodes: structuredClone(nodes), edges: structuredClone(edges) });
    setNodes(next.nodes);
    setEdges(next.edges);
    setSelectedNode(null);
    syncHistoryFlags();
  }, [nodes, edges, setNodes, setEdges, syncHistoryFlags]);

  // ---- 工作流列表 + 模板 ----
  const loadWorkflows = useCallback(() => {
    if (!backendReady) return;
    api<WorkflowInfo[]>("/api/v1/workflows")
      .then(setWorkflows)
      .catch((e) => console.warn("WorkflowEditor: workflows load failed:", e));
  }, [backendReady, api]);
  const loadTemplates = useCallback(() => {
    if (!backendReady) return;
    api<WorkflowInfo[]>("/api/v1/workflows/templates")
      .then(setTemplates)
      .catch((e) => console.warn("WorkflowEditor: templates load failed:", e));
  }, [backendReady, api]);
  useEffect(() => {
    loadWorkflows();
    loadTemplates();
  }, [backendReady, loadWorkflows, loadTemplates]);

  // ---- Phase 17: auto-layout helper（网格排列，避免随机位置） ----
  const autoLayout = (flowNodes: Node[]) => {
    const COLS = 3;
    const X_GAP = 260;
    const Y_GAP = 180;
    return flowNodes.map((n, i) => ({
      ...n,
      position: { x: 50 + (i % COLS) * X_GAP, y: 20 + Math.floor(i / COLS) * Y_GAP },
    }));
  };

  // ---- 加载工作流 → 画布（通过 get-by-id 获取完整 graph） ----
  const loadToCanvas = async (wf: WorkflowInfo) => {
    setSelectedId(wf.id);
    setLoadedTemplate(Boolean(wf.is_template));
    setWorkflowName(wf.name);
    pushHistory(); // Task 7.20: 加载新工作流前保存当前画布状态（可撤销恢复）
    try {
      const full = await api<{ id: string; name: string; graph: unknown }>(`/api/v1/workflows/${wf.id}`);
      const rawGraph = full.graph;
      const graph: WorkflowGraph =
        typeof rawGraph === "string" ? (JSON.parse(rawGraph) as WorkflowGraph) : (rawGraph as WorkflowGraph);
      const flowNodes: Node[] = graph.nodes.map(graphNodeToFlowNode);
      const flowEdges: Edge[] = graph.edges.map((e) => ({
        id: `${e.from}->${e.to}`,
        source: e.from,
        target: e.to,
        markerEnd: { type: MarkerType.ArrowClosed },
      }));
      // Phase 17: auto-layout——网格排列避免随机位置
      setNodes(autoLayout(flowNodes));
      setEdges(flowEdges);
      setStatusMsg(`${t("workflows.loaded")}: ${wf.name}`);
    } catch {
      setStatusMsg(t("workflows.parseErr"));
    }
  };

  // ---- 连线 ----
  const onConnect = useCallback(
    (conn: Connection) => {
      pushHistory(); // Task 7.20: 连线前保存状态
      setEdges((eds) => addEdge({ ...conn, markerEnd: { type: MarkerType.ArrowClosed } }, eds));
    },
    [setEdges, pushHistory],
  );

  // ---- 拖入新节点 ----
  const onDragOver = useCallback((event: React.DragEvent) => {
    event.preventDefault();
    event.dataTransfer.dropEffect = "move";
  }, []);

  const onDrop = useCallback(
    (event: React.DragEvent) => {
      event.preventDefault();
      const nodeType = event.dataTransfer.getData("application/reactflow-type");
      if (!nodeType) return;
      const pos = { x: event.clientX - 280, y: event.clientY - 80 };
      pushHistory(); // Task 7.20: 拖入节点前保存状态
      setNodes((nds) => [...nds, createFlowNode(nextId(), nodeType, pos)]);
    },
    [setNodes, pushHistory],
  );

  // ---- 右键上下文菜单 ----
  const onNodeContextMenu: NodeMouseHandler = useCallback((event, node) => {
    event.preventDefault();
    setCtxMenu({ x: event.clientX, y: event.clientY, type: "node", node, pos: node.position });
  }, []);

  // 右键画布空白 → 添加节点
  const onPaneContextMenu = useCallback((event: React.MouseEvent | MouseEvent) => {
    event.preventDefault();
    setCtxMenu({ x: (event as MouseEvent).clientX, y: (event as MouseEvent).clientY, type: "pane" });
  }, []);

  const closeCtxMenu = () => setCtxMenu(null);

  // 统一的"添加节点"——从画布位置计算 flow 坐标
  const addNodeAtContext = (nodeType: string) => {
    const flowPos = flowRef.current?.screenToFlowPosition({ x: ctxMenu!.x, y: ctxMenu!.y }) || {
      x: ctxMenu!.x - 280,
      y: ctxMenu!.y - 60,
    };
    pushHistory(); // Task 7.20: 右键添加节点前保存状态
    setNodes((nds) => [...nds, createFlowNode(nextId(), nodeType, flowPos)]);
    closeCtxMenu();
  };

  // ---- 点击编辑 ----
  const initNodeEditor = useCallback((node: Node) => {
    setSelectedNode(node);
    const d = dataOf(node);
    setNodePrompt(d.prompt ?? "");
    setNodeToolName(d.tool_name ?? "");
    setNodeToolArguments(
      d.arguments && typeof d.arguments === "object"
        ? JSON.stringify(d.arguments, null, 2)
        : "{}",
    );
  }, []);

  const onNodeClick = useCallback((_event: React.MouseEvent, node: Node) => {
    initNodeEditor(node);
  }, [initNodeEditor]);

  const onNodeDoubleClick: NodeMouseHandler = useCallback((_event, node) => {
    initNodeEditor(node);
  }, [initNodeEditor]);

  // Delete 键删除选中节点
  useEffect(() => {
    const h = (e: KeyboardEvent) => {
      if (e.key === "Delete" && selectedNode && document.activeElement?.closest(".react-flow")) {
        pushHistory(); // Task 7.20: 删除节点前保存状态（可撤销恢复）
        setNodes((nds) => nds.filter((n) => n.id !== selectedNode.id));
        setEdges((eds) => eds.filter((e) => e.source !== selectedNode.id && e.target !== selectedNode.id));
        setSelectedNode(null);
      }
    };
    document.addEventListener("keydown", h);
    return () => document.removeEventListener("keydown", h);
  }, [selectedNode, setEdges, setNodes, pushHistory]);

  // Task 7.20: Undo/Redo 键盘快捷键——Ctrl+Z 撤销 / Ctrl+Y 或 Ctrl+Shift+Z 重做
  // 焦点在 input/textarea/contenteditable 时跳过——让浏览器原生 undo 工作
  useEffect(() => {
    const h = (e: KeyboardEvent) => {
      const el = document.activeElement;
      if (
        el &&
        (el.tagName === "INPUT" || el.tagName === "TEXTAREA" ||
          (el instanceof HTMLElement && el.isContentEditable))
      ) {
        return;
      }
      if (matchShortcut(e, "undo")) {
        e.preventDefault();
        undo();
      } else if (matchShortcut(e, "redo") || ((e.ctrlKey || e.metaKey) && e.shiftKey && e.key.toLowerCase() === "z")) {
        e.preventDefault();
        redo();
      }
    };
    document.addEventListener("keydown", h);
    return () => document.removeEventListener("keydown", h);
  }, [undo, redo]);

  const updateNodeData = () => {
    if (!selectedNode) return;
    const isTool = dataOf(selectedNode).nodeType === "tool_execute";
    let argsObj: Record<string, unknown> | undefined;
    if (isTool) {
      try {
        argsObj = JSON.parse(nodeToolArguments || "{}") as Record<string, unknown>;
      } catch {
        setStatusMsg(t("workflows.invalidArguments"));
        return;
      }
    }
    pushHistory(); // Task 7.20: 修改节点数据前保存状态
    setNodes((nds) =>
      nds.map((n) => {
        if (n.id !== selectedNode.id) return n;
        const d = dataOf(n);
        const base = { ...d, prompt: nodePrompt };
        if (isTool) {
          base.tool_name = nodeToolName;
          base.arguments = argsObj;
          base.label = nodeToolName || "tool";
        } else {
          base.label = nodePrompt.slice(0, 60);
        }
        return { ...n, data: base };
      }),
    );
    setSelectedNode(null);
  };

  const deleteNode = (node: Node) => {
    pushHistory(); // Task 7.20: 删除节点前保存状态（可撤销恢复）
    setNodes((nds) => nds.filter((n) => n.id !== node.id));
    setEdges((eds) => eds.filter((e) => e.source !== node.id && e.target !== node.id));
    if (selectedNode?.id === node.id) setSelectedNode(null);
  };
  const deleteSelectedNode = () => {
    if (!selectedNode) return;
    deleteNode(selectedNode);
  };

  // ---- JSON 导入/导出 ----
  const exportJson = () => {
    setJsonText(JSON.stringify(flowNodesToGraph(nodes, edges), null, 2));
    setJsonPanel(true);
  };

  const importJson = () => {
    try {
      const g: WorkflowGraph = JSON.parse(jsonText);
      pushHistory(); // Task 7.20: JSON 导入前保存状态
      setNodes(g.nodes.map(graphNodeToFlowNode));
      setEdges(
        g.edges.map((e) => ({
          id: `${e.from}->${e.to}`,
          source: e.from,
          target: e.to,
          markerEnd: { type: MarkerType.ArrowClosed },
        })),
      );
      setStatusMsg(t("workflows.importOk"));
      setJsonPanel(false);
    } catch {
      setStatusMsg(t("workflows.importErr"));
    }
  };

  // ---- 保存 / 运行 ----
  const saveWorkflow = async () => {
    const graph = flowNodesToGraph(nodes, edges);
    if (graph.nodes.length === 0) {
      setStatusMsg(t("workflows.canvasEmpty"));
      return;
    }
    try {
      const result = await saveWorkflowGraph(api, {
        selectedId,
        isTemplate: loadedTemplate,
        name: getNameOrDefault(),
        graph,
      });
      setSelectedId(result.id);
      setLoadedTemplate(false);
      setStatusMsg(result.copiedFromTemplate
        ? t("workflows.savedAsCopy")
        : result.created ? t("workflows.saved") : t("workflows.updated"));
      loadWorkflows();
    } catch (e: unknown) {
      setStatusMsg(t("workflowEditor.saveError", { error: e instanceof Error ? e.message : String(e) }));
    }
  };

  const runWorkflow = async () => {
    if (!selectedId) {
      setStatusMsg(`WARN ${t("workflows.canvasEmpty")}`);
      return;
    }
    try {
      const result = await api<{ run_id: string }>(`/api/v1/workflows/${selectedId}/run`, {
        method: "POST",
        body: JSON.stringify({ initial_input: "" }),
      });
      setStatusMsg(t("workflowEditor.runStarted", { run_id: result.run_id }));
      setLastRunId(result.run_id);
      setShowTimeline(true);
    } catch (e: unknown) {
      setStatusMsg(t("workflowEditor.runError", { error: e instanceof Error ? e.message : String(e) }));
    }
  };

  const newWorkflow = () => {
    pushHistory(); // Task 7.20: 清空画布前保存状态（可撤销恢复）
    setSelectedId(null);
    setLoadedTemplate(false);
    setWorkflowName(""); // user fills name; save uses default below
    setNodes([]);
    setEdges([]);
    setSelectedNode(null);
    setStatusMsg(null);
  };
  const getNameOrDefault = () => workflowName.trim() || t("workflows.defaultName");

  if (!backendReady) return <div className="p-8 text-muted-foreground">{t("workflows.backendNotReady")}</div>;

  const selData = selectedNode ? dataOf(selectedNode) : null;

  return (
    <ReactFlowProvider>
      <div className="h-[calc(100vh-48px)] flex">
        {/* 左侧面板：三 Tab */}
        <aside className="w-60 shrink-0 border-r flex flex-col bg-background">
          {/* Tab 切换 */}
          <div className="flex border-b text-xs">
            {(["workflows", "templates"] as const).map((tabKey) => (
              <button
                key={tabKey}
                onClick={() => setSidebarTab(tabKey)}
                className={`flex-1 py-2 text-center transition-colors ${
                  sidebarTab === tabKey
                    ? "border-b-2 border-primary font-semibold text-primary"
                    : "text-muted-foreground hover:text-foreground"
                }`}
              >
                {{ workflows: t("workflows.workflows"), templates: t("workflows.templates") }[tabKey]}
              </button>
            ))}
          </div>

          {/* 工作流列表 */}
          {sidebarTab === "workflows" && (
            <div className="p-3 flex-1 overflow-y-auto">
              <div className="flex items-center justify-between mb-2">
                <span className="text-xs font-semibold text-muted-foreground">{t("workflows.myWorkflows")}</span>
                <button onClick={newWorkflow} className="text-xs px-2 py-0.5 border rounded hover:bg-secondary">
                  {t("workflows.newWorkflow")}
                </button>
              </div>
              {workflows.filter((w) => !w.is_template).length === 0 ? (
                <EmptyState icon={<Layers size={16} />} title={t("workflows.noWorkflows")} compact />
              ) : (
                <div className="space-y-0.5">
                  {workflows
                    .filter((w) => !w.is_template)
                    .map((w) => (
                      <div
                        key={w.id}
                        className={`group flex items-center rounded ${selectedId === w.id ? "bg-primary-muted" : "hover:bg-secondary"}`}
                      >
                        <button
                          onClick={() => loadToCanvas(w)}
                          className={`flex-1 text-left px-2 py-1 text-xs truncate ${selectedId === w.id ? "font-semibold" : ""}`}
                        >
                          {w.display_name || w.description || w.name}
                        </button>
                        <button
                          onClick={async (e) => {
                            e.stopPropagation();
                            const ok = await confirm({
                              title: t("common.confirm"),
                              description: t("workflows.deleteConfirm"),
                              variant: "destructive",
                            });
                            if (!ok) return;
                            try {
                              await api(`/api/v1/workflows/${w.id}`, { method: "DELETE" });
                              if (selectedId === w.id) {
                                setSelectedId(null);
                                setNodes([]);
                                setEdges([]);
                              }
                              loadWorkflows();
                              setStatusMsg(t("workflows.deleted"));
                            } catch {
                              setStatusMsg(t("workflows.deleteFailed"));
                            }
                          }}
                          className="opacity-0 group-hover:opacity-100 px-1.5 text-[10px] text-destructive hover:text-destructive/80 shrink-0"
                          title={t("legacy.git_2f4a")}
                        >
                          ×
                        </button>
                      </div>
                    ))}
                </div>
              )}
            </div>
          )}

          {/* 模板市场 */}
          {sidebarTab === "templates" && (
            <div className="p-3 flex-1 overflow-y-auto">
              <p className="text-xs text-muted-foreground mb-2">{t("workflows.clickToLoad")}</p>
              {templates.length === 0 ? (
                <EmptyState icon={<Layers size={16} />} title={t("workflows.noTemplates")} compact />
              ) : (
                <div className="space-y-1.5">
                  {templates.map((tmpl) => (
                    <button
                      key={tmpl.id}
                      onClick={() => loadToCanvas(tmpl)}
                      className="w-full text-left border rounded p-2 hover:bg-secondary transition-colors"
                    >
                      <div className="text-xs font-semibold">{tmpl.display_name || tmpl.name}</div>
                      <div className="text-[10px] text-muted-foreground mt-0.5">
                        {tmpl.description || t("workflows.clickToLoad")}
                      </div>
                    </button>
                  ))}
                </div>
              )}
            </div>
          )}
        </aside>

        {/* 主画布区 */}
        <main className="flex-1 flex flex-col min-w-0">
          <div className="h-12 border-b px-4 flex items-center gap-3 shrink-0">
            {/* Task 7.20: Undo/Redo 按钮——对标 n8n / ComfyUI 工具栏 */}
            <button
              onClick={undo}
              disabled={!canUndo}
              className="px-2 py-1 border rounded text-xs hover:bg-secondary disabled:opacity-40 disabled:cursor-not-allowed"
              title={`${t("workflows.undo")} (Ctrl+Z)`}
            >
              ↶ {t("workflows.undo")}
            </button>
            <button
              onClick={redo}
              disabled={!canRedo}
              className="px-2 py-1 border rounded text-xs hover:bg-secondary disabled:opacity-40 disabled:cursor-not-allowed"
              title={`${t("workflows.redo")} (Ctrl+Y)`}
            >
              ↷ {t("workflows.redo")}
            </button>
            <div className="w-px h-6 bg-border" />
            <input
              value={workflowName}
              onChange={(e) => setWorkflowName(e.target.value)}
              className="px-2 py-1 border rounded text-sm w-40"
              placeholder={t("workflows.defaultName")}
            />
            <button
              onClick={saveWorkflow}
              className="px-3 py-1 bg-primary text-primary-foreground rounded text-xs font-semibold"
            >
              {t("workflows.save")}
            </button>
            <button
              onClick={runWorkflow}
              className="px-3 py-1 border rounded text-xs font-semibold hover:bg-secondary"
              disabled={!selectedId}
            >
              {t("workflows.run")}
            </button>
            <WorkflowTriggersButton workflowId={selectedId} />
            <button
              onClick={async () => {
                if (!selectedId) return;
                setLogLoading(true);
                try {
                  const d = await api<{ runs: typeof runLogs }>(`/api/v1/workflows/${selectedId}/runs`);
                  setRunLogs(d.runs || []);
                } catch {
                  setRunLogs([]);
                }
                setLogLoading(false);
                setShowRunLogs(true);
              }}
              className="px-3 py-1 border rounded text-xs font-semibold hover:bg-secondary"
              disabled={!selectedId || logLoading}
            >
              {logLoading ? "…" : t("workflows.logs")}
            </button>
            <div className="w-px h-6 bg-border" />
            <button onClick={exportJson} className="px-3 py-1 border rounded text-xs hover:bg-secondary">
              {t("workflows.exportJson")}
            </button>
            <button
              onClick={() => {
                setJsonText("");
                setJsonPanel(true);
              }}
              className="px-3 py-1 border rounded text-xs hover:bg-secondary"
            >
              {t("workflows.importJson")}
            </button>
            {statusMsg && <span className="text-xs text-muted-foreground ml-auto">{statusMsg}</span>}
          </div>
          <div className="flex-1 relative">
            <ReactFlow
              nodes={nodes}
              edges={edges}
              onNodesChange={onNodesChange}
              onEdgesChange={onEdgesChange}
              onConnect={onConnect}
              onDragOver={onDragOver}
              onDrop={onDrop}
              onNodeClick={onNodeClick}
              onNodeDoubleClick={onNodeDoubleClick}
              onNodeContextMenu={onNodeContextMenu}
              onPaneClick={closeCtxMenu}
              onPaneContextMenu={onPaneContextMenu}
              onInit={(instance) => {
                flowRef.current = instance;
              }}
              fitView
            >
              <Background variant={BackgroundVariant.Dots} />
              <Controls />
              <MiniMap
                nodeColor={(n) => {
                  const t = dataOf(n).nodeType;
                  if (t === "human") return "var(--warning)";
                  if (t === "delegate_to_agent") return "var(--chart-8)";
                  if (t === "tool_execute") return "var(--chart-4)";
                  if (t === "ha_native") return "var(--chart-5)";
                  return "var(--primary)";
                }}
              />
            </ReactFlow>

            {/* 右键上下文菜单 */}
            {ctxMenu && (
              <>
                <div className="fixed inset-0 z-ctx" onClick={closeCtxMenu} />
                <div style={{ ...CTX_MENU_STYLE, left: ctxMenu.x, top: ctxMenu.y }}>
                  {ctxMenu.type === "node" && ctxMenu.node && (
                    <>
                      <button
                        className="w-full text-left px-3 py-1.5 hover:bg-secondary text-xs flex items-center gap-2"
                        onClick={() => {
                          initNodeEditor(ctxMenu.node!);
                          closeCtxMenu();
                        }}
                      >
                        {dataOf(ctxMenu.node!).nodeType === "tool_execute" ? t("workflows.editTool") : t("workflows.editPrompt")}
                      </button>
                      <button
                        className="w-full text-left px-3 py-1.5 hover:bg-secondary text-xs flex items-center gap-2 text-destructive"
                        onClick={() => {
                          closeCtxMenu();
                          deleteNode(ctxMenu.node!);
                        }}
                      >
                        {t("workflows.deleteNode")}
                      </button>
                      <div className="border-t border-border my-0.5" />
                    </>
                  )}
                  <button
                    className="w-full text-left px-3 py-1.5 hover:bg-secondary text-xs flex items-center gap-2"
                    onClick={() => addNodeAtContext("default")}
                  >
                    {" "}
                    {t("workflowEditor.addAgentNode")}
                  </button>
                  <button
                    className="w-full text-left px-3 py-1.5 hover:bg-secondary text-xs flex items-center gap-2"
                    onClick={() => addNodeAtContext("human")}
                  >
                    {" "}
                    {t("workflowEditor.addApprovalNode")}
                  </button>
                  {/* Phase 43 G2.1: OC 工具节点 */}
                  <div className="text-[9px] text-muted-foreground px-3 pb-0.5">{t("workflowEditor.ocToolSection")}</div>
                  <button
                    className="w-full text-left px-3 py-1.5 hover:bg-secondary text-xs flex items-center gap-2"
                    onClick={() => addNodeAtContext("oc_vision")}
                  >
                    {t("workflowEditor.ocVisionNode")}
                  </button>
                  <button
                    className="w-full text-left px-3 py-1.5 hover:bg-secondary text-xs flex items-center gap-2"
                    onClick={() => addNodeAtContext("oc_search")}
                  >
                    {t("workflowEditor.ocSearchNode")}
                  </button>
                  <button
                    className="w-full text-left px-3 py-1.5 hover:bg-secondary text-xs flex items-center gap-2"
                    onClick={() => addNodeAtContext("oc_messaging")}
                  >
                    {t("workflowEditor.ocMessagingNode")}
                  </button>
                  <div className="text-[9px] text-muted-foreground px-3 pb-0.5">{t("workflows.toolSection")}</div>
                  <button
                    className="w-full text-left px-3 py-1.5 hover:bg-secondary text-xs flex items-center gap-2"
                    onClick={() => addNodeAtContext("tool_execute")}
                  >
                    {t("workflows.addTool")}
                  </button>
                  <div className="text-[9px] text-muted-foreground px-3 pb-0.5">{t("workflows.haSection")}</div>
                  <button
                    className="w-full text-left px-3 py-1.5 hover:bg-secondary text-xs flex items-center gap-2"
                    onClick={() => addNodeAtContext("ha_native")}
                  >
                    {t("workflows.addHA")}
                  </button>
                  <div className="text-[9px] text-muted-foreground px-3 pb-0.5">{t("workflows.delegateSection")}</div>
                  <button
                    className="w-full text-left px-3 py-1.5 hover:bg-secondary text-xs flex items-center gap-2"
                    onClick={() => addNodeAtContext("delegate_to_agent")}
                  >
                    {t("workflows.delegateNode")}
                  </button>
                </div>
              </>
            )}
          </div>
        </main>

        {/* 节点编辑侧栏 */}
        {selectedNode && selData && (
          <aside className="w-72 shrink-0 border-l bg-background p-4 space-y-3">
            <h3 className="font-semibold text-sm">
              {t("workflows.editNode")}:{" "}
              {selData.nodeType === "human"
                ? "HITL"
                : selData.nodeType === "delegate_to_agent"
                  ? t("workflowEditor.nodeLabelDelegate")
                  : selData.nodeType === "tool_execute"
                    ? t("workflows.toolNode")
                    : selData.nodeType === "ha_native"
                      ? t("workflows.haNode")
                      : "Agent"}
            </h3>
            <div>
              <label className="text-xs text-muted-foreground">{t("workflows.nodeType")}</label>
              <div className="text-sm font-mono mt-0.5">{selData.nodeType ?? "default"}</div>
            </div>
            {selData.nodeType === "tool_execute" ? (
              <>
                <div>
                  <label className="text-xs text-muted-foreground">{t("workflows.toolName")}</label>
                  <input
                    value={nodeToolName}
                    onChange={(e) => setNodeToolName(e.target.value)}
                    className="w-full px-3 py-1.5 border rounded text-xs mt-1"
                    placeholder={t("workflows.toolName")}
                  />
                </div>
                <div>
                  <label className="text-xs text-muted-foreground">{t("workflows.toolArguments")}</label>
                  <textarea
                    value={nodeToolArguments}
                    onChange={(e) => setNodeToolArguments(e.target.value)}
                    rows={6}
                    className="w-full px-3 py-2 border rounded text-xs font-mono resize-y mt-1"
                    spellCheck={false}
                  />
                </div>
              </>
            ) : (
              <div>
                <label className="text-xs text-muted-foreground">{t("workflows.prompt")}</label>
                <textarea
                  value={nodePrompt}
                  onChange={(e) => setNodePrompt(e.target.value)}
                  rows={6}
                  className="w-full px-3 py-2 border rounded text-xs font-mono resize-y mt-1"
                  spellCheck={false}
                />
              </div>
            )}
            <div className="flex gap-2">
              <button
                onClick={updateNodeData}
                className="flex-1 px-3 py-1.5 bg-primary text-primary-foreground rounded text-xs"
              >
                {t("workflows.apply")}
              </button>
              <button
                onClick={() => setSelectedNode(null)}
                className="px-3 py-1.5 border rounded text-xs hover:bg-secondary"
              >
                {t("legacy.skill_625f")}
              </button>
            </div>
            <button
              onClick={deleteSelectedNode}
              className="w-full px-3 py-1.5 border border-destructive/30 rounded text-xs text-destructive hover:bg-destructive/10"
            >
              {t("workflows.deleteNode")}
            </button>
          </aside>
        )}

        {/* JSON 导入/导出面板 */}
        {jsonPanel && (
          <aside className="w-96 shrink-0 border-l bg-background p-4 space-y-3 overflow-y-auto">
            <h3 className="font-semibold text-sm">{t("workflows.workflowJson")}</h3>
            <textarea
              value={jsonText}
              onChange={(e) => setJsonText(e.target.value)}
              rows={20}
              className="w-full px-3 py-2 border rounded text-xs font-mono resize-y"
              spellCheck={false}
            />
            <div className="flex gap-2">
              <button
                onClick={importJson}
                className="flex-1 px-3 py-1.5 bg-primary text-primary-foreground rounded text-xs"
              >
                {t("workflows.importToCanvas")}
              </button>
              <button
                onClick={() => setJsonPanel(false)}
                className="px-3 py-1.5 border rounded text-xs hover:bg-secondary"
              >
                {t("common.close")}
              </button>
            </div>
          </aside>
        )}

        {/* P2: DAG 运行历史日志 */}
        <DetailDrawer open={showRunLogs} title={t("workflows.runLog")} onClose={() => setShowRunLogs(false)}>
          <WorkflowRunsList runs={runLogs} />
        </DetailDrawer>

        {/* Phase 7: 工作流执行时间线 */}
        <DetailDrawer
          open={showTimeline && !!lastRunId}
          title={t("workflows.workflowTimeline")}
          onClose={() => setShowTimeline(false)}
        >
          {lastRunId && <WorkflowTimeline runId={lastRunId} workflowId={selectedId || undefined} />}
        </DetailDrawer>
        {confirmDialog}
      </div>
    </ReactFlowProvider>
  );
}
