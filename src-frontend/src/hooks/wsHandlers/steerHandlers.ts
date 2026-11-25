import i18next from "i18next";
import { emitGlobalToast } from "../../components/common/toast-context";
import type { Handler } from "./taskHandlers";

export const steerApplied: Handler = (msg) => {
  emitGlobalToast(i18next.t("chat.steerApplied", { prompt: msg.prompt ?? "" }), "success");
};

export const steerError: Handler = (msg) => {
  emitGlobalToast(
    i18next.t("chat.steerFailed", { error: msg.error ?? "" }),
    "error",
  );
};

export const subagentSteerQueued: Handler = () => {
  emitGlobalToast(i18next.t("subagents.steerQueued"), "info");
};

/**
 * 追加指令已入队（还没被应用）——给用户即时回执。
 * 之前只有 steer_applied / steer_error，用户提交后要等下一轮才有反馈。
 */
export const steerQueued: Handler = (msg) => {
  const queued = typeof msg.queued === "number" ? msg.queued : 0;
  emitGlobalToast(
    i18next.t("chat.steerQueued", { queued, defaultValue: "指令已排队" }),
    "info",
  );
};
