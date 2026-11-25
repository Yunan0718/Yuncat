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

import { WorkflowTriggersDialog } from "../WorkflowTriggersDialog";

const TRIGGERS = [
  {
    id: "tr-1",
    workflow_id: "wf-1",
    trigger_type: "event",
    config: { event: "task_completed" },
    enabled: true,
    run_count: 3,
    next_run_at: null,
    last_run_at: null,
    created_at: 1,
  },
  {
    id: "tr-2",
    workflow_id: "wf-1",
    trigger_type: "cron",
    config: { cron_expr: "0 9 * * *" },
    enabled: true,
    run_count: 0,
    next_run_at: Math.floor(Date.now() / 1000) + 3600,
    created_at: 2,
  },
];

describe("WorkflowTriggersDialog", () => {
  beforeEach(() => {
    mockApi.mockReset();
    mockConfirm.mockReset();
    mockApi.mockResolvedValue(TRIGGERS);
  });

  it("列出触发器与配置摘要", async () => {
    render(<WorkflowTriggersDialog workflowId="wf-1" open onClose={() => {}} />);
    await waitFor(() => expect(screen.getByText("task_completed")).toBeInTheDocument());
    expect(screen.getByText("0 9 * * *")).toBeInTheDocument();
    expect(screen.getAllByText("workflowTriggers.types.event").length).toBeGreaterThan(0);
  });

  it("创建事件触发器", async () => {
    mockApi.mockImplementation((url: string, opts?: { method?: string }) => {
      if (url.endsWith("/triggers") && opts?.method === "POST") return Promise.resolve({ trigger_id: "new" });
      return Promise.resolve(TRIGGERS);
    });
    render(<WorkflowTriggersDialog workflowId="wf-1" open onClose={() => {}} />);
    await waitFor(() => expect(screen.getByText("task_completed")).toBeInTheDocument());

    fireEvent.change(screen.getByPlaceholderText("workflowTriggers.eventPlaceholder"), {
      target: { value: "task_failed" },
    });
    fireEvent.click(screen.getByText("workflowTriggers.create"));

    await waitFor(() => {
      expect(mockApi).toHaveBeenCalledWith(
        "/api/v1/workflows/wf-1/triggers",
        expect.objectContaining({ method: "POST" }),
      );
    });
    const postCall = mockApi.mock.calls.find((c) => c[1]?.method === "POST");
    assertTriggerConfig(JSON.parse(String(postCall?.[1]?.body)));
  });

  it("删除触发器需要确认", async () => {
    mockConfirm.mockResolvedValue(true);
    mockApi.mockImplementation((url: string, opts?: { method?: string }) => {
      if (opts?.method === "DELETE") return Promise.resolve({ deleted: true });
      return Promise.resolve(TRIGGERS);
    });
    render(<WorkflowTriggersDialog workflowId="wf-1" open onClose={() => {}} />);
    await waitFor(() => expect(screen.getByText("task_completed")).toBeInTheDocument());

    fireEvent.click(screen.getAllByLabelText("workflowTriggers.delete")[0]);
    await waitFor(() => {
      expect(mockApi).toHaveBeenCalledWith(
        "/api/v1/workflows/wf-1/triggers/tr-1",
        { method: "DELETE" },
      );
    });
    expect(mockConfirm).toHaveBeenCalledTimes(1);
  });

  it("手动触发调用 fire 端点", async () => {
    mockApi.mockImplementation((url: string, _opts?: { method?: string }) => {
      if (url.endsWith("/fire")) return Promise.resolve({ success: true });
      return Promise.resolve(TRIGGERS);
    });
    render(<WorkflowTriggersDialog workflowId="wf-1" open onClose={() => {}} />);
    await waitFor(() => expect(screen.getByText("task_completed")).toBeInTheDocument());

    fireEvent.click(screen.getAllByLabelText("workflowTriggers.fire")[0]);
    await waitFor(() => {
      expect(mockApi).toHaveBeenCalledWith(
        "/api/v1/workflows/wf-1/triggers/tr-1/fire",
        expect.objectContaining({ method: "POST" }),
      );
    });
  });

  it("事件名为空时不发请求", async () => {
    render(<WorkflowTriggersDialog workflowId="wf-1" open onClose={() => {}} />);
    await waitFor(() => expect(screen.getByText("task_completed")).toBeInTheDocument());
    fireEvent.click(screen.getByText("workflowTriggers.create"));
    // 校验失败时不发请求，输入框保持原样
    expect(
      screen.getByPlaceholderText("workflowTriggers.eventPlaceholder"),
    ).toBeInTheDocument();
    expect(mockApi).not.toHaveBeenCalledWith(
      "/api/v1/workflows/wf-1/triggers",
      expect.objectContaining({ method: "POST" }),
    );
  });
});

function assertTriggerConfig(body: { trigger_type: string; config: Record<string, unknown> }) {
  expect(body.trigger_type).toBe("event");
  expect(body.config.event).toBe("task_failed");
}
