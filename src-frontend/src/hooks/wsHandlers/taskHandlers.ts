import i18next from "i18next";
import { useTaskStore } from "../../store/taskStore";
import type { TaskStore, ContextBudgets } from "../../types/task";
import { emitGlobalToast } from "../../components/common/toast-context";

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export type Handler = (msg: Record<string, any>, store: TaskStore) => void;

/* ── 共享通知去重 ── */
const _notified = new Set<string>();
export const getNotifiedSet = () => _notified;

function tryNotify(taskId: string, status: "completed" | "failed") {
  // 仅窗口不可见时弹桌面通知（可见时 UI 已有反馈，避免打扰）。
  // 与 useNotification hook 共享 _notified 去重，同一 task 只通知一次。
  if (!document.hidden) return;
  if (_notified.has(taskId)) return;
  const now = Date.now();
  const t = useTaskStore.getState().tasks[taskId];
  if (!t || !t.startedAt) return;
  // 运行不足 60s 的短任务不弹通知，避免瞬时任务刷屏
  if ((now - t.startedAt) / 1000 < 60) return;
  _notified.add(taskId);
  const prompt = (t.prompt || "").slice(0, 80).replace(/\n/g, " ");
  const title = status === "completed" ? i18next.t("task.completed") : i18next.t("task.failed");
  const body = `${prompt}${prompt.length > 78 ? "…" : ""}`;
  // 携带 link，点击系统通知可导航回对应任务
  window.yuncat?.showNotification?.(title, body, `/chat?task=${taskId}`)?.catch(() => {});
}

export const taskCreated: Handler = (msg, store) => {
  store.createTask(
    msg.task_id,
    msg.route_info?.agent_id ?? "unknown",
    msg.prompt ?? "",
    msg.route_info?.reason ?? "",
    msg.session_id,
  );
  // Phase 82: 长任务标记（Tier 3 long_task_confirm 创建的 task 带 long_task:true）
  if (msg.long_task === true) {
    useTaskStore.getState().setTaskLongTask(msg.task_id);
  }
  store.bumpSessionList();
};

export const streamChunk: Handler = (msg, store) => {
  store.appendChunk(msg.task_id, msg.data ?? msg.chunk ?? "");
};

export const replayMessages: Handler = (msg, store) => {
  // Task 6.9: WS 重连后恢复流式上下文——后端从 conversations 表推送最近 N 轮历史，
  // 前端 diff 渲染（已渲染的不重复）。对标 Claude Code 重连后全量状态同步。
  const taskId = msg.task_id;
  const task = store.tasks[taskId];
  if (!task) return;

  const messages = Array.isArray(msg.messages) ? msg.messages : [];

  // diff 基准：已有 chunks 拼接成完整文本，用 substring 检查是否已渲染
  const existingText = (task.chunks || []).join("");
  const existingThinking = (task.thinkingChunks || []).join("");

  let appended = false;
  for (const m of messages) {
    const role = String(m.role || "").toLowerCase();
    const content = typeof m.content === "string" ? m.content : "";
    const reasoning = typeof m.reasoning_content === "string" ? m.reasoning_content : "";

    // 只 replay assistant 内容（user 消息由 prompt 单独展示，避免重复）
    if (role === "assistant" && content && !existingText.includes(content)) {
      useTaskStore.getState().appendChunk(taskId, content);
      appended = true;
    }
    // reasoning_content 同样 diff
    if (role === "assistant" && reasoning && !existingThinking.includes(reasoning)) {
      useTaskStore.setState((s) => ({
        tasks: {
          ...s.tasks,
          [taskId]: {
            ...s.tasks[taskId],
            thinkingChunks: [...(s.tasks[taskId].thinkingChunks || []), reasoning],
          },
        },
      }));
    }
  }

  // 若追加了内容，触发 session list 刷新（与 streamChunk 行为对齐）
  if (appended) {
    store.bumpSessionList();
  }
};

export const streamDone: Handler = (msg, store) => {
  store.setTaskDone(msg.task_id);
  tryNotify(msg.task_id, "completed");
  store.bumpSessionList();
};

