/**
 * types/task.ts —— 任务相关类型契约
 *
 * 6c-2 修复：从 store/taskStore.ts 提取类型定义。
 * 目的：将类型契约与 zustand 运行时实现分离——消费者可独立导入类型而不引入 store 副作用，
 * 同时让 taskStore.ts 聚焦于状态实现。taskStore.ts 通过 re-export 保持现有 import 路径兼容。
 */

// ---- 基础类型 ----

export interface StreamChunk {
  task_id: string;
  data: string;
}

/** Phase 78: 目标澄清问题 */
export interface ClarificationQuestion {
  id: string;
  type: "scope" | "acceptance" | "constraint" | "priority" | "assumption";
  question: string;
}

/** P2-2.5: 后端推送的动态预算——供前端截断点消费，替代硬编码值 */
export interface ContextBudgets {
  tool_result_limit?: number;
  history_limit?: number;
  fts_memories?: number;
  vector_memories?: number;
  skill_preview_chars?: number;
  skill_total_chars?: number;
  micro_compact_threshold?: number;
}

export interface TaskState {
  taskId: string;
  agentId: string;
  prompt: string;
  status: "running" | "completed" | "failed" | "cancelled" | "paused" | "interrupted";
  chunks: string[];
  routeReason: string;
  errorMessage: string | null;
  /** 后端给的错误码（AUTH / RATE_LIMIT / CONFIG_MISSING …）——用来给用户可执行的下一步，而不是只甩一句报错 */
  errorCode?: string;
  /** Phase 6: 任务进度（步数） */
  progress?: { step: number; total: number };
  /** Phase 6: 结构化结果类型 */
  resultType?: string;
  /** Phase 17: 消息已被后续操作替代（编辑/重新生成 → 渲染时半透明） */
  superseded?: boolean;
  /** Phase 18: 上下文注入摘要 */
  contextSummary?: string;
  /** cswitch 风格：精确 Token 用量（从 API usage 字段） */
  tokenUsage?: {
    promptTokens: number;
    completionTokens: number;
    cacheHitTokens: number;
    cacheMissTokens: number;
    elapsedMs: number;
  };
  /** Phase 20: 思维链 chunk 累积 */
  thinkingChunks: string[];
  /** Phase 20: 工具调用记录 — 按轮分组 [{round, name, args}] */
  toolCalls: Array<{ name?: string; args?: unknown; index: number; round?: number; startedAt?: number }>;
  /** Phase 35: 当前工具轮次 */
  toolRound?: number;
  /** Phase 35: 工具结果预览 · Phase 71: 增加 startedAt/finishedAt 用于耗时徽章 */
  toolResults: Array<{ tool_name: string; success: boolean; preview: string; fullResult?: string; startedAt?: number; finishedAt?: number }>;
  /** AI 审批结论：放行（approve）/ 否决（deny），来自 WS approval_review */
  approvals?: Array<{ tool_name: string; decision: string; reason?: string }>;
  /** P0: 实时工具状态 */
  currentTool?: { name: string; status: "running" | "done" | "done-retry" | "failed"; preview?: string };
  /** 工具实时输出（后端 tool_output_chunk 事件）——tool_name → 已收到的输出文本 */
  toolLiveOutput?: Record<string, string>;
  /** 消息序号（全局递增） */
  msgSeq?: number;
  /** 依赖的消息序号列表 */
  dependsOn?: number[];
  /** Plan 自动续行: 是否有自动生成的 plan.md */
  planAuto?: boolean;
  planDone?: number;
  planTotal?: number;
  planRatio?: number;
  /** 任务创建时间戳——桌面通知用 (长任务完成时弹出) */
  startedAt?: number;
  /** L8.2a: 多备选回复——存储 regenerate 前的旧输出 */
  variants?: Array<{
    chunks: string[];
    thinkingChunks: string[];
    toolCalls: Array<{ name?: string; args?: unknown; index: number; round?: number; startedAt?: number }>;
    createdAt: number;
  }>;
  activeVariant?: number;
  /** Phase 49-C: 版本树——记录编辑/重试生成的历史版本 */
  versions?: Array<{ taskId: string; prompt: string; chunks: string[]; createdAt: number }>;
  parentTaskId?: string | null;
  activeVersionTaskId?: string | null;
  /** 消息所属会话 id */
  convId?: string;
  /** Phase 78: 目标澄清——服务端推送的问题列表 + 阶段（initial/followup/auto_followup/adversarial_replan）
   *  Task 6.11: auto_followup 阶段携带 reason（低 confidence 自主追问理由） */
  clarification?: { questions: ClarificationQuestion[]; stage: string; reason?: string };
  /** Phase 78: 确认的目标——refine_goal 后由服务端推送，执行中保持不变 */
  confirmedGoal?: string;
  /** Task 6.11: 目标确认置信度（0.0-1.0，来自 refine_goal.confidence） */
  goalConfidence?: number;
  /** Phase 78: 验收等级（2-5 级，从基础到完善） */
  gradingLevels?: string[];
  /** Phase 78: 提炼的假设列表 */
  assumptions?: string[];
  /** Phase 82: 标记为长任务（WS task_created 消息 long_task:true 时设置） */
  isLongTask?: boolean;
  /** Phase 49-F5: 长任务分析阶段报告（analysis_report WS 消息） */
  analysisReport?: string;
  /** Phase 49-F5: 长任务标准查询报告（standards_lookup_report WS 消息） */
  standardsReport?: unknown;
  /** Phase 86: 长任务生命周期事件（task_lifecycle WS 消息聚合） */
  lifecycle?: {
    phase: string;
    phaseDescription?: string;
    phaseStartedAt?: number;
    stepIndex?: number;
    totalSteps?: number;
    outputPreview?: string;
    stall?: {
      lastTool?: string | null;
      roundsWithoutProgress: number;
      suggestion?: string;
      at: number;
    };
    estimate?: {
      elapsedSeconds: number;
      estimatedRemainingSeconds?: number;
      confidence: number;
      at: number;
    };
  };
  /** Phase 105: 当前任务角色（pm/dev/reviewer/merge） */
  role?: "pm" | "dev" | "reviewer" | "merge";
  /** Phase 108: 真实子任务树节点（来自 parent_task_id：spawn_subagent / plan 子步骤 / 版本链） */
  subTasks?: SubTaskNode[];
  /** E4: 是否正在流式输出——流式期间跳过 Markdown 渲染（O(n²)→O(n)），stream_done 后全量渲染 */
  isStreaming?: boolean;
}

