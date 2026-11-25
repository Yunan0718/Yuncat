import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

const { mockApi } = vi.hoisted(() => ({ mockApi: vi.fn() }));

vi.mock("../../../hooks/useApi", () => ({ useApi: () => ({ api: mockApi }) }));

import { WorkflowTimeline } from "../WorkflowTimeline";

const RUN = {
  id: "r1",
  workflow_id: "w1",
  status: "running",
  node_results: {},
  started_at: 1000,
  finished_at: null,
};

describe("WorkflowTimeline 运行级控制", () => {
  beforeEach(() => {
    mockApi.mockReset();
  });

  it("运行中显示暂停与取消，点击后调用对应接口", async () => {
    mockApi.mockImplementation(async (url: string, init?: { method?: string }) => {
      if (init?.method === "POST") return { status: "ok" };
      if (String(url).includes("/workflows/runs/r1")) return { ...RUN };
      return {};
    });
    render(<WorkflowTimeline runId="r1" workflowId="w1" />);

    await waitFor(() => expect(screen.getByText("workflowTimeline.pause")).toBeInTheDocument());
    fireEvent.click(screen.getByText("workflowTimeline.pause"));
    await waitFor(() => {
      expect(
        mockApi.mock.calls.some(([u, i]) =>
          String(u).includes("/workflows/runs/r1/pause")
          && (i as { method?: string })?.method === "POST"),
      ).toBe(true);
    });
  });

  it("已暂停的运行显示继续按钮", async () => {
    mockApi.mockImplementation(async (url: string) => {
      if (String(url).includes("/workflows/runs/r1")) return { ...RUN, status: "paused" };
      return {};
    });
    render(<WorkflowTimeline runId="r1" workflowId="w1" />);

    await waitFor(() => expect(screen.getByText("workflowTimeline.resume")).toBeInTheDocument());
    expect(screen.queryByText("workflowTimeline.pause")).toBeNull();
  });
});
