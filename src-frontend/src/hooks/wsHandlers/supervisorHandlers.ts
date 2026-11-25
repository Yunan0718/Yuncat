import i18next from "i18next";
import { emitGlobalToast } from "../../components/common/toast-context";
import { useTaskStore } from "../../store/taskStore";
import type { TaskState } from "../../types/task";

import type { Handler } from "./taskHandlers";

export const supervisorSuggestion: Handler = (msg) => {
  if (msg.task_id && msg.suggestion) {
    useTaskStore.getState().appendSupervisorSuggestion(msg.task_id, String(msg.suggestion));
  }
};

// ── Phase 82: 长任务 Supervisor 事件 ──
export const longTaskStepStart: Handler = (msg) => {
  // best-effort 更新 current_step_id（无 view 时跳过，等全量拉取创建）
  const ts82 = useTaskStore.getState();
  const view82 = ts82.supervisorViews[msg.task_id];
  if (view82) {
    useTaskStore.setState({
      supervisorViews: {
        ...ts82.supervisorViews,
        [msg.task_id]: { ...view82, current_step_id: msg.step_id ?? null },
      },
    });
  }
};

// 修复 1.7 (F-076): 为 step_done/fail/completed/failed 注册 handler，即时更新
// current_step_id 和 status。全量拉取（抽屉打开/生命周期变化时）负责刷新完整 view。
export const longTaskStepDone: Handler = (msg) => {
  patchSupervisorView(msg.task_id, { current_step_id: null });
};

export const longTaskStepFail: Handler = (msg) => {
  patchSupervisorView(msg.task_id, { current_step_id: null });
};

export const longTaskCompleted: Handler = (msg) => {
  patchSupervisorView(msg.task_id, { current_step_id: null, status: "completed" });
};

export const longTaskFailed: Handler = (msg) => {
  patchSupervisorView(msg.task_id, { current_step_id: null, status: "failed" });
};

/** best-effort 局部 patch SupervisorView——无 view 时跳过（等全量拉取创建） */
function patchSupervisorView(
  taskId: string,
  patch: Partial<{ current_step_id: string | null; status: string }>,
) {
  const ts = useTaskStore.getState();
  const view = ts.supervisorViews[taskId];
  if (!view) return;
  useTaskStore.setState({
    supervisorViews: {
      ...ts.supervisorViews,
      [taskId]: { ...view, ...patch },
    },
  });
}

// ── Phase 86: 长任务生命周期事件（task_lifecycle）──
export const taskLifecycle: Handler = (msg) => {
  const ts86 = useTaskStore.getState().tasks[msg.task_id];
  if (!ts86) return;
  const subtype = msg.subtype;
  const now = Date.now();
  // H13 修复：用 taskStore 的规范类型替代 any——对齐 patchTaskLifecycle 签名，
  // 字段拼写错误现在会被 tsc 捕获（原 any 完全绕过类型检查）。
  const patch: Partial<NonNullable<TaskState["lifecycle"]>> = {};
  if (subtype === "phase_start") {
    patch.phase = msg.phase;
    patch.phaseDescription = msg.description;
    patch.phaseStartedAt = msg.started_at ? msg.started_at * 1000 : now;
  } else if (subtype === "step_done") {
    patch.stepIndex = msg.step_index;
    patch.totalSteps = msg.total_steps;
    patch.outputPreview = msg.output_preview;
  } else if (subtype === "stall_check") {
    patch.stall = {
      lastTool: msg.last_tool ?? null,
      roundsWithoutProgress: msg.rounds_without_progress,
      suggestion: msg.suggestion,
      at: now,
    };
  } else if (subtype === "estimate") {
    patch.estimate = {
      elapsedSeconds: msg.elapsed_seconds,
      estimatedRemainingSeconds: msg.estimated_remaining_seconds,
      confidence: msg.confidence,
      at: now,
    };
  }
  useTaskStore.getState().patchTaskLifecycle(msg.task_id, patch);
};

export const bridgeToolApproval: Handler = (msg) => {
  useTaskStore.getState().setBridgeApproval({
    approvalId: msg.approval_id,
    toolName: msg.tool_name,
    arguments: msg.arguments || "",
    autoApprove: msg.auto_approve === true,
  });
};