export interface PlanStep {
  step: number;
  task: string;
  status: "pending" | "running" | "done" | "failed";
  taskId?: string;
  stepId?: string;
  fileHint?: string;
  filesChanged?: string[];
  output?: string;
  error?: string;
  startTime?: number;
  endTime?: number;
  elapsedMs?: number;
}
export interface PlanState {
  steps: PlanStep[];
  totalSteps: number;
  activePlanId: string | null;
}

/** Phase 17: 会话摘要 */
export interface SessionSummary {
  session_id: string;
  msg_count: number;
  last_at: number;
  agents: string[];
  title: string;
  preview: string;
  /** 账本口径的 token 用量（输入 + 输出）——与用量看板同一口径 */
  total_tokens?: number;
  prompt_tokens?: number;
  completion_tokens?: number;
  llm_calls?: number;
  /** 该会话跑过的任务数与状态分布 */
  task_count?: number;
  completed_tasks?: number;
  failed_tasks?: number;
}

export interface ConfirmDialogItem {
  toolName: string;
  preview: Record<string, unknown>;
  confirmToken: string;
  /** Batch 4b: 确认 token 过期时间（epoch 秒）——0 表示未知 */
  expiresAt?: number;
}

export interface ConfirmDialog {
  taskId: string;
  toolName: string;
  preview: Record<string, unknown>;
  confirmToken: string;
  /** Batch 4b: 确认 token 过期时间（epoch 秒）——0 表示未知 */
  expiresAt?: number;
  /** Batch 4b: 来源任务 input 摘要（后端截 80 字符）——弹窗头部展示 */
  taskPrompt?: string;
  /** Batch 3.2: 支持单/批量弹窗共享队列——一个弹窗可能对应多个 resolve */
  resolves: Array<(confirmed: boolean) => void>;
  /** Batch 3.2: 批量确认时为非空 */
  items?: ConfirmDialogItem[];
}

