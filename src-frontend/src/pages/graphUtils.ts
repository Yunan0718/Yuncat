import { type Node, type Edge } from "@xyflow/react";
import i18next from "i18next";

// ---- 类型 ----
export interface NodeData {
  label: string;
  prompt: string;
  nodeType: string;
  model?: string;
  tool_name?: string;
  arguments?: Record<string, unknown>;
}

export interface WorkflowGraph {
  nodes: Array<{ id: string; type: string; prompt: string; model?: string; tool_name?: string; arguments?: Record<string, unknown> }>;
  edges: Array<{ from: string; to: string }>;
}

/** 安全获取节点 data 的类型化视图 */
export function dataOf(node: Node): NodeData {
  return node.data as unknown as NodeData;
}

/** 从 graph 节点创建 ReactFlow Node */
export function graphNodeToFlowNode(n: { id: string; type: string; prompt: string; model?: string; tool_name?: string; arguments?: Record<string, unknown> }): Node {
  const isHuman = n.type === "human";
  const isDelegate = n.type === "delegate_to_agent";
  const isTool = n.type === "tool_execute";
  const isHA = n.type === "ha_native";
  const typeColor = isHuman ? "var(--warning)" : isDelegate ? "var(--chart-8)" : isTool ? "var(--chart-4)" : isHA ? "var(--chart-5)" : "var(--primary)";
  const typeBg = isHuman
    ? "color-mix(in srgb, var(--warning) 8%, transparent)"
    : isDelegate
      ? "color-mix(in srgb, var(--chart-8) 6%, transparent)"
      : isTool
        ? "color-mix(in srgb, var(--chart-4) 8%, transparent)"
        : isHA
          ? "color-mix(in srgb, var(--chart-5) 8%, transparent)"
          : "color-mix(in srgb, var(--primary) 6%, transparent)";
  return {
    id: n.id,
    type: "default",
    position: { x: Math.random() * 400, y: Math.random() * 300 },
    data: {
      label: isTool ? n.tool_name || "tool" : n.prompt.slice(0, 60),
      prompt: n.prompt,
      nodeType: n.type,
      model: n.model,
      tool_name: n.tool_name,
      arguments: n.arguments,
    },
    style: {
      background: typeBg,
      border: `2px solid ${typeColor}`,
      borderRadius: "8px",
      padding: "10px",
      width: 220,
    },
  };
}

/** 创建新的 ReactFlow 节点 */
export function createFlowNode(id: string, nodeType: string, pos: { x: number; y: number }): Node {
  const isHuman = nodeType === "human";
  const isDelegate = nodeType === "delegate_to_agent";
  const isTool = nodeType === "tool_execute";
  const isHA = nodeType === "ha_native";
  // P0 (2026-07-04): 节点默认 label 是创建时的 seed 值（存入 node.data，不再随语言切换变化），
  // 因此用 i18next.t() 全局实例而非 useTranslation 的 t——避免给 createFlowNode 增加 t 参数
  // 传染所有调用点（onDrop useCallback 依赖等）。
  const label = isHuman ? "HITL" : isDelegate ? i18next.t("workflowEditor.nodeLabelDelegate") : isTool ? "tool" : isHA ? "HA" : "new node";
  const prompt = isHuman ? "waiting for approval" : isDelegate ? "delegate task to agent" : isTool ? "tool execute" : isHA ? "HA native task" : "enter prompt...";
  const bg = isHuman
    ? "color-mix(in srgb, var(--warning) 8%, transparent)"
    : isDelegate
      ? "color-mix(in srgb, var(--chart-8) 6%, transparent)"
      : isTool
        ? "color-mix(in srgb, var(--chart-4) 8%, transparent)"
        : isHA
          ? "color-mix(in srgb, var(--chart-5) 8%, transparent)"
          : "color-mix(in srgb, var(--primary) 6%, transparent)";
  const border = isHuman ? "var(--warning)" : isDelegate ? "var(--chart-8)" : isTool ? "var(--chart-4)" : isHA ? "var(--chart-5)" : "var(--primary)";
  const data: Record<string, unknown> = { label, prompt, nodeType };
  if (isTool) {
    data.tool_name = "";
    data.arguments = {};
  }
  return {
    id,
    type: "default",
    position: pos,
    data,
    style: { background: bg, border: `2px solid ${border}`, borderRadius: "8px", padding: "10px", width: 200 },
  };
}

/** 画布上的节点 → graph 格式 */
export function flowNodesToGraph(nodes: Node[], edges: Edge[]): WorkflowGraph {
  return {
    nodes: nodes.map((n) => {
      const d = dataOf(n);
      const base: Record<string, unknown> = { id: n.id, type: d.nodeType ?? "default", prompt: d.prompt ?? "" };
      if (d.model) base.model = d.model;
      if (d.tool_name) base.tool_name = d.tool_name;
      if (d.arguments) base.arguments = d.arguments;
      return base as { id: string; type: string; prompt: string; model?: string; tool_name?: string; arguments?: Record<string, unknown> };
    }),
    edges: edges.map((e) => ({ from: e.source, to: e.target })),
  };
}
