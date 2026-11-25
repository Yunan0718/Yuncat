import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useTaskStore } from "../../../store/taskStore";
import { UserQuestionDialog } from "../UserQuestionDialog";

const { answerMock, toastMock } = vi.hoisted(() => ({
  answerMock: vi.fn(), toastMock: vi.fn(),
}));

vi.mock("react-i18next", () => ({ useTranslation: () => ({ t: (key: string) => key }) }));
vi.mock("../../../hooks/useWebSocket", () => ({
  useWebSocket: () => ({ answerUserQuestion: answerMock }),
}));
vi.mock("../../../components/common/toast-context", () => ({ emitGlobalToast: toastMock }));

describe("UserQuestionDialog", () => {
  beforeEach(() => {
    vi.useRealTimers();
    answerMock.mockReset();
    toastMock.mockReset();
    useTaskStore.setState({
      userQuestions: [{
        token: "q-token", task_id: "t1", question: "Pick one",
        options: ["A", "B"], allow_free_text: false,
        expires_at: Math.floor(Date.now() / 1000) + 300,
      }],
    });
  });

  afterEach(() => vi.useRealTimers());

  it("提交选项回答", () => {
    render(<UserQuestionDialog />);
    fireEvent.click(screen.getByText("B"));
    fireEvent.click(screen.getByText("askUser.submit"));
    expect(answerMock).toHaveBeenCalledWith("q-token", "B");
  });

  it("取消会显式唤醒等待方", () => {
    render(<UserQuestionDialog />);
    fireEvent.click(screen.getByText("common.cancel"));
    expect(answerMock).toHaveBeenCalledWith("q-token", "", true);
    expect(useTaskStore.getState().userQuestions).toEqual([]);
  });

  it("队列只展示最早的问题", () => {
    useTaskStore.getState().enqueueUserQuestion({
      token: "second", task_id: "t2", question: "Second", options: [],
      allow_free_text: true, expires_at: 0,
    });
    render(<UserQuestionDialog />);
    expect(screen.getByText("Pick one")).toBeTruthy();
    expect(screen.queryByText("Second")).toBeNull();
  });

  it("超时后自动关闭并提示", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-10-21T00:00:00Z"));
    useTaskStore.setState({
      userQuestions: [{
        token: "q-token", task_id: "t1", question: "Pick one",
        options: ["A"], allow_free_text: false,
        expires_at: Math.floor(Date.now() / 1000) + 2,
      }],
    });
    render(<UserQuestionDialog />);
    act(() => vi.advanceTimersByTime(3000));
    expect(useTaskStore.getState().userQuestions).toEqual([]);
    expect(toastMock).toHaveBeenCalledWith("askUser.expired", "info");
  });
});