/** Phase 44: Hermes Bridge 危险工具审批 */
export interface BridgeApproval {
  approvalId: string;
  toolName: string;
  arguments: string;
  autoApprove: boolean;
}

/** Phase 80 Tier 3: 系统建议长任务确认（complexity=extreme 时推送 long_task_suggested） */
export interface LongTaskSuggestion {
  taskId: string;
  reason: string;
}

export interface UserQuestion {
  token: string;
  task_id: string;
  agent_id?: string;
  question: string;
  options: string[];
  allow_free_text: boolean;
  expires_at: number;
}

/** 会话任务清单（对标 DSH tool-todo 的 item 结构） */
export interface TodoItem {
  content: string;
  status: "pending" | "in_progress" | "completed";
}

/** Phase 108: 子任务树节点（镜像后端 tasks_repo.get_child_tasks 返回；原始 API 为 snake_case，与 TaskDetailData 一致） */
export interface SubTaskNode {
  id: string;
  input: string;
  status: "running" | "completed" | "failed" | "cancelled" | "paused" | "interrupted" | "pending";
  agent_id: string;
  created_at: number;
  output_preview: string;
}

// ---- Phase 82: Supervisor 类型 ----

/** Phase 82: plan_steps 分解树节点（镜像后端 PlanTreeNode） */
export interface PlanTreeNode {
  id: string;
  title: string;
  status: "pending" | "running" | "completed" | "failed" | "blocked" | "skipped";
  level: number;
  parent_id: string | null;
  description?: string;
  output?: string;
  review?: Record<string, unknown>;
}

/** Phase 82: 审查日志条目（来自 plan_steps.review 聚合） */
export interface ReviewLogEntry {
  step_id: string;
  step_title: string;
  review: Record<string, unknown>;
}

/** Phase 82: SupervisorView（镜像后端 services/task_supervisor.py SupervisorView.to_dict()） */
export interface SupervisorView {
  task_id: string;
  status: string;
  confirmed_goal: string;
  grading_levels: string[];
  assumptions: string[];
  plan_tree: PlanTreeNode[];
  current_step_id: string | null;
  elapsed_seconds: number;
  estimated_remaining_seconds: number | null;
  review_log: ReviewLogEntry[];
  stall_rounds: number;
  suggestions: string[];
}

// ---- Store 接口 ----

/** TaskStore 完整状态 + actions 接口——供 create<TaskStore> 使用 */
export interface TaskStore {
  tasks: Record<string, TaskState>;
  userQuestions: UserQuestion[];
  enqueueUserQuestion: (question: UserQuestion) => void;
  removeUserQuestion: (token: string) => void;
  clearUserQuestionsForTask: (taskId: string) => void;
  activeTaskId: string | null;
  confirmDialog: ConfirmDialog | null;
  /**
   * P0-1 (2026-07-04): 等待展示的确认弹窗队列（FIFO）。
   * 旧实现：新请求来时自动 resolve(false) 旧弹窗 → Agent 跳过工具。
   * 新实现：已有弹窗展示中时新请求入队，等当前弹窗 resolve 后再弹出下一个。
   * 对标 Claude Code / Cursor：多个确认请求依次排队，不互相打断。
   */
  confirmQueue: ConfirmDialog[];
  /** Phase 44: Hermes Bridge 工具审批 */
  bridgeApproval: BridgeApproval | null;
  setBridgeApproval: (approval: BridgeApproval | null) => void;
  /** Phase 80 Tier 3: 系统建议长任务确认（复杂度极高时推送） */
  longTaskSuggestion: LongTaskSuggestion | null;
  setLongTaskSuggestion: (suggestion: LongTaskSuggestion | null) => void;

