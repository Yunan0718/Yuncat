import i18next from "i18next";
import { useTaskStore } from "../../store/taskStore";
import { useDeliberationStore } from "../../store/deliberationStore";
import { useAppStore } from "../../store/appStore";
import { useSpecStore } from "../../store/specStore";
import { emitGlobalToast } from "../../components/common/toast-context";

import type { Handler } from "./taskHandlers";

export const goalClarificationRequest: Handler = (msg) => {
  // Phase 78: 服务端推送澄清问题 → 写入 task.clarification 字段
  // Task 6.11: auto_followup 阶段携带 reason（低 confidence 自主追问理由）
  const qs = Array.isArray(msg.questions) ? msg.questions : [];
  const reason = msg.reason !== undefined ? String(msg.reason ?? "") : undefined;
  useTaskStore.getState().setClarification(
    msg.task_id,
    qs.map((q: Record<string, unknown>) => ({
      id: String(q.id ?? ""),
      type: (String(q.type ?? "scope") as "scope" | "acceptance" | "constraint" | "priority" | "assumption"),
      question: String(q.question ?? ""),
    })),
    String(msg.stage ?? "initial"),
    reason,
  );
};

export const goalConfirmed: Handler = (msg) => {
  // Phase 78: 服务端推送确认目标 → 写入 task.confirmedGoal + 清除澄清状态
  // Task 6.11: 透传 confidence（0.0-1.0）供前端展示目标置信度
  const confidence =
    typeof msg.confidence === "number" ? msg.confidence : undefined;
  useTaskStore.getState().setConfirmedGoal(
    msg.task_id,
    String(msg.confirmed_goal ?? ""),
    Array.isArray(msg.grading_levels) ? msg.grading_levels.map(String) : [],
    Array.isArray(msg.assumptions) ? msg.assumptions.map(String) : [],
    confidence,
  );
};

export const deliberationChunk: Handler = (msg) => {
  useDeliberationStore.getState().appendChunk(msg.role_name, msg.phase, msg.data);
};

export const deliberationPhase: Handler = (msg) => {
  useDeliberationStore.getState().setPhase(msg.phase);
};

export const deliberationDone: Handler = (msg) => {
  useDeliberationStore.getState().setConsensus(msg.consensus);
  useDeliberationStore.getState().setPhase("consensus");
  // Phase D G1.3: 将讨论共识作为 assistant 消息注入当前会话
  try {
    const ts = useTaskStore.getState();
    const convId = ts.currentSessionId;
    if (convId && msg.consensus) {
      const taskId = `delib-${msg.task_id ?? Date.now()}`;
      ts.injectAssistantMessage(
        taskId,
        msg.prompt ?? i18next.t("deliberation.defaultTitle"),
        msg.consensus,
        convId,
        "deliberation",
      );
    }
  } catch {
    // best-effort：注入失败不影响讨论状态
  }
};

export const specPhase: Handler = (msg) => {
  useSpecStore.getState().setPhase(msg.phase as never, msg);
  if (msg.phase === "architect_done")
    useSpecStore.getState().setArchitectDone((msg.spec_name as string) ?? "", (msg.fr_count as number) ?? 0);
  else if (msg.phase === "developer_done")
    useSpecStore.getState().setDeveloperDone((msg.files_count as number) ?? 0, (msg.files as string[]) ?? []);
  else if (msg.phase === "auditor_done")
    useSpecStore.getState().setAuditorDone({
      matches: (msg.matches as number) ?? 0,
      mismatches: (msg.mismatches as number) ?? 0,
      missing: (msg.missing as number) ?? 0,
      mismatchesList: (msg.mismatches_list as string[]) ?? [],
      missingList: (msg.missing_list as string[]) ?? [],
      verificationPassed: (msg.verification_passed as boolean) ?? false,
      verificationResults: [],
      auditorProvider: "",
    });
};

export const specLoopDone: Handler = (msg) => {
  useSpecStore
    .getState()
    .setLoopDone(
      (msg.converged as boolean) ?? false,
      (msg.rounds as number) ?? 0,
      (msg.spec_name as string) ?? "",
      {
        matches: (msg.audit_summary?.matches as number) ?? 0,
        mismatches: (msg.audit_summary?.mismatches as number) ?? 0,
        missing: (msg.audit_summary?.missing as number) ?? 0,
        mismatchesList: (msg.audit_summary?.mismatches_list as string[]) ?? [],
        missingList: (msg.audit_summary?.missing_list as string[]) ?? [],
        verificationPassed: (msg.audit_summary?.verification_passed as boolean) ?? false,
        verificationResults: [],
        auditorProvider: "",
      },
    );
};

export const specEscalated: Handler = () => {
  useSpecStore.getState().setEscalated(0);
};

// AI 自动触发讨论——complexity=high 时后端推送，ChatView 监听后切换到 DeliberationView
// 显示 toast 让用户知道为什么界面突然切换（对标 GitHub Actions 自动触发通知）
export const deliberationAutoStart: Handler = (msg) => {
  const prompt = String(msg.prompt ?? "");
  if (prompt) {
    useAppStore.getState().setPendingDeliberationPrompt(prompt);
    emitGlobalToast(i18next.t("phase49.deliberation.autoStartToast"), "info");
  }
};

// Phase 49-D6: 服务端确认用户消息是否被接受
// 正常路径（accepted=true）无需 UI 反馈——消息已在客户端 optimistic 添加。
// 异常路径（accepted=false）说明 task 已结束或 service 未就绪——
// 在控制台告警便于排查，不打扰用户（消息仍可见，只是不会注入到下一阶段）。
export const deliberationUserMessageAck: Handler = (msg) => {
  if (msg.accepted === false) {
    console.warn(
      "[deliberation] user message rejected:",
      { taskId: msg.task_id, error: msg.error, queuedCount: msg.queued_count },
    );
  }
};
