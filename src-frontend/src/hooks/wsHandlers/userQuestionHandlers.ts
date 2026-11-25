import i18next from "i18next";
import { useTaskStore } from "../../store/taskStore";
import { emitGlobalToast } from "../../components/common/toast-context";
import type { Handler } from "./taskHandlers";

export const userQuestionRequest: Handler = (msg, store) => {
  const token = String(msg.token || "");
  const question = String(msg.question || "").trim();
  if (!token || !question) {
    emitGlobalToast(i18next.t("askUser.error", { error: "invalid question payload" }), "error");
    return;
  }
  store.enqueueUserQuestion({
    token,
    task_id: String(msg.task_id || ""),
    agent_id: typeof msg.agent_id === "string" ? msg.agent_id : "",
    question,
    options: Array.isArray(msg.options) ? msg.options.map(String) : [],
    allow_free_text: msg.allow_free_text !== false,
    expires_at: Number(msg.expires_at) || 0,
  });
};

export const userQuestionAck: Handler = (msg) => {
  const token = String(msg.token || "");
  if (!token) return;
  const active = useTaskStore.getState().userQuestions.some((item) => item.token === token);
  useTaskStore.getState().removeUserQuestion(token);
  if (!msg.accepted && active) {
    emitGlobalToast(i18next.t("askUser.answerRejected"), "error");
  }
};

export const userQuestionError: Handler = (msg) => {
  const token = String(msg.token || "");
  const active = !token || useTaskStore.getState().userQuestions.some((item) => item.token === token);
  if (token) useTaskStore.getState().removeUserQuestion(token);
  if (active) emitGlobalToast(i18next.t("askUser.error", { error: msg.error || "" }), "error");
};

export const userQuestionInvalid: Handler = (msg) => {
  emitGlobalToast(
    i18next.t("askUser.answerTooLong", { max: Number(msg.max_chars) || 8000 }),
    "error",
  );
};

export const userQuestionClosed: Handler = (msg) => {
  const token = String(msg.token || "");
  if (!token) return;
  const active = useTaskStore.getState().userQuestions.some((item) => item.token === token);
  useTaskStore.getState().removeUserQuestion(token);
  if (active) {
    emitGlobalToast(
      msg.reason === "timeout" ? i18next.t("askUser.expired") : i18next.t("askUser.taskEnded"),
      "info",
    );
  }
};
