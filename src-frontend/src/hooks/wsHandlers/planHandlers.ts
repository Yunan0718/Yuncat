import i18next from "i18next";
import { useTaskStore } from "../../store/taskStore";

import type { Handler } from "./taskHandlers";

export const planReady: Handler = (msg) => {
  if (msg.steps && Array.isArray(msg.steps)) {
    useTaskStore.getState().setPlanSteps(
      msg.steps.map((s: Record<string, unknown>) => ({
        step: s.step as number,
        task: s.task as string,
        status: "pending" as const,
        fileHint: (s.file_hint as string) || undefined,
        stepId: (s.step_id as string) || (s.id as string) || undefined,
      })),
    );
    useTaskStore.setState({ planActiveId: msg.task_id });
  }
};

export const planStepStart: Handler = (msg) => {
  const vs = useTaskStore.getState();
  if (vs.planSteps.length === 0 && msg.total_steps)
    vs.setPlanSteps(
      Array.from({ length: msg.total_steps }, (_, i) => ({
        step: i + 1,
        task: msg.step_task || i18next.t("task.stepLabel", { n: i + 1 }),
        status: "pending" as const,
        stepId: (msg.step_id as string) || undefined,
      })),
    );
  vs.updatePlanStepStart(msg.step_index);
  vs.updatePlanStep(msg.step_index, "running");
};

export const planStepDone: Handler = (msg) => {
  const st = useTaskStore.getState();
  st.updatePlanStepEnd(msg.step_index);
  st.updatePlanStep(msg.step_index, "done");
  const af =
    Array.isArray(msg.files_written) && msg.files_written.length > 0 ? msg.files_written : msg.files_changed;
  if (af && Array.isArray(af) && af.length > 0) st.updatePlanStepFiles(msg.step_index, af);
  if (msg.output) {
    st.updatePlanStepOutput(msg.step_index, String(msg.output).slice(0, 1000));
    st.appendChunk(
      msg.task_id,
      "\n\n**" + i18next.t("task.stepDone", { n: msg.step_index + 1 }) + "**:\n" + msg.output.slice(0, 1000) + "\n",
    );
  }
};

export const contextInjected: Handler = (msg) => {
  const ts = useTaskStore.getState().tasks[msg.task_id];
  if (ts)
    useTaskStore.setState({
      tasks: { ...useTaskStore.getState().tasks, [msg.task_id]: { ...ts, contextSummary: msg.summary || "" } },
    });
  // 2026-09-16 修复：这里原来把 history_limit（字符/消息预算）当成「消息条数」乘 400，
  // 于是状态栏出现了 22964.4k / 131k 这种离谱数字。上下文用量只能来自真实用量，
  // 由 token_usage 事件（provider 自己报的 prompt_tokens）更新；这里只更新窗口大小。
  const contextWindow = Number(msg.context_window) || 131072;
  const current = useTaskStore.getState().contextUsage;
  const knownTokens = Math.min(current.tokens, contextWindow);
  useTaskStore
    .getState()
    .setContextUsage(contextWindow > 0 ? knownTokens / contextWindow : 0, knownTokens, contextWindow);
};

export const planBootstrapped: Handler = (msg) => {
  const ts = useTaskStore.getState().tasks[msg.task_id];
  if (ts)
    useTaskStore.setState({
      tasks: {
        ...useTaskStore.getState().tasks,
        [msg.task_id]: { ...ts, planAuto: true, planTotal: msg.pre_steps || 5, planDone: 0 },
      },
    });
};

export const planProgress: Handler = (msg) => {
  const ts = useTaskStore.getState().tasks[msg.task_id];
  if (ts && msg.action === "auto_continue")
    useTaskStore.setState({
      tasks: {
        ...useTaskStore.getState().tasks,
        [msg.task_id]: { ...ts, planDone: msg.done || 0, planTotal: msg.total || 0, planRatio: msg.ratio || 0 },
      },
    });
};

export const planStepFail: Handler = (msg) => {
  const fs = useTaskStore.getState();
  fs.updatePlanStepEnd(msg.step_index);
  fs.updatePlanStep(msg.step_index, "failed", String(msg.error || ""));
};
