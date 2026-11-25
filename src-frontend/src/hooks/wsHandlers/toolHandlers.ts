import i18next from "i18next";
import { useTaskStore } from "../../store/taskStore";
import { emitGlobalToast } from "../../components/common/toast-context";

import type { Handler } from "./taskHandlers";

interface ToolCallChunk {
  index?: number;
  round?: number;
  function?: { name?: string; arguments?: string };
}

/* ── Phase 71: 工具起止时间戳——tool_start 时记录，tool_result_preview 时取出作为 startedAt ── */
const _toolStartAt = new Map<string, number>();
function toolStartKey(taskId: string, toolName: string): string {
  return `${taskId}:${toolName}`;
}

export const toolConfirmRequired: Handler = (msg, store) => {
  store.showConfirmDialog(
    msg.task_id,
    msg.tool_name ?? msg.toolName ?? "unknown",
    msg.preview ?? {},
    msg.confirm_token ?? "",
    // Batch 4b: 过期时间（epoch 秒）+ 来源任务摘要——弹窗倒计时/来源标识
    typeof msg.expires_at === "number" ? msg.expires_at : 0,
    typeof msg.task_prompt === "string" ? msg.task_prompt : "",
  );
};

// Batch 3.2: 同一轮多个 confirmable 工具聚合为批量确认弹窗
export const toolConfirmBatchRequired: Handler = (msg, store) => {
  const items = Array.isArray(msg.items) ? msg.items : [];
  store.showConfirmBatchDialog(
    msg.task_id,
    items.map((it: { tool_name?: string; preview?: unknown; confirm_token?: string; expires_at?: number }) => ({
      toolName: it.tool_name ?? "unknown",
      preview: (it.preview as Record<string, unknown>) ?? {},
      confirmToken: it.confirm_token ?? "",
      expiresAt: typeof it.expires_at === "number" ? it.expires_at : 0,
    })),
    typeof msg.task_prompt === "string" ? msg.task_prompt : "",
  );
};

// Batch 4b: 后端标记 token 过期——从确认弹窗队列移除，避免用户点确认收到 404
export const toolConfirmExpired: Handler = (msg, store) => {
  store.dropConfirmByToken(msg.confirm_token ?? "");
};

export const toolRound: Handler = (msg) => {
  useTaskStore.getState().setToolRound(msg.task_id, msg.round, msg.max);
};

export const toolCallChunk: Handler = (msg) => {
  const items = (Array.isArray(msg.data) ? msg.data : [msg.data]) as ToolCallChunk[];
  useTaskStore.getState().replaceToolCalls(
    msg.task_id,
    items
      .filter((tc) => tc?.function)
      .map((tc) => ({
        index: tc.index ?? 0,
        round: typeof tc.round === "number" ? tc.round : undefined,
        name: tc.function?.name || "",
        args: tc.function?.arguments || "",
      })),
  );
};

export const toolStart: Handler = (msg) => {
  useTaskStore.getState().setTaskToolStatus(msg.task_id, msg.tool_name, "running");
  // Phase 71: 记录工具开始时间戳，供 tool_result_preview 计算耗时
  _toolStartAt.set(toolStartKey(msg.task_id, msg.tool_name), Date.now());
};

export const toolDone: Handler = (msg) => {
  // 行动链修复：工具结束即清除 currentTool——结果已落到 toolResults 列表，无需残留"进行中"指示框
  useTaskStore.getState().clearCurrentTool(msg.task_id);
  // 实时输出到此为止——最终内容以 toolResults 为准，不留两份
  useTaskStore.getState().clearToolLiveOutput(msg.task_id, msg.tool_name || "run_command");
};

/**
 * 工具实时输出（后端 tool_output_chunk）。
 *
 * 后端从 Phase 61 起就在流式回传命令 stdout/stderr（run_command 边跑边推），
 * 但前端从来没有接过这个事件——事件被 useWebSocket 直接丢弃，
 * 于是"命令执行中能看到输出"这件事只存在于后端，界面上完全看不到。
 * 这里累积到 task.toolLiveOutput，由轨迹卡片的行动链条目滚动展示。
 */