// ── Phase 80 Tier 3: 系统建议长任务（complexity=extreme 时推送）──
// 用户可在 ChatView 横幅点"使用长任务"确认 → 走 long_task_confirm 创建新 task
export const longTaskSuggested: Handler = (msg) => {
  if (msg.task_id) {
    useTaskStore.getState().setLongTaskSuggestion({
      taskId: String(msg.task_id),
      reason: String(msg.reason || ""),
    });
  }
};

// ── Phase 49-F5: 长任务分析阶段 + 标准查询阶段进度 ──
// 这两个阶段在 goal_confirmed 之后、decompose 之前执行，
// 前端通过 lifecycle.phase 展示进度，让用户知道系统在做什么。

export const analysisStarted: Handler = (msg) => {
  if (msg.task_id) {
    useTaskStore.getState().patchTaskLifecycle(msg.task_id, { phase: "analyzing" });
  }
};

export const analysisReport: Handler = (msg) => {
  // 分析报告存储到 task state——LongTaskSupervisorDrawer 可读取展示
  if (msg.task_id && msg.report) {
    useTaskStore.setState((s) => {
      const t = s.tasks[msg.task_id];
      if (!t) return s;
      return {
        tasks: {
          ...s.tasks,
          [msg.task_id]: {
            ...t,
            analysisReport: String(msg.report),
          },
        },
      };
    });
  }
};

export const standardsLookupStarted: Handler = (msg) => {
  if (msg.task_id) {
    useTaskStore.getState().patchTaskLifecycle(msg.task_id, { phase: "standards_lookup" });
  }
};

export const standardsLookupReport: Handler = (msg) => {
  // 标准查询报告存储到 task state
  if (msg.task_id && msg.report) {
    useTaskStore.setState((s) => {
      const t = s.tasks[msg.task_id];
      if (!t) return s;
      return {
        tasks: {
          ...s.tasks,
          [msg.task_id]: {
            ...t,
            standardsReport: msg.report,
          },
        },
      };
    });
  }
};

// ── Phase 49-F6: pending_approval_reinject —— 重连后重新注入待审批 token ──
// 后端在 task resume 时检查 pending_tokens 表，重发待审批请求。
// 前端复用 tool_confirm_required 的弹窗逻辑。
export const pendingApprovalReinject: Handler = (msg) => {
  if (msg.task_id && msg.token) {
    useTaskStore.getState().showConfirmDialog(
      msg.task_id,
      msg.state_type ?? "pending_approval",
      (msg.payload ?? {}) as Record<string, unknown>,
      msg.token,
    );
  }
};

/**
 * 长任务上下文翻滚（上下文窗口用完 → 换一代 Worker 会话继续）。
 * 后端早就在推，前端没接：用户只看到进度突然跳，不知道发生了什么。
 */
export const longTaskWindowRollover: Handler = (msg) => {
  const generation = typeof msg.generation === "number" ? msg.generation : 0;
  const progress = typeof msg.progress === "number" ? Math.round(msg.progress * 100) : 0;
  emitGlobalToast(
    i18next.t("supervisor.windowRollover", {
      generation,
      progress,
      defaultValue: `上下文翻滚：第 ${generation} 代（${progress}%）`,
    }),
    "info",
  );
};

/**
 * 监督者发现偏离 → 升交给主 Agent 仲裁。这是需要用户知道的事：
 * 之前这条事件没人处理，长任务跑偏时界面毫无提示。
 */
export const longTaskSupervisorEscalation: Handler = (msg) => {
  emitGlobalToast(
    i18next.t("supervisor.escalated", {
      reason: msg.reason ?? "",
      defaultValue: "长任务监督升级：需要仲裁",
    }),
    "error",
  );
};

/** 仲裁有了结论（继续 / 中止 / 重新锚定）——告诉用户最后是怎么处理的。 */
export const longTaskArbitrationResolved: Handler = (msg) => {
  const decision = String(msg.decision || msg.action || "");
  emitGlobalToast(
    i18next.t("supervisor.arbitrationResolved", {
      decision,
      directive: msg.directive ?? "",
      defaultValue: `长任务仲裁：${decision || "已处理"}`,
    }),
    "info",
  );
};
