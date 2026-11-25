import { create } from "zustand";

// 6c-2 修复：类型定义已提取到 types/task.ts——独立消费且避免 zustand 运行时耦合
// TaskStore 用于 create<TaskStore>；PlanStep/TaskState 用于实现体内的 Partial<T> 类型注解
import type { TaskStore, PlanStep, TaskState, ConfirmDialog, TodoItem } from "../types/task";
// re-export 保持现有 import 路径兼容（消费者无需改动）
export type {
  StreamChunk,
  ClarificationQuestion,
  ContextBudgets,
  TaskState,
  PlanStep,
  PlanState,
  SessionSummary,
  ConfirmDialog,
  BridgeApproval,
  LongTaskSuggestion,
  PlanTreeNode,
  ReviewLogEntry,
  SupervisorView,
  UserQuestion,
  TodoItem,
  TaskStore,
} from "../types/task";

export const useTaskStore = create<TaskStore>((set, get) => ({
  tasks: {},
  userQuestions: [],
  enqueueUserQuestion: (question) => set((state) => (
    state.userQuestions.some((item) => item.token === question.token)
      ? state
      : { userQuestions: [...state.userQuestions, question] }
  )),
  removeUserQuestion: (token) => set((state) => ({
    userQuestions: state.userQuestions.filter((item) => item.token !== token),
  })),
  clearUserQuestionsForTask: (taskId) => set((state) => ({
    userQuestions: state.userQuestions.filter((item) => item.task_id !== taskId),
  })),
  activeTaskId: null,
  confirmDialog: null,
  confirmQueue: [],
  bridgeApproval: null,
  setBridgeApproval: (approval) => set({ bridgeApproval: approval }),
  longTaskSuggestion: null,
  setLongTaskSuggestion: (suggestion) => set({ longTaskSuggestion: suggestion }),
  planSteps: [],
  planActiveId: null,
  currentSessionId: null,
  sessions: [],
  sessionTodos: [],
  setSessionTodos: (todos: TodoItem[]) => set({ sessionTodos: todos }),
  deliverablesVersion: 0,
  bumpDeliverables: () => set((s) => ({ deliverablesVersion: s.deliverablesVersion + 1 })),
  sessionListVersion: 0,
  // Phase 82: Supervisor 状态
  supervisorViews: {},
  supervisorActiveTaskId: null,

  createTask: (taskId, agentId, prompt, reason, convId) =>
    set((s) => ({
      tasks: {
        ...s.tasks,
        [taskId]: {
          taskId,
          agentId,
          prompt,
          status: "running",
          chunks: [],
          thinkingChunks: [],
          planAuto: false,
          planDone: 0,
          planTotal: 0,
          planRatio: 0,
          toolCalls: [],
          toolResults: [],
          approvals: [],
          routeReason: reason,
          errorMessage: null,
          startedAt: Date.now(),
          convId,
          isStreaming: true, // E4: 新任务开始流式——stream_done 后设 false
        },
      },
      activeTaskId: taskId,
    })),

  appendChunk: (taskId, chunk) =>
    set((s) => {
      const t = s.tasks[taskId];
      if (!t) return s;
      return {
        tasks: {
          ...s.tasks,
          [taskId]: { ...t, chunks: [...t.chunks, chunk] },
        },
      };
    }),

  setStreaming: (taskId, streaming) =>
    set((s) => {
      const t = s.tasks[taskId];
      if (!t || t.isStreaming === streaming) return s;
      return {
        tasks: {
          ...s.tasks,
          [taskId]: { ...t, isStreaming: streaming },
        },
      };
    }),

  appendThinking: (taskId, data) =>
    set((s) => {
      const t = s.tasks[taskId];
      if (!t) return s;
      return {
        tasks: {
          ...s.tasks,
          [taskId]: { ...t, thinkingChunks: [...t.thinkingChunks, data] },
        },
      };
    }),

  // 后端已累积合并——直接替换整个 tool calls 列表
  replaceToolCalls: (taskId, calls) =>
    set((s) => {
      const t = s.tasks[taskId];
      if (!t) return s;
      return { tasks: { ...s.tasks, [taskId]: { ...t, toolCalls: calls } } };
    }),

  appendToolCall: (taskId, call, _index) =>
    set((s) => {
      const t = s.tasks[taskId];
      if (!t) return s;
      const existing = [...t.toolCalls];
      const existingIdx = existing.findIndex((c) => c.index === call.index);
      if (existingIdx >= 0) {
        const existing_entry = existing[existingIdx];
        existing[existingIdx] = {
          ...existing_entry,
          name: call.name || existing_entry.name,
          args: call.args ? String(existing_entry.args || "") + String(call.args || "") : existing_entry.args,
        };
      } else {
        existing.push({ ...call, index: call.index ?? existing.length, round: t.toolRound , startedAt: Date.now() });
      }
      return { tasks: { ...s.tasks, [taskId]: { ...t, toolCalls: existing } } };
    }),

  appendApproval: (taskId, entry) =>
    set((s) => {
      const t = s.tasks[taskId];
      if (!t) return s;
      return {
        tasks: {
          ...s.tasks,
          [taskId]: { ...t, approvals: [...(t.approvals || []), entry] },
        },
      };
    }),

  appendToolResult: (taskId, toolName, success, preview, fullResult, startedAtFromEvent) =>
    set((s) => {
      const t = s.tasks[taskId];
      if (!t) return s;
      // 工具耗时只有一个真源：tool_start 事件时间戳；事件缺失时退化为
      // 与第 N 个工具调用配对，仍对不上就只用当前时刻。
      const callIndex = (t.toolResults || []).length;
      const startedAt = startedAtFromEvent ?? (t.toolCalls || [])[callIndex]?.startedAt ?? Date.now();
      return {
        tasks: {
          ...s.tasks,
          [taskId]: {
            ...t,
            toolResults: [
              ...(t.toolResults || []),
              {
                tool_name: toolName,
                success,
                preview: preview.slice(0, 300),
                // P2-2.5: fullResult 上限跟随动态预算（后端 context_stats 推送），fallback 4000
                fullResult: (fullResult || "").slice(0, s.contextUsage.budgets?.tool_result_limit || 4000),
                // Phase 71: 工具耗时——startedAt 取值见上方，finishedAt 为结果到达时刻
                startedAt: startedAt,
                finishedAt: Date.now(),
              },
            ],
          },
        },
      };
    }),

  setToolRound: (taskId, round, _max) =>
    set((s) => {
      const t = s.tasks[taskId];
      if (!t) return s;
      return { tasks: { ...s.tasks, [taskId]: { ...t, toolRound: round } } };
    }),

  // Phase 43 R1-R2: 路由决策推送到前端
  setTaskRoute: (taskId, agentId, routeReason) =>
    set((s) => {
      const t = s.tasks[taskId];
      if (!t) return s;
      return { tasks: { ...s.tasks, [taskId]: { ...t, agentId, routeReason } } };
    }),

  // 后端 task_status 事件：状态同步的**通用入口**。
  // 只认已知状态值——后端要是发了别的字符串，宁可不动状态也不要写进脏值。
  setTaskStatus: (taskId, status) =>
    set((s) => {
      const t = s.tasks[taskId];
      if (!t) return s;
      const known = ["running", "completed", "failed", "cancelled", "paused", "interrupted"];
      if (!known.includes(status)) return s;
      return {
        tasks: {
          ...s.tasks,
          [taskId]: {
            ...t,
            status: status as typeof t.status,
            isStreaming: status === "running" ? t.isStreaming : false,
          },
        },
      };
    }),

  setTaskDone: (taskId) =>
    set((s) => {
      const t = s.tasks[taskId];
      if (!t) return s;
      // Phase 6 guard: don't overwrite cancelled status
      // G4: also preserve interrupted（优雅关闭后迟到的 stream_done 不应覆盖）
      // 2026-09-16: 也要保留 failed——后端在 stream_error 之后仍会发 stream_done
      // （表示"流结束了"），旧逻辑会把失败改成"已完成"，界面上就只剩一个空气泡，
      // 用户既看不到原因也看不到重试按钮（实测踩到）。
      if (t.status === "cancelled" || t.status === "interrupted" || t.status === "failed") return s;
      return {
        tasks: {
          ...s.tasks,
          [taskId]: { ...t, status: "completed", isStreaming: false },
        },
        activeTaskId: s.activeTaskId === taskId ? null : s.activeTaskId,
      };
    }),

  setTaskError: (taskId, error, errorCode) =>
    set((s) => {
      const t = s.tasks[taskId];
      if (!t) return s;
      // Guard: don't overwrite final states
      // G4: also preserve interrupted（迟到的 error 不应覆盖中断状态）
      if (t.status === "cancelled" || t.status === "interrupted") return s;
      return {
        tasks: {
          ...s.tasks,
          [taskId]: { ...t, status: "failed", errorMessage: error, errorCode, isStreaming: false },
        },
        activeTaskId: s.activeTaskId === taskId ? null : s.activeTaskId,
      };
    }),

  setActiveTask: (taskId) => set({ activeTaskId: taskId }),

  // ---- Phase 6 新增 ----
  setTaskCancelled: (taskId) =>
    set((s) => {
      const t = s.tasks[taskId];
      if (!t) return s;
      // Only cancel running tasks; preserve completed/failed
      if (t.status !== "running") return s;
      return {
        tasks: {
          ...s.tasks,
          [taskId]: { ...t, status: "cancelled", isStreaming: false },
        },
        activeTaskId: s.activeTaskId === taskId ? null : s.activeTaskId,
      };
    }),

  // Phase 48-H 维度 6: Pause/Resume 任务状态——对标 Claude Code
  setTaskPaused: (taskId) =>
    set((s) => {
      const t = s.tasks[taskId];
      if (!t || t.status !== "running") return s;
      return { tasks: { ...s.tasks, [taskId]: { ...t, status: "paused" as const, isStreaming: false } } };
    }),

  setTaskResumed: (taskId) =>
    set((s) => {
      const t = s.tasks[taskId];
      // G4: resume 接受 paused 和 interrupted（均可恢复状态）
      if (!t || (t.status !== "paused" && t.status !== "interrupted")) return s;
      return { tasks: { ...s.tasks, [taskId]: { ...t, status: "running" as const, isStreaming: true } } };
    }),

  // G4: 中断任务——running → interrupted（优雅关闭/强杀，可恢复）
  setTaskInterrupted: (taskId) =>
    set((s) => {
      const t = s.tasks[taskId];
      // 仅 running 可被中断；保留终态（completed/failed/cancelled）与已中断
      if (!t || t.status !== "running") return s;
      return { tasks: { ...s.tasks, [taskId]: { ...t, status: "interrupted" as const, isStreaming: false } } };
    }),

  setTaskProgress: (taskId, step, total) =>
    set((s) => {
      const t = s.tasks[taskId];
      if (!t) return s;
      return {
        tasks: {
          ...s.tasks,
          [taskId]: { ...t, progress: { step, total } },
        },
      };
    }),

  setTaskResultType: (taskId, resultType) =>
    set((s) => {
      const t = s.tasks[taskId];
      if (!t) return s;
      return { tasks: { ...s.tasks, [taskId]: { ...t, resultType } } };
    }),

  setTaskToolStatus: (taskId, name, status, preview) =>
    set((s) => {
      const t = s.tasks[taskId];
      if (!t) return s;
      return { tasks: { ...s.tasks, [taskId]: { ...t, currentTool: { name, status, preview } } } };
    }),

  // 行动链修复：工具完成后清除 currentTool，避免"进行中"指示框残留在已完成工具上
  // （原实现仅在 tool_start/tool_done 写入，tool_result_preview 只追加结果，从不清除，
  //  导致已完成工具的脉冲指示框永久停留，并与结果列表中的已完成条目重复显示）
  clearCurrentTool: (taskId) =>
    set((s) => {
      const t = s.tasks[taskId];
      if (!t || !t.currentTool) return s;
      return { tasks: { ...s.tasks, [taskId]: { ...t, currentTool: undefined } } };
    }),

  // 工具实时输出：命令执行期间后端持续推 tool_output_chunk，前端在这里累积。
  // 上限 8k 字符（保留最新的一段）——命令输出可能很大，无上限会把内存吃满。
  appendToolLiveOutput: (taskId, toolName, text) =>
    set((s) => {
      const t = s.tasks[taskId];
      if (!t || !toolName || !text) return s;
      const prev = t.toolLiveOutput?.[toolName] ?? "";
      const merged = prev + text;
      const capped = merged.length > 8000 ? merged.slice(merged.length - 8000) : merged;
      return {
        tasks: {
          ...s.tasks,
          [taskId]: { ...t, toolLiveOutput: { ...(t.toolLiveOutput || {}), [toolName]: capped } },
        },
      };
    }),

  clearToolLiveOutput: (taskId, toolName) =>
    set((s) => {
      const t = s.tasks[taskId];
      if (!t || !t.toolLiveOutput?.[toolName]) return s;
      const next = { ...t.toolLiveOutput };
      delete next[toolName];
      return { tasks: { ...s.tasks, [taskId]: { ...t, toolLiveOutput: next } } };
    }),

  setMsgNumbered: (taskId, seq, role, msgId, replyTo, replyToSeq) =>
    set((s) => {
      const t = s.tasks[taskId];
      if (!t) return s;
      const dependsOn = replyToSeq ? [...(t.dependsOn || []), replyToSeq] : t.dependsOn || [];
      return { tasks: { ...s.tasks, [taskId]: { ...t, msgSeq: seq, dependsOn, convId: msgId } } };
    }),

  setContextUsage: (pct, tokens, window) => set({ contextUsage: { pct, tokens, window } }),

  setContextBudgets: (budgets) => set((s) => ({
    contextUsage: { ...s.contextUsage, budgets },
  })),

  contextUsage: { pct: 0, tokens: 0, window: 131072 },

  showConfirmDialog: (taskId, toolName, preview, confirmToken = "", expiresAt = 0, taskPrompt = "") =>
    new Promise<boolean>((resolve) => {
      // P0-1 / Batch 3.2: 队列化——已有弹窗展示中时新请求入队
      const { confirmDialog: current, confirmQueue = [] } = get();
      const dialog: ConfirmDialog = {
        taskId, toolName, preview, confirmToken, expiresAt, taskPrompt, resolves: [resolve],
      };
      if (current) {
        set({ confirmQueue: [...confirmQueue, dialog] });
        return;
      }
      set({ confirmDialog: dialog });
    }),

  showConfirmBatchDialog: (taskId, items, taskPrompt = "") =>
    new Promise<boolean>((resolve) => {
      const { confirmDialog: current, confirmQueue = [] } = get();
      const dialog: ConfirmDialog = {
        taskId,
        toolName: "batch",
        preview: { count: items.length },
        confirmToken: "",
        taskPrompt,
        resolves: [resolve],
        items,
      };
      if (current) {
        set({ confirmQueue: [...confirmQueue, dialog] });
        return;
      }
      set({ confirmDialog: dialog });
    }),

  resolveConfirmDialog: (confirmed) => {
    const { confirmDialog, confirmQueue = [] } = get();
    if (confirmDialog?.resolves?.length) {
      confirmDialog.resolves.forEach((resolve) => resolve(confirmed));
    }
    // P0-1: 从队列取下一个弹窗展示（FIFO）
    // Batch 4b: 跳过已过期项（expires_at < now → resolve(false) 自动出队，避免 404）
    const now = Date.now() / 1000;
    const rest = [...confirmQueue];
    let next: ConfirmDialog | undefined;
    while (rest.length) {
      const head = rest.shift()!;
      const expired = !!head.expiresAt && head.expiresAt < now;
      if (expired) {
        head.resolves.forEach((resolve) => resolve(false));
        continue;
      }
      next = head;
      break;
    }
    if (next) {
      set({ confirmDialog: next, confirmQueue: rest });
    } else {
      set({ confirmDialog: null, confirmQueue: [] });
    }
  },

  // Batch 4b: 后端 tool_confirm_expired——按 token 移除当前/队列中的确认项
  dropConfirmByToken: (confirmToken) => {
    if (!confirmToken) return;
    const { confirmDialog, confirmQueue = [] } = get();
    const strip = (d: ConfirmDialog): ConfirmDialog | null => {
      if (d.items?.length) {
        const kept = d.items.filter((it) => it.confirmToken !== confirmToken);
        if (kept.length === d.items.length) return d; // 无匹配
        if (!kept.length) {
          d.resolves.forEach((resolve) => resolve(false));
          return null;
        }
        return { ...d, items: kept, preview: { count: kept.length } };
      }
      if (d.confirmToken === confirmToken) {
        d.resolves.forEach((resolve) => resolve(false));
        return null;
      }
      return d;
    };
    const nextCurrent = confirmDialog ? strip(confirmDialog) : null;
    const rest: ConfirmDialog[] = [];
    for (const d of confirmQueue) {
      const stripped = strip(d);
      if (stripped) rest.push(stripped);
    }
    // 当前弹窗被移除时提升队列下一个（FIFO）
    let promoted = nextCurrent;
    while (!promoted && rest.length) {
      promoted = rest.shift()!;
    }
    set({ confirmDialog: promoted, confirmQueue: rest });
  },

  // ---- Phase 14: Plan 步骤执行 ----
  setPlanSteps: (steps) =>
    set({
      planSteps: steps.map((s) => ({ ...s, status: "pending" as const })),
      // 不清空 planActiveId——plan_ready 已正确设置 taskId 绑定
    }),
  updatePlanStep: (idx, status, error) =>
    set((s) => ({
      planSteps: s.planSteps.map((st, i) => {
        if (i !== idx) return st;
        const now = Date.now();
        const patch: Partial<PlanStep> = { status };
        if (status === "running") patch.startTime = st.startTime || now;
        if (status === "done" || status === "failed") {
          patch.endTime = now;
          patch.elapsedMs = st.startTime ? now - st.startTime : 0;
        }
        if (error !== undefined) patch.error = error;
        return { ...st, ...patch };
      }),
    })),
  updatePlanStepFiles: (idx, filesChanged) =>
    set((s) => ({
      planSteps: s.planSteps.map((st, i) => (i === idx ? { ...st, filesChanged } : st)),
    })),
  updatePlanStepOutput: (idx, output) =>
    set((s) => ({
      planSteps: s.planSteps.map((st, i) => (i === idx ? { ...st, output } : st)),
    })),
  updatePlanStepStart: (idx) =>
    set((s) => ({
      planSteps: s.planSteps.map((st, i) => (i === idx ? { ...st, startTime: st.startTime || Date.now() } : st)),
    })),
  updatePlanStepEnd: (idx) =>
    set((s) => ({
      planSteps: s.planSteps.map((st, i) =>
        i === idx ? { ...st, endTime: Date.now(), elapsedMs: st.startTime ? Date.now() - st.startTime : 0 } : st,
      ),
    })),
  clearPlan: () => set({ planSteps: [], planActiveId: null }),

  // ---- Phase 17: 会话管理 ----
  setCurrentSession: (sessionId) => set({ currentSessionId: sessionId }),
  setSessions: (sessions) => set({ sessions }),
  addSession: (s) =>
    set((st) => {
      const filtered = st.sessions.filter((x) => x.session_id !== s.session_id);
      return { sessions: [s, ...filtered] };
    }),
  setTaskSuperseded: (taskId) =>
    set((st) => {
      const t = st.tasks[taskId];
      if (!t) return st;
      // L8.2a: 保存旧回复为 variant
      const oldVariant = {
        chunks: [...t.chunks],
        thinkingChunks: [...t.thinkingChunks],
        toolCalls: [...(t.toolCalls || [])],
        createdAt: Date.now(),
      };
      const variants = [...(t.variants || []), oldVariant];
      return {
        tasks: { ...st.tasks, [taskId]: { ...t, superseded: true, variants, activeVariant: variants.length - 1 } },
      };
    }),
  switchVariant: (taskId, idx) =>
    set((st) => {
      const t = st.tasks[taskId];
      if (!t?.variants?.[idx]) return st;
      const v = t.variants[idx];
      return {
        tasks: {
          ...st.tasks,
          [taskId]: {
            ...t,
            chunks: v.chunks,
            thinkingChunks: v.thinkingChunks,
            toolCalls: v.toolCalls,
            activeVariant: idx,
          },
        },
      };
    }),

  setTaskParent: (taskId, parentTaskId) =>
    set((st) => {
      const t = st.tasks[taskId];
      if (!t) return st;
      return { tasks: { ...st.tasks, [taskId]: { ...t, parentTaskId } } };
    }),

  addTaskVersion: (parentTaskId, version) =>
    set((st) => {
      const t = st.tasks[parentTaskId];
      if (!t) return st;
      const versions = [...(t.versions || []), version];
      return { tasks: { ...st.tasks, [parentTaskId]: { ...t, versions } } };
    }),

  switchVersion: (taskId, versionTaskId) =>
    set((st) => {
      const t = st.tasks[taskId];
      const v = t?.versions?.find((x) => x.taskId === versionTaskId);
      if (!t || !v) return st;
      return {
        tasks: {
          ...st.tasks,
          [taskId]: { ...t, chunks: v.chunks, prompt: v.prompt, activeVersionTaskId: versionTaskId },
        },
      };
    }),

  // ---- Phase 78: 目标澄清 ----
  setClarification: (taskId, questions, stage, reason) =>
    set((st) => {
      const t = st.tasks[taskId];
      if (!t) return st;
      // Task 6.11: reason 仅 auto_followup 阶段携带（低 confidence 自主追问理由）
      const clarification: TaskState["clarification"] = { questions, stage };
      if (reason !== undefined) clarification.reason = reason;
      return {
        tasks: { ...st.tasks, [taskId]: { ...t, clarification } },
      };
    }),
  clearClarification: (taskId) =>
    set((st) => {
      const t = st.tasks[taskId];
      if (!t) return st;
      const { clarification: _omit, ...rest } = t;
      return { tasks: { ...st.tasks, [taskId]: rest } };
    }),
  setConfirmedGoal: (taskId, goal, gradingLevels, assumptions, confidence) =>
    set((st) => {
      const t = st.tasks[taskId];
      if (!t) return st;
      const patch: Partial<TaskState> = {
        confirmedGoal: goal,
        gradingLevels,
      };
      if (assumptions !== undefined) patch.assumptions = assumptions;
      // Task 6.11: 透传 confidence（可选，缺失时不覆盖已有值）
      if (confidence !== undefined) patch.goalConfidence = confidence;
      // 目标确认后清除澄清弹窗状态
      const { clarification: _omit, ...rest } = t;
      return { tasks: { ...st.tasks, [taskId]: { ...rest, ...patch } } };
    }),

  clearTasks: () => set({ tasks: {}, activeTaskId: null, planSteps: [], planActiveId: null }),
  bumpSessionList: () => set((s) => ({ sessionListVersion: s.sessionListVersion + 1 })),

  // ---- Phase 82: Supervisor actions ----
  setTaskLongTask: (taskId) =>
    set((s) => {
      const t = s.tasks[taskId];
      if (!t) return s;
      return { tasks: { ...s.tasks, [taskId]: { ...t, isLongTask: true } } };
    }),

  patchTaskLifecycle: (taskId, patch) =>
    set((s) => {
      const t = s.tasks[taskId];
      if (!t) return s;
      return {
        tasks: {
          ...s.tasks,
          [taskId]: {
            ...t,
            lifecycle: { ...(t.lifecycle || { phase: "" }), ...patch },
          },
        },
      };
    }),

  // Phase 105: 设置任务当前角色（用于 MessageItem RoleBadge / Supervisor 角色卡片高亮）
  setTaskRole: (taskId, role) =>
    set((s) => {
      const t = s.tasks[taskId];
      if (!t) return s;
      return { tasks: { ...s.tasks, [taskId]: { ...t, role } } };
    }),

  // Phase D G1.3: 将讨论共识作为已完成 assistant 消息注入当前会话
  injectAssistantMessage: (taskId, prompt, content, convId, agentId = "deliberation") =>
    set((s) => {
      if (s.tasks[taskId]) return s;
      return {
        tasks: {
          ...s.tasks,
          [taskId]: {
            taskId,
            agentId,
            prompt,
            status: "completed",
            chunks: [content],
            thinkingChunks: [],
            planAuto: false,
            planDone: 0,
            planTotal: 0,
            planRatio: 0,
            toolCalls: [],
            toolResults: [],
            approvals: [],
            routeReason: "deliberation_consensus",
            errorMessage: null,
            startedAt: Date.now(),
            convId,
          },
        },
      };
    }),

  setSupervisorView: (taskId, view) =>
    set((s) => ({ supervisorViews: { ...s.supervisorViews, [taskId]: view } })),

  clearSupervisorView: (taskId) =>
    set((s) => {
      const next = { ...s.supervisorViews };
      delete next[taskId];
      return { supervisorViews: next };
    }),

  setSupervisorActiveTask: (taskId) => set({ supervisorActiveTaskId: taskId }),

  appendSupervisorSuggestion: (taskId, suggestion) =>
    set((s) => {
      const view = s.supervisorViews[taskId];
      if (!view) return s; // best-effort：无 view 时跳过
      return {
        supervisorViews: {
          ...s.supervisorViews,
          [taskId]: { ...view, suggestions: [...view.suggestions, suggestion] },
        },
      };
    }),
}));
