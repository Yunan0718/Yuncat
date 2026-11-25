import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

const { mockApi } = vi.hoisted(() => ({ mockApi: vi.fn() }));

vi.mock("../../../hooks/useApi", () => ({ useApi: () => ({ api: mockApi }) }));

import { ArbitrationTab } from "../ArbitrationTab";

const PENDING = {
  pending: [
    {
      task_id: "task-1234-abcd",
      pending_arbitration: true,
      current_generation: 2,
      goal_summary: "做出一个完整的登录流程",
      last_escalation: { verdict: "drift", issues: ["偏离原始目标：开始改配色"] },
    },
  ],
};

describe("ArbitrationTab", () => {
  beforeEach(() => mockApi.mockReset());

  it("没有待仲裁时给出空态", async () => {
    mockApi.mockResolvedValue({ pending: [] });
    render(<ArbitrationTab />);
    await waitFor(() => expect(screen.getByText("supervisor.arbitrationEmpty")).toBeInTheDocument());
  });

  it("列出待仲裁并展示升级原因", async () => {
    mockApi.mockResolvedValue(PENDING);
    render(<ArbitrationTab />);
    await waitFor(() => expect(screen.getByText(/task-123/)).toBeInTheDocument());
    expect(screen.getByText("做出一个完整的登录流程")).toBeInTheDocument();
    expect(screen.getByText("drift")).toBeInTheDocument();
    expect(screen.getByText("偏离原始目标：开始改配色")).toBeInTheDocument();
  });

  it("adjust 带指令提交仲裁", async () => {
    mockApi.mockImplementation((url: string, opts?: { method?: string }) => {
      if (opts?.method === "POST") return Promise.resolve({ ok: true });
      return Promise.resolve(PENDING);
    });
    render(<ArbitrationTab />);
    await waitFor(() => expect(screen.getByText(/task-123/)).toBeInTheDocument());

    fireEvent.change(screen.getByPlaceholderText("supervisor.arbitrationDirective"), {
      target: { value: "只修复登录，不碰样式" },
    });
    fireEvent.click(screen.getByText("supervisor.arbitrationSubmit"));

    await waitFor(() => {
      expect(mockApi).toHaveBeenCalledWith(
        "/api/v1/tasks/task-1234-abcd/arbitration",
        expect.objectContaining({ method: "POST" }),
      );
    });
    const body = JSON.parse(String(mockApi.mock.calls.find((c) => c[1]?.method === "POST")?.[1]?.body));
    expect(body).toEqual({ decision: "adjust", directive: "只修复登录，不碰样式" });
  });

  it("adjust 没填指令时不发请求", async () => {
    mockApi.mockResolvedValue(PENDING);
    render(<ArbitrationTab />);
    await waitFor(() => expect(screen.getByText(/task-123/)).toBeInTheDocument());
    fireEvent.click(screen.getByText("supervisor.arbitrationSubmit"));
    await waitFor(() => {
      expect(mockApi).not.toHaveBeenCalledWith(
        expect.stringContaining("/arbitration"), expect.anything(),
      );
    });
  });

  it("dismiss 不需要指令", async () => {
    mockApi.mockImplementation((url: string, opts?: { method?: string }) => {
      if (opts?.method === "POST") return Promise.resolve({ ok: true });
      return Promise.resolve(PENDING);
    });
    render(<ArbitrationTab />);
    await waitFor(() => expect(screen.getByText(/task-123/)).toBeInTheDocument());

    fireEvent.change(screen.getByLabelText("supervisor.arbitrationDecision"), {
      target: { value: "dismiss" },
    });
    fireEvent.click(screen.getByText("supervisor.arbitrationSubmit"));

    await waitFor(() => {
      const body = JSON.parse(String(mockApi.mock.calls.find((c) => c[1]?.method === "POST")?.[1]?.body));
      expect(body).toEqual({ decision: "dismiss", directive: null });
    });
  });
});