export const error: Handler = (msg, store) => {
  store.setTaskError(msg.task_id, msg.error ?? msg.message ?? "Unknown error");
  tryNotify(msg.task_id, "failed");
};

export const routeInfo: Handler = (msg, store) => {
  const t = store.tasks[msg.task_id];
  if (t)
    useTaskStore.setState({
      tasks: { ...store.tasks, [msg.task_id]: { ...t, routeReason: msg.reason ?? t.routeReason } },
    });
};

export const taskRouted: Handler = (msg) => {
  useTaskStore.getState().setTaskRoute(msg.task_id, msg.agent_id ?? "hermes", msg.route_reason ?? "");
};

export const taskCancelled: Handler = (msg, store) => {
  store.setTaskCancelled(msg.task_id);
};

export const taskPaused: Handler = (msg, store) => {
  store.setTaskPaused(msg.task_id);
};

export const taskResumed: Handler = (msg, store) => {
  store.setTaskResumed(msg.task_id);
};

export const taskInterrupted: Handler = (msg, store) => {
  // G4: 优雅关闭/强杀中断——可恢复（区别于 cancelled 终态）
  store.setTaskInterrupted(msg.task_id);
};

// Phase 49-F1: task_failed——任务多次崩溃恢复失败后后端标记 failed。
// 不走 error 消息（那是通用错误），task_failed 是明确的终态通知。
export const taskFailed: Handler = (msg, store) => {
  store.setTaskError(msg.task_id, msg.error ?? "Task failed");
  tryNotify(msg.task_id, "failed");
};

// Phase 49-F2: budget_exceeded——token 预算耗尽，后端标记 paused_budget。
// 前端需要将任务显示为暂停（区别于正常 paused），并通知用户原因。
export const budgetExceeded: Handler = (msg, store) => {
  store.setTaskPaused(msg.task_id);
  emitGlobalToast(
    i18next.t("task.budgetExceeded", {
      used: msg.used_tokens ?? 0,
      max: msg.max_budget_tokens ?? 0,
    }),
    "error",
  );
};

// Phase 49-F3: stream_error——流式响应中途出错（如 Provider 断连）。
// 后端随后会 _mark_task("failed")，但不发 task_failed 消息——前端需自行标记。
export const streamError: Handler = (msg, store) => {
  // error_code 是给用户看下一步用的（AUTH → 去设置里换密钥），不能丢
  store.setTaskError(msg.task_id, msg.error ?? "Stream error", msg.error_code ?? undefined);
  tryNotify(msg.task_id, "failed");
};

export const taskProgress: Handler = (msg, store) => {
  store.setTaskProgress(msg.task_id, msg.step ?? msg.current, msg.total);
};

export const taskResultType: Handler = (msg, store) => {
  store.setTaskResultType(msg.task_id, msg.result_type ?? msg.resultType);
};

export const workflowHitl: Handler = (msg, store) => {
  store.showConfirmDialog(msg.run_id ?? msg.task_id, "hitl_approval", {
    node_id: msg.node_id,
    prompt: msg.prompt,
    timeout: msg.timeout,
    actions: msg.actions ?? ["approve", "reject"],
  });
};

export const tokenUsage: Handler = (msg) => {
  const ts = useTaskStore.getState().tasks[msg.task_id];
  // 2026-09-16: 顺手用真实用量刷新上下文占用——这是全局唯一可信的「上下文满了多少」来源。
  const promptTokens = Number(msg.prompt_tokens) || 0;
  if (promptTokens > 0) {
    const windowSize = useTaskStore.getState().contextUsage.window || 131072;
    useTaskStore
      .getState()
      .setContextUsage(Math.min(100, (promptTokens / windowSize) * 100), promptTokens, windowSize);
  }
  if (ts)
    useTaskStore.setState({
      tasks: {
        ...useTaskStore.getState().tasks,
        [msg.task_id]: {
          ...ts,
          tokenUsage: {
            promptTokens: msg.prompt_tokens || 0,
            completionTokens: msg.completion_tokens || 0,
            cacheHitTokens: msg.cache_hit_tokens || 0,
            cacheMissTokens: msg.cache_miss_tokens || 0,
            elapsedMs: msg.elapsed_ms || 0,
          },
        },
      },
    });
};

