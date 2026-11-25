import { useTaskStore } from "../../store/taskStore";
import type { TaskStore } from "../../types/task";

import * as task from "./taskHandlers";
import * as tool from "./toolHandlers";
import * as plan from "./planHandlers";
import * as clar from "./clarificationHandlers";
import * as sup from "./supervisorHandlers";
import * as sys from "./systemHandlers";
import * as steer from "./steerHandlers";
import * as userQuestion from "./userQuestionHandlers";
import * as session from "./sessionHandlers";

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Handler = (msg: Record<string, any>, store: TaskStore) => void;

/**
 * WebSocket 消息分发注册表（handler map 模式）。
 *
 * 每个消息类型映射到一个独立 handler 函数。新增消息类型只需在此注册一行。
 *
 * 空 case（auth_ok / pong / sync_state / plan_all_done / resume_start /
 * resume_end）不注册——dispatchToStore 找不到 handler 时静默跳过，与原 switch 行为一致。
 */
const handlers: Record<string, Handler> = {
  // ── task 域 ──
  task_created: task.taskCreated,
  stream_chunk: task.streamChunk,
  replay_messages: task.replayMessages,
  stream_done: task.streamDone,
  error: task.error,
  route_info: task.routeInfo,
  task_routed: task.taskRouted,
  task_cancelled: task.taskCancelled,
  task_paused: task.taskPaused,
  task_resumed: task.taskResumed,
  task_interrupted: task.taskInterrupted,
  task_failed: task.taskFailed,
  budget_exceeded: task.budgetExceeded,
  stream_error: task.streamError,
  task_progress: task.taskProgress,
  task_result_type: task.taskResultType,
  task_status: task.taskStatus,
  workflow_hitl: task.workflowHitl,
  token_usage: task.tokenUsage,
  msg_numbered: task.msgNumbered,
  compact_result: task.compactResult,
  context_stats: task.contextStats,

  // ── tool 域 ──
  tool_call_chunk: tool.toolCallChunk,
  tool_start: tool.toolStart,
  tool_done: tool.toolDone,
  tool_result_preview: tool.toolResultPreview,
  tool_output_chunk: tool.toolOutputChunk,
  standards_lookup_result: tool.standardsLookupResult,
  tool_round: tool.toolRound,
  tool_confirm_required: tool.toolConfirmRequired,
  tool_confirm_batch_required: tool.toolConfirmBatchRequired,
  tool_confirm_error: tool.toolConfirmError,
  tool_confirm_expired: tool.toolConfirmExpired,
  tool_failure_analysis: tool.toolFailureAnalysis,
  approval_review: tool.approvalReview,
  model_degraded: tool.modelDegraded,
  tool_degraded: tool.toolDegraded,
  stop_hooks_failed: tool.stopHooksFailed,
  steer_applied: steer.steerApplied,
  steer_queued: steer.steerQueued,
  steer_error: steer.steerError,
  subagent_steer_queued: steer.subagentSteerQueued,
  user_question_request: userQuestion.userQuestionRequest,
  user_question_ack: userQuestion.userQuestionAck,
  user_question_error: userQuestion.userQuestionError,
  user_question_invalid: userQuestion.userQuestionInvalid,
  user_question_closed: userQuestion.userQuestionClosed,

  // ── system 域 ──
  memory_saved: sys.memorySaved,
  slash_command_matched: sys.slashCommandMatched,

  // ── session 域 ──
  session_title_updated: session.sessionTitleUpdated,
  todo_updated: session.todoUpdated,
  deliverables_updated: session.deliverablesUpdated,

  // ── plan 域 ──
  plan_ready: plan.planReady,
  plan_step_start: plan.planStepStart,
  plan_step_done: plan.planStepDone,
  plan_step_fail: plan.planStepFail,
  plan_progress: plan.planProgress,
  plan_bootstrapped: plan.planBootstrapped,
  context_injected: plan.contextInjected,

  // ── clarification / deliberation / spec 域 ──
  goal_clarification_request: clar.goalClarificationRequest,
  goal_confirmed: clar.goalConfirmed,
  deliberation_chunk: clar.deliberationChunk,
  deliberation_phase: clar.deliberationPhase,
  deliberation_done: clar.deliberationDone,
  deliberation_auto_start: clar.deliberationAutoStart,
  deliberation_user_message_ack: clar.deliberationUserMessageAck,
  spec_phase: clar.specPhase,
  spec_loop_done: clar.specLoopDone,
  spec_escalated: clar.specEscalated,

  // ── supervisor / lifecycle 域 ──
  supervisor_suggestion: sup.supervisorSuggestion,
  task_lifecycle: sup.taskLifecycle,
  long_task_step_start: sup.longTaskStepStart,
  long_task_step_done: sup.longTaskStepDone,
  long_task_step_fail: sup.longTaskStepFail,
  long_task_completed: sup.longTaskCompleted,
  long_task_failed: sup.longTaskFailed,
  long_task_suggested: sup.longTaskSuggested,
  long_task_window_rollover: sup.longTaskWindowRollover,
  long_task_supervisor_escalation: sup.longTaskSupervisorEscalation,
  long_task_arbitration_resolved: sup.longTaskArbitrationResolved,
  bridge_tool_approval: sup.bridgeToolApproval,
  analysis_started: sup.analysisStarted,
  analysis_report: sup.analysisReport,
  standards_lookup_started: sup.standardsLookupStarted,
  standards_lookup_report: sup.standardsLookupReport,
  pending_approval_reinject: sup.pendingApprovalReinject,

  // ── system 域 ──
  agent_notification: sys.agentNotification,
  notification: sys.notification,
  agent_status: sys.agentStatus,
  adapter_degraded: sys.adapterDegraded,
  circuit_breaker_tripped: sys.circuitBreakerTripped,
  thinking_chunk: sys.thinkingChunk,
  context_emergency: sys.contextEmergency,
  pre_compact_warning: sys.preCompactWarning,
  context_micro_compact: sys.contextMicroCompact,
  file_changed: sys.fileChanged,
  diagnostics: sys.diagnostics,
  skill_install_complete: sys.skillInstallComplete,
  approval_error: sys.approvalError,
};

/**
 * WebSocket 消息分发——根据 msg.type 查找 handler 并执行。
 * 找不到 handler 时静默跳过（与原 switch 的 default 行为一致）。
 */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export function dispatchToStore(msg: Record<string, any>) {
  const store = useTaskStore.getState();
  const handler = handlers[msg.type];
  if (handler) handler(msg, store);
}

// 重新导出 getNotifiedSet 以保持向后兼容（useNotification.ts 仍从 useWebSocket 导入）
export { getNotifiedSet } from "./taskHandlers";