export const toolOutputChunk: Handler = (msg, store) => {
  const text = typeof msg.data === "string" ? msg.data : "";
  if (!text) return;
  store.appendToolLiveOutput(msg.task_id, msg.tool_name || "run_command", text);
};

export const toolResultPreview: Handler = (msg) => {
  // Phase 71: 取出 tool_start 时记录的时间戳作为 startedAt
  const startedAt = _toolStartAt.get(toolStartKey(msg.task_id, msg.tool_name));
  useTaskStore
    .getState()
    .appendToolResult(msg.task_id, msg.tool_name, msg.success, msg.preview || "", msg.full_result || "", startedAt);
  _toolStartAt.delete(toolStartKey(msg.task_id, msg.tool_name));
  // 行动链修复：结果到达即清除 currentTool（兜底，防止仅收 result 未收 tool_done 时指示框留存）
  useTaskStore.getState().clearCurrentTool(msg.task_id);
  // P0-3 (2026-07-04): 检查点失败时展示红色 toast 通知用户"本次修改无法撤销"
  const _ckWarning = msg.meta?.checkpoint_warning;
  if (typeof _ckWarning === "string" && _ckWarning) {
    emitGlobalToast(i18next.t("common.checkpointFailed", { defaultValue: "检查点创建失败，本次修改无法撤销" }), "error");
  }
};

export const approvalReview: Handler = (msg, store) => {
  // AI 审批结论（审批方式 = ai）：追到轨迹里，用户能看到谁把这次调用放了或拦了
  const taskId = (msg.task_id as string) || "";
  if (!taskId) return;
  store.appendApproval(taskId, {
    tool_name: (msg.tool_name as string) || "",
    decision: (msg.decision as string) || "",
    reason: (msg.reason as string) || "",
  });
};

export const toolFailureAnalysis: Handler = (msg) => {
  const startedAt = _toolStartAt.get(toolStartKey(msg.task_id, msg.tool_name));
  useTaskStore.getState().appendToolResult(msg.task_id, msg.tool_name, false, i18next.t("task.analysisResult", { suggestion: msg.suggestion || "" }), undefined, startedAt);
  _toolStartAt.delete(toolStartKey(msg.task_id, msg.tool_name));
  useTaskStore.getState().clearCurrentTool(msg.task_id);
};

export const modelDegraded: Handler = (msg, store) => {
  store.appendToolResult((msg.task_id as string) || "", "model_degraded", false, i18next.t("task.modelDegraded"));
};

export const stopHooksFailed: Handler = (msg, store) => {
  store.appendToolResult((msg.task_id as string) || "", "stop_hooks", false, (msg.summary as string) || "");
};

// Phase 49-F4: tool_degraded——工具连续失败超过阈值，后端标记降级。
// 对标 model_degraded handler，追加到工具结果列表让用户看到。
export const toolDegraded: Handler = (msg, store) => {
  const toolName = msg.tool_name ?? "unknown";
  const failures = msg.failures ?? 0;
  store.appendToolResult(
    (msg.task_id as string) || "",
    toolName,
    false,
    i18next.t("task.toolDegraded", { tool: toolName, count: failures }),
  );
};

// Phase 49-F4: tool_confirm_error——工具确认处理失败（如 token 过期/不存在）。
// HTTP 响应已处理弹窗关闭——此 WS 消息是异步错误通知，只需 toast 提示用户。
export const toolConfirmError: Handler = (msg) => {
  emitGlobalToast(
    i18next.t("task.toolConfirmError", { error: msg.error ?? "" }),
    "error",
  );
};

/**
 * 标准查询（standards_lookup）的结果——后端专门推来让前端实时看到。
 * 没有这个 handler 时，查询建议只进了下一轮的模型上下文，界面上完全看不到。
 */
export const standardsLookupResult: Handler = (msg) => {
  const summary = typeof msg.summary === "string" ? msg.summary : "";
  const recs = Array.isArray(msg.recommendations) ? msg.recommendations : [];
  const body = summary || recs.slice(0, 3).join("；");
  if (!body) return;
  emitGlobalToast(
    i18next.t("chat.standardsLookupResult", {
      summary: body,
      defaultValue: `标准查询：${body}`,
    }),
    "info",
  );
};