export const msgNumbered: Handler = (msg) => {
  useTaskStore
    .getState()
    .setMsgNumbered(msg.task_id, msg.seq, msg.role, msg.msg_id, msg.reply_to, msg.reply_to_seq);
};

export const compactResult: Handler = (msg, store) => {
  store.bumpSessionList();
};

export const contextStats: Handler = (msg) => {
  // Phase 56-P2: /context /tokens 结果渲染为系统提示
  // P2-2.5: 同时持久化动态预算到 store——供前端截断点消费
  if (msg.budgets) {
    useTaskStore.getState().setContextBudgets(msg.budgets as ContextBudgets);
  }
  const activeId = useTaskStore.getState().activeTaskId;
  if (activeId && msg.error) {
    useTaskStore.getState().appendChunk(activeId, `\n\n${i18next.t("contextStats.titleError", { error: msg.error })}`);
  } else if (activeId) {
    const lines = [
      `\n\n${i18next.t("contextStats.title")}`,
      i18next.t("contextStats.model", { model: msg.model || "unknown" }),
      i18next.t("contextStats.window", { window: msg.context_window?.toLocaleString?.() || msg.context_window }),
      i18next.t("contextStats.estimatedUsage", { tokens: msg.estimated_tokens?.toLocaleString?.() || msg.estimated_tokens, percent: msg.usage_percent }),
      msg.last_real_prompt_tokens !== undefined
        ? i18next.t("contextStats.lastRealPrompt", { tokens: msg.last_real_prompt_tokens.toLocaleString?.() || msg.last_real_prompt_tokens })
        : "",
      msg.last_real_completion_tokens !== undefined
        ? i18next.t("contextStats.lastRealCompletion", { tokens: msg.last_real_completion_tokens.toLocaleString?.() || msg.last_real_completion_tokens })
        : "",
      i18next.t("contextStats.messageCount", { count: msg.message_count }),
      i18next.t("contextStats.dataSource", { source: msg.source }),
      "",
      i18next.t("contextStats.thresholdsTitle"),
      i18next.t("contextStats.microCompact", { percent: msg.thresholds?.micro_compact }),
      i18next.t("contextStats.standardCompact", { percent: msg.thresholds?.standard_compact }),
      i18next.t("contextStats.emergencyTrim", { percent: msg.thresholds?.emergency_trim }),
      "",
      i18next.t("contextStats.budgetsTitle"),
      i18next.t("contextStats.toolResultLimit", { chars: msg.budgets?.tool_result_limit?.toLocaleString?.() }),
      i18next.t("contextStats.historyLimit", { limit: msg.budgets?.history_limit }),
      i18next.t("contextStats.ftsMemories", { count: msg.budgets?.fts_memories }),
      i18next.t("contextStats.vectorMemories", { count: msg.budgets?.vector_memories }),
      i18next.t("contextStats.skillPreview", { chars: msg.budgets?.skill_preview_chars?.toLocaleString?.() }),
      i18next.t("contextStats.skillTotal", { chars: msg.budgets?.skill_total_chars?.toLocaleString?.() }),
      i18next.t("contextStats.microCompactThreshold", { chars: msg.budgets?.micro_compact_threshold?.toLocaleString?.() }),
    ];
    useTaskStore.getState().appendChunk(activeId, lines.join("\n"));
  }
};

/**
 * 后端推的任务状态（spec 循环审计等路径会改状态但不走 task_done）。
 * 没有这个 handler 时，那条路径下任务状态在界面上不更新。
 */
export const taskStatus: Handler = (msg, store) => {
  const status = typeof msg.status === "string" ? msg.status : "";
  if (!status) return;
  store.setTaskStatus(msg.task_id, status);
};
