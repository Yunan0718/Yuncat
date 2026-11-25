/**
 * lib/trajectory.ts —— 把一条回复的零散状态投影成"轨迹条目"（2026-09-16）
 *
 * 为什么要这一层：一条回复现在散在五个字段里（chunks / thinkingChunks / toolCalls /
 * toolResults / planSteps），由四个组件各自解读、各自画框，于是套框、细线多、状态散。
 * 借鉴 deepseek-harness 的 `ui-trajectory`：先投影成**有序条目列表**，渲染层只遍历列表。
 *
 * 这一版是纯函数 + 单测，不改任何界面——先证明"投影是对的"，再动渲染（分步走，可回滚）。
 * 折叠状态、耗时、复制、失败高亮以后都只实现一遍；新增事件类型只加一个 kind。
 */
import { toolSummary } from "./toolViews";
import type { ToolArgs } from "./toolViews";

/** 文案函数（与 i18next 的 t 同形）；不传时用中文默认值，方便纯逻辑测试与脚本调用 */
export type TrajectoryTranslate = (key: string, options?: Record<string, unknown>) => string;

export type TrajectoryKind = "thinking" | "plan" | "tool" | "approval" | "message" | "error";
export type TrajectoryStatus = "running" | "done" | "failed";

export interface TrajectoryItem {
  id: string;
  kind: TrajectoryKind;
  /** 折叠行上的一行摘要（展开前用户看到的就是它） */
  title: string;
  /** 展开后的内容 */
  detail: string;
  status: TrajectoryStatus;
  /** 这一步花了多久（有起止时间时才有） */
  durationMs?: number;
  /** 正文不折叠，过程类（思考/计划/工具）折叠 */
  collapsible: boolean;
}

/** 计划步骤（结构对齐 store 的 PlanStep，但不 import，保持纯函数可测） */
export interface TrajectoryPlanStep {
  step: number;
  task: string;
  status?: string;
  error?: string;
  elapsedMs?: number;
}

/** 只依赖结构，不 import store——纯函数才好测 */
export interface TrajectoryInput {
  chunks?: string[];
  thinkingChunks?: string[];
  toolCalls?: Array<{ name?: string; args?: unknown; round?: number; startedAt?: number }>;
  toolResults?: Array<{ tool_name?: string; success?: boolean; preview?: string; fullResult?: string; startedAt?: number; finishedAt?: number }>;
  currentTool?: { name?: string; status?: string; preview?: string } | null;
  /** 工具实时输出（tool_name → 输出文本）——结果到达前显示这个 */
  toolLiveOutput?: Record<string, string>;
  /** AI 审批结论（审批方式 = ai 时后端推过来） */
  approvals?: Array<{ tool_name?: string; decision?: string; reason?: string }>;
  planSteps?: TrajectoryPlanStep[];
  status?: string;
  errorMessage?: string | null;
}

function formatArgs(args: unknown): string {
  if (args === undefined || args === null) return "";
  if (typeof args === "string") return args;
  try {
    return JSON.stringify(args, null, 2);
  } catch {
    // 降级：参数里有循环引用时不能整个条目挂掉，退回字符串形式
    return String(args);
  }
}

function describeStep(step: TrajectoryPlanStep): string {
  const mark = step.status === "done" ? "[x]" : step.status === "failed" ? "[!]" : "[ ]";
  const spend = step.elapsedMs ? ` (${(step.elapsedMs / 1000).toFixed(1)}s)` : "";
  const error = step.error ? ` — ${step.error}` : "";
  return `${mark} ${step.step + 1}. ${step.task}${spend}${error}`;
}

/** 起止都有且顺序正常才算耗时；缺一半或时钟回拨时返回 undefined（不显示假数字） */
function durationBetween(start?: number, end?: number): number | undefined {
  if (typeof start !== "number" || typeof end !== "number") return undefined;
  const span = end - start;
  return span >= 0 ? span : undefined;
}

function planStatus(steps: TrajectoryPlanStep[]): TrajectoryStatus {
  if (steps.some((step) => step.status === "failed")) return "failed";
  if (steps.length > 0 && steps.every((step) => step.status === "done")) return "done";
  return "running";
}

