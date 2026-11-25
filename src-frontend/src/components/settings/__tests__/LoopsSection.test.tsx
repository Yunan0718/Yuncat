import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

const { mockApi, mockPrompt } = vi.hoisted(() => ({
  mockApi: vi.fn(),
  mockPrompt: vi.fn(),
}));

vi.mock("../../../hooks/useApi", () => ({ useApi: () => ({ api: mockApi }) }));
vi.mock("../../../hooks/usePromptDialog", () => ({
  usePromptDialog: () => ({ prompt: mockPrompt, promptDialog: null }),
}));

import { LoopsSection } from "../LoopsSection";

const STATUS = {
  ok: true,
  total: 2,
  running: 1,
  degraded: 1,
  loops: [
    {
      loop_id: "scheduler",
      status: "running",
      loop_type: "timer",
      interval: 30,
      priority: 0,
      last_run: Math.floor(Date.now() / 1000) - 5,
      last_ok: Math.floor(Date.now() / 1000) - 5,
      restart_count: 0,
      error_count: 0,
      iteration_count: 120,
      last_error: "",
      strategy: "permanent",
    },
    {
      loop_id: "backup_scheduler",
      status: "degraded",
      loop_type: "timer",
      interval: 600,
      priority: 3,
      last_run: Math.floor(Date.now() / 1000) - 7200,
      last_ok: 0,
      restart_count: 2,
      error_count: 5,
      iteration_count: 30,
      last_error: "boom",
      strategy: "permanent",
    },
  ],
};

describe("LoopsSection", () => {
  beforeEach(() => {
    mockApi.mockReset();
    mockPrompt.mockReset();
    mockApi.mockResolvedValue(STATUS);
  });

  it("列出循环状态、间隔与错误", async () => {
    render(<LoopsSection />);
    await waitFor(() => expect(screen.getByText("scheduler")).toBeInTheDocument());
    expect(screen.getByText("backup_scheduler")).toBeInTheDocument();
    expect(screen.getByText("loops.status.degraded")).toBeInTheDocument();
    expect(screen.getByText("boom")).toBeInTheDocument();
    expect(screen.getByText("loops.summary")).toBeInTheDocument();
  });

  it("非运行中的循环可以启动（只有它显示启动按钮）", async () => {
    mockApi.mockImplementation((url: string, _opts?: { method?: string }) => {
      if (url.includes("/restart")) return Promise.resolve({ ok: true });
      return Promise.resolve(STATUS);
    });
    render(<LoopsSection />);
    await waitFor(() => expect(screen.getByText("scheduler")).toBeInTheDocument());
    expect(screen.getAllByLabelText("loops.start")).toHaveLength(1);

    fireEvent.click(screen.getByLabelText("loops.start"));
    await waitFor(() => {
      expect(mockApi).toHaveBeenCalledWith(
        "/api/v1/supervisor/loops/backup_scheduler/restart",
        { method: "POST" },
      );
    });
  });

  it("调整间隔：合法值发请求", async () => {
    mockPrompt.mockResolvedValue("45");
    mockApi.mockImplementation((url: string, _opts?: { method?: string }) => {
      if (url.includes("/interval")) return Promise.resolve({ ok: true });
      return Promise.resolve(STATUS);
    });
    render(<LoopsSection />);
    await waitFor(() => expect(screen.getByText("scheduler")).toBeInTheDocument());

    fireEvent.click(screen.getAllByLabelText("loops.setInterval")[0]);
    await waitFor(() => {
      expect(mockApi).toHaveBeenCalledWith(
        "/api/v1/supervisor/loops/scheduler/interval?interval=45",
        { method: "POST" },
      );
    });
  });

  it("调整间隔：非法值不发请求（0 会让循环空转）", async () => {
    mockPrompt.mockResolvedValue("0");
    render(<LoopsSection />);
    await waitFor(() => expect(screen.getByText("scheduler")).toBeInTheDocument());
    fireEvent.click(screen.getAllByLabelText("loops.setInterval")[0]);
    await waitFor(() => expect(mockPrompt).toHaveBeenCalledTimes(1));
    expect(mockApi).not.toHaveBeenCalledWith(
      expect.stringContaining("/interval"), expect.anything(),
    );
  });
});
