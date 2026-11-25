import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useTaskStore } from "../../../store/taskStore";

const apiMock = vi.fn();

vi.mock("../../../hooks/useApi", () => ({ useApi: () => ({ api: apiMock }) }));

import { TodoChecklist } from "../TodoChecklist";

describe("TodoChecklist", () => {
  beforeEach(() => {
    apiMock.mockReset();
    useTaskStore.setState({ sessionTodos: [], currentSessionId: "s1" });
  });

  it("没有清单时不渲染入口", async () => {
    apiMock.mockResolvedValue({ todos: [] });
    const { container } = render(<TodoChecklist />);
    await waitFor(() => expect(apiMock).toHaveBeenCalled());
    expect(container.firstChild).toBeNull();
  });

  it("拉取清单并可展开查看", async () => {
    apiMock.mockResolvedValue({
      todos: [
        { content: "写实现", status: "in_progress" },
        { content: "补测试", status: "pending" },
      ],
    });
    render(<TodoChecklist />);
    await waitFor(() => expect(screen.getByText("0/2")).toBeInTheDocument());
    fireEvent.click(screen.getByLabelText("todo.title"));
    expect(screen.getByText("写实现")).toBeInTheDocument();
    expect(screen.getByText("补测试")).toBeInTheDocument();
  });

  it("WS 推送后立即更新", async () => {
    apiMock.mockResolvedValue({ todos: [] });
    render(<TodoChecklist />);
    await waitFor(() => expect(apiMock).toHaveBeenCalled());
    act(() => {
      useTaskStore.getState().setSessionTodos([{ content: "来自推送", status: "in_progress" }]);
    });
    await waitFor(() => expect(screen.getByText("0/1")).toBeInTheDocument());
  });
});