/** 按"用户看到的时间顺序"投影：思考 → 计划 → 工具 → 失败 → 正文 */
export function projectTrajectory(input: TrajectoryInput, t?: TrajectoryTranslate): TrajectoryItem[] {
  const text = (key: string, fallback: string, options?: Record<string, unknown>): string =>
    t ? t(key, options) : fallback;
  const items: TrajectoryItem[] = [];

  const thinking = (input.thinkingChunks || []).join("").trim();
  if (thinking) {
    items.push({
      id: "thinking", kind: "thinking", title: text("trajectory.thinking", "思考过程"), detail: thinking,
      status: "done", collapsible: true,
    });
  }

  const steps = input.planSteps || [];
  if (steps.length > 0) {
    const done = steps.filter((step) => step.status === "done").length;
    items.push({
      id: "plan",
      kind: "plan",
      title: text("trajectory.plan", `执行计划 ${done}/${steps.length}`, { done, total: steps.length }),
      detail: steps.map(describeStep).join("\n"),
      status: planStatus(steps), collapsible: true,
    });
  }

  const calls = input.toolCalls || [];
  const results = input.toolResults || [];
  calls.forEach((call, index) => {
    const name = call.name || "tool";
    const args = (call.args ?? {}) as ToolArgs;
    const summarized = toolSummary(name, args);
    const result = results[index];
    // 结果还没到时展示实时输出（命令 stdout/stderr 流式回传），到了之后以结果为准
    const live = input.toolLiveOutput?.[name] ?? "";
    const body = result ? (result.fullResult || result.preview) : live;
    const detail = [formatArgs(call.args), body]
      .filter((part) => !!part && String(part).trim().length > 0)
      .join("\n\n");
    items.push({
      id: `tool-${index}`,
      kind: "tool",
      title: summarized || name,
      detail,
      status: result ? (result.success === false ? "failed" : "done") : "running",
      durationMs: durationBetween(call.startedAt ?? result?.startedAt, result?.finishedAt),
      collapsible: true,
    });
  });

  // 外部 Agent（Hermes）在**服务端**执行工具：SSE 只给结果事件，没有 tool_call_chunk，
  // 于是 toolResults 会比 toolCalls 多。把多出来的结果也渲染成工具条目，
  // 否则外部 Agent 的“行动链”整段不可见（用户只看到一段最终回答）。
  results.slice(calls.length).forEach((result, offset) => {
    const index = calls.length + offset;
    const name = result.tool_name || "tool";
    items.push({
      id: `tool-result-${index}`,
      kind: "tool",
      title: name,
      detail: result.fullResult || result.preview || "",
      status: result.success === false ? "failed" : "done",
      durationMs: durationBetween(result.startedAt, result.finishedAt),
      collapsible: true,
    });
  });

  const running = input.currentTool;
  if (running?.name && running.status === "running" && calls.length === 0) {
    items.push({
      id: "tool-running", kind: "tool", title: running.preview || running.name,
      detail: input.toolLiveOutput?.[running.name] ?? "",
      status: "running", collapsible: true,
    });
  }

  for (const [index, entry] of (input.approvals || []).entries()) {
    const approved = entry.decision === "approve";
    const tool = entry.tool_name || "";
    items.push({
      id: `approval-${index}`,
      kind: "approval",
      title: approved
        ? text("trajectory.approvalApproved", `AI 审批放行 · ${tool}`, { tool })
        : text("trajectory.approvalDenied", `AI 审批否决 · ${tool}`, { tool }),
      detail: entry.reason || "",
      status: approved ? "done" : "failed",
      collapsible: Boolean(entry.reason),
    });
  }

  if (input.errorMessage) {
    items.push({
      id: "error", kind: "error", title: text("trajectory.error", "这次回复失败了"),
      detail: input.errorMessage, status: "failed", collapsible: false,
    });
  }

  const answer = (input.chunks || []).join("");
  if (answer.trim()) {
    items.push({
      id: "message", kind: "message", title: text("trajectory.message", "回复"), detail: answer,
      status: input.status === "running" ? "running" : "done", collapsible: false,
    });
  }

  return items;
}