  // ---- actions ----
  createTask: (taskId: string, agentId: string, prompt: string, reason: string, convId?: string) => void;
  appendChunk: (taskId: string, chunk: string) => void;
  /** E4: 设置流式状态——stream_done 后设 false 触发全量 Markdown 渲染 */
  setStreaming: (taskId: string, streaming: boolean) => void;
  /** Phase 20: 追加思维链 chunk */
  appendThinking: (taskId: string, data: string) => void;
  /** Phase 20: 追加工具调用记录 → 合并同 index delta（H7） */
  appendToolCall: (taskId: string, call: { name?: string; args?: unknown; index?: number }, index: number) => void;
  /** 后端已累积合并——直接替换整个工具调用列表（round 来自 tool_call_chunk，用于行动链按轮分组） */
  replaceToolCalls: (taskId: string, calls: Array<{ index: number; name?: string; args?: unknown; round?: number }>) => void;
  /** Phase 43 R1-R2: 任务路由信息 */
  setTaskRoute: (taskId: string, agentId: string, routeReason: string) => void;
  /** Phase 35: 追加工具结果预览 · Phase 71: startedAt 可选（来自 tool_start 时间戳） */
  appendToolResult: (taskId: string, toolName: string, success: boolean, preview: string, fullResult?: string, startedAt?: number) => void;
  /** 追加一条 AI 审批结论（轨迹卡片会把它渲染成一行） */
  appendApproval: (taskId: string, entry: { tool_name: string; decision: string; reason?: string }) => void;
  /** Phase 35: 设置工具轮次 */
  setToolRound: (taskId: string, round: number, max: number) => void;
  setTaskDone: (taskId: string) => void;
  /** 通用状态同步（后端 task_status 事件）——只接受已知状态值 */
  setTaskStatus: (taskId: string, status: string) => void;
  setTaskError: (taskId: string, error: string, errorCode?: string) => void;
  setActiveTask: (taskId: string | null) => void;
  /** Phase 6: 标记任务为已取消 */
  setTaskCancelled: (taskId: string) => void;
  /** Phase 48-H 维度 6: 标记任务为已暂停 */
  setTaskPaused: (taskId: string) => void;
  /** Phase 48-H 维度 6: 标记任务为已恢复 */
  setTaskResumed: (taskId: string) => void;
  /** G4: 标记任务为已中断（优雅关闭/强杀，可恢复，区别于 cancelled 终态） */
  setTaskInterrupted: (taskId: string) => void;
  /** Phase 6: 更新任务进度 */
  setTaskProgress: (taskId: string, step: number, total: number) => void;
  /** Phase 6: 设置结构化结果类型 */
  setTaskResultType: (taskId: string, resultType: string) => void;
  /** P0: 实时工具状态 */
  setTaskToolStatus: (
    taskId: string,
    name: string,
    status: "running" | "done" | "done-retry" | "failed",
    preview?: string,
  ) => void;
  /** 行动链修复：工具完成后清除 currentTool（见 taskStore.clearCurrentTool） */
  clearCurrentTool: (taskId: string) => void;
  /** 追加工具实时输出（命令 stdout/stderr 流式回传） */
  appendToolLiveOutput: (taskId: string, toolName: string, text: string) => void;
  /** 工具结束后清掉实时输出——最终结果以 toolResults 为准 */
  clearToolLiveOutput: (taskId: string, toolName: string) => void;
  /** 消息序号注册 */
  setMsgNumbered: (
    taskId: string,
    seq: number,
    role: string,
    msgId: string,
    replyTo?: string,
    replyToSeq?: number,
  ) => void;
  /** 上下文用量状态 */
  contextUsage: { pct: number; tokens: number; window: number; budgets?: ContextBudgets };
  setContextUsage: (pct: number, tokens: number, window: number) => void;
  /** P2-2.5: 持久化后端推送的动态预算——供前端截断点消费 */
  setContextBudgets: (budgets: ContextBudgets) => void;
  showConfirmDialog: (
    taskId: string,
    toolName: string,
    preview: Record<string, unknown>,
    confirmToken?: string,
    expiresAt?: number,
    taskPrompt?: string,
  ) => Promise<boolean>;
  showConfirmBatchDialog: (
    taskId: string,
    items: ConfirmDialogItem[],
    taskPrompt?: string,
  ) => Promise<boolean>;
  resolveConfirmDialog: (confirmed: boolean) => void;
  /** Batch 4b: 后端 tool_confirm_expired 事件——按 token 从弹窗队列移除（含批量 items） */
  dropConfirmByToken: (confirmToken: string) => void;
  /** Phase 14: Plan 步骤执行状态 */
  planSteps: PlanStep[];
  planActiveId: string | null;
  setPlanSteps: (steps: { step: number; task: string }[]) => void;
  updatePlanStep: (stepIndex: number, status: "running" | "done" | "failed", error?: string) => void;
  updatePlanStepFiles: (stepIndex: number, filesChanged: string[]) => void;
  updatePlanStepOutput: (stepIndex: number, output: string) => void;
  updatePlanStepStart: (stepIndex: number) => void;
  updatePlanStepEnd: (stepIndex: number) => void;
  clearPlan: () => void;
  /** Phase 17: 会话管理 */
  currentSessionId: string | null;
  sessions: SessionSummary[];
  /** 当前会话的任务清单——Agent 用 todo_write 整表替换 */
  sessionTodos: TodoItem[];
  setSessionTodos: (todos: TodoItem[]) => void;
  /** 交付物面板的刷新信号——present 声明或任务完成时 +1 */
  deliverablesVersion: number;
  bumpDeliverables: () => void;
  setCurrentSession: (sessionId: string | null) => void;
  setSessions: (sessions: SessionSummary[]) => void;
  addSession: (s: SessionSummary) => void;
  setTaskSuperseded: (taskId: string) => void;
  switchVariant: (taskId: string, idx: number) => void;
  /** Phase 49-C: 设置任务父节点 */
  setTaskParent: (taskId: string, parentTaskId: string | null) => void;
  /** Phase 49-C: 向父任务追加历史版本 */
  addTaskVersion: (
    parentTaskId: string,
    version: { taskId: string; prompt: string; chunks: string[]; createdAt: number },
  ) => void;
  /** Phase 49-C: 切换到指定历史版本 */
  switchVersion: (taskId: string, versionTaskId: string) => void;
  /** Phase 78: 设置目标澄清问题（服务端推送 goal_clarification_request 时调用）
   *  Task 6.11: reason 用于 auto_followup 阶段展示低 confidence 追问理由 */
  setClarification: (
    taskId: string,
    questions: ClarificationQuestion[],
    stage: string,
    reason?: string,
  ) => void;
  /** Phase 78: 清除澄清状态（用户提交答案或跳过后） */
  clearClarification: (taskId: string) => void;
  /** Phase 78: 设置确认目标（服务端推送 goal_confirmed 时调用）
   *  Task 6.11: confidence 来自 refine_goal，用于前端展示置信度 */
  setConfirmedGoal: (
    taskId: string,
    goal: string,
    gradingLevels: string[],
    assumptions?: string[],
    confidence?: number,
  ) => void;
  clearTasks: () => void;
  /** Phase 17: 触发会话列表刷新（TaskCenter 发消息后 +1） */
  sessionListVersion: number;
  bumpSessionList: () => void;
  // ---- Phase 82: Supervisor 状态 ----
  /** Phase 82: per-task SupervisorView 缓存（GET /api/v1/tasks/{id}/supervisor 返回） */
  supervisorViews: Record<string, SupervisorView>;
  /** Phase 82: 抽屉中当前查看的 task id */
  supervisorActiveTaskId: string | null;
  /** Phase 82: 标记 task 为长任务 */
  setTaskLongTask: (taskId: string) => void;
  /** Phase 82: 写入/更新 SupervisorView */
  setSupervisorView: (taskId: string, view: SupervisorView) => void;
  /** Phase 82: 清除 SupervisorView */
  clearSupervisorView: (taskId: string) => void;
  /** Phase 82: 切换抽屉中查看的 task */
  setSupervisorActiveTask: (taskId: string | null) => void;
  /** Phase 82: 追加 suggestion 到 view（WS supervisor_suggestion 消息） */
  appendSupervisorSuggestion: (taskId: string, suggestion: string) => void;
  /** Phase 86: patch 合并 task_lifecycle 事件到 task.lifecycle */
  patchTaskLifecycle: (
    taskId: string,
    patch: Partial<NonNullable<TaskState["lifecycle"]>>,
  ) => void;
  /** Phase 105: 设置任务当前角色 */
  setTaskRole: (taskId: string, role: NonNullable<TaskState["role"]>) => void;
  /** Phase D G1.3: 将讨论共识作为 assistant 消息注入当前会话 */
  injectAssistantMessage: (taskId: string, prompt: string, content: string, convId: string, agentId?: string) => void;
}
