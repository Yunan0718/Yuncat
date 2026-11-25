import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

const { mockApi, mockConfirm } = vi.hoisted(() => ({
  mockApi: vi.fn(),
  mockConfirm: vi.fn(),
}));

vi.mock("../../../hooks/useApi", () => ({ useApi: () => ({ api: mockApi }) }));
vi.mock("../../../hooks/useConfirmDialog", () => ({
  useConfirmDialog: () => ({ confirm: mockConfirm, confirmDialog: null }),
}));

import { SchedulesSection } from "../SchedulesSection";

const ROWS = [
  {
    id: "s-pending",
    task_input: "每天同步一次",
    agent_id: "yuncat",
    cron_expr: "0 9 * * *",
    next_run_at: Math.floor(Date.now() / 1000) + 3600,
    status: "pending",
    label: "daily",
    last_run_at: null,
    run_count: 5,
    error_count: 0,
  },
  {
    id: "s-paused",
    task_input: "周报",
    agent_id: "yuncat",
    cron_expr: "0 10 * * 1",
    next_run_at: 0,
    status: "paused",
    label: "",
    last_run_at: null,
    run_count: 3,
    error_count: 1,
  },
];

describe("SchedulesSection", () => {
  beforeEach(() => {
    mockApi.mockReset();
    mockConfirm.mockReset();
    mockApi.mockResolvedValue({ items: ROWS });
  });

  it("列出定时任务与状态", async () => {
    render(<SchedulesSection />);
    await waitFor(() => expect(screen.getByText("每天同步一次")).toBeInTheDocument());
    expect(screen.getByText("周报")).toBeInTheDocument();
    expect(screen.getByText("schedules.status.pending")).toBeInTheDocument();
    expect(screen.getByText("schedules.status.paused")).toBeInTheDocument();
    expect(screen.getByText(/0 9 \* \* \*/)).toBeInTheDocument();
  });

  it("只有待执行任务能取消", async () => {
    render(<SchedulesSection />);
    await waitFor(() => expect(screen.getByText("每天同步一次")).toBeInTheDocument());
    expect(screen.getAllByLabelText("schedules.cancelAction")).toHaveLength(1);
  });

  it("取消前必须确认，确认后调用取消接口并刷新", async () => {
    mockConfirm.mockResolvedValue(true);
    render(<SchedulesSection />);
    await waitFor(() => expect(screen.getByText("每天同步一次")).toBeInTheDocument());

    fireEvent.click(screen.getByLabelText("schedules.cancelAction"));

    await waitFor(() => {
      expect(mockApi).toHaveBeenCalledWith(
        "/api/v1/schedules/s-pending/cancel",
        { method: "POST" },
      );
    });
    expect(mockConfirm).toHaveBeenCalledTimes(1);
    // 取消后重新拉列表
    await waitFor(() => expect(mockApi).toHaveBeenCalledWith("/api/v1/schedules"));
  });

  it("确认时点取消则不调用接口", async () => {
    mockConfirm.mockResolvedValue(false);
    render(<SchedulesSection />);
    await waitFor(() => expect(screen.getByText("每天同步一次")).toBeInTheDocument());
    fireEvent.click(screen.getByLabelText("schedules.cancelAction"));
    await waitFor(() => expect(mockConfirm).toHaveBeenCalledTimes(1));
    expect(mockApi).not.toHaveBeenCalledWith(
      expect.stringContaining("/cancel"), expect.anything(),
    );
  });

  it("一次性任务（cron 为空）显示「一次性」而不是空白", async () => {
    mockApi.mockResolvedValue({
      items: [{
        ...ROWS[0],
        id: "s-once",
        task_input: "30 分钟后提醒我",
        cron_expr: "",
      }],
    });
    render(<SchedulesSection />);
    await waitFor(() => expect(screen.getByText("30 分钟后提醒我")).toBeInTheDocument());
    expect(screen.getByText("schedules.once")).toBeInTheDocument();
  });

  it("空列表给出引导", async () => {
    mockApi.mockResolvedValue({ items: [] });
    render(<SchedulesSection />);
    await waitFor(() => expect(screen.getByText("schedules.empty")).toBeInTheDocument());
  });
});
