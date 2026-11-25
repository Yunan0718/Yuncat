import { beforeEach, describe, expect, it, vi } from "vitest";
import { useTaskStore } from "../../../store/taskStore";
import type { UserQuestion } from "../../../types/task";
import {
  userQuestionAck,
  userQuestionClosed,
  userQuestionInvalid,
  userQuestionRequest,
} from "../userQuestionHandlers";

const { toastMock } = vi.hoisted(() => ({ toastMock: vi.fn() }));

vi.mock("../../../components/common/toast-context", () => ({ emitGlobalToast: toastMock }));
vi.mock("i18next", () => ({ default: { t: (key: string) => key } }));

const _question = (token: string): UserQuestion => ({
  token, task_id: "t1", question: "Choose", options: ["A"],
  allow_free_text: false, expires_at: 0,
});

describe("userQuestionHandlers", () => {
  beforeEach(() => {
    toastMock.mockReset();
    useTaskStore.setState({ userQuestions: [] });
  });

  it("按 token 去重并保持 FIFO", () => {
    userQuestionRequest({ ..._question("one"), type: "user_question_request" }, useTaskStore.getState());
    userQuestionRequest({ ..._question("two"), type: "user_question_request" }, useTaskStore.getState());
    userQuestionRequest({ ..._question("one"), type: "user_question_request" }, useTaskStore.getState());
    expect(useTaskStore.getState().userQuestions.map((item) => item.token)).toEqual(["one", "two"]);
  });

  it("拒绝的 ack 会收起失效问题", () => {
    useTaskStore.getState().enqueueUserQuestion(_question("stale"));
    userQuestionAck({ token: "stale", accepted: false }, useTaskStore.getState());
    expect(useTaskStore.getState().userQuestions).toEqual([]);
    expect(toastMock).toHaveBeenCalledWith("askUser.answerRejected", "error");
  });

  it("超时关闭事件只提示当前活跃问题", () => {
    useTaskStore.getState().enqueueUserQuestion(_question("expired"));
    userQuestionClosed({ token: "expired", reason: "timeout" }, useTaskStore.getState());
    userQuestionClosed({ token: "expired", reason: "timeout" }, useTaskStore.getState());
    expect(useTaskStore.getState().userQuestions).toEqual([]);
    expect(toastMock).toHaveBeenCalledTimes(1);
  });

  it("过长回答只提示，不移除当前问题", () => {
    useTaskStore.getState().enqueueUserQuestion(_question("active"));
    userQuestionInvalid({ token: "active", max_chars: 8000 }, useTaskStore.getState());
    expect(useTaskStore.getState().userQuestions).toHaveLength(1);
    expect(toastMock).toHaveBeenCalledWith("askUser.answerTooLong", "error");
  });
});
