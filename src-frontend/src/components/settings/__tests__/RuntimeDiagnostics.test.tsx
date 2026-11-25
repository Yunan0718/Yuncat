import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

const { mockApi } = vi.hoisted(() => ({ mockApi: vi.fn() }));

vi.mock("../../../hooks/useApi", () => ({ useApi: () => ({ api: mockApi }) }));

import { LogLevelControl } from "../LogLevelControl";
import { PendingTriggersCard } from "../PendingTriggersCard";

describe("LogLevelControl", () => {
  beforeEach(() => mockApi.mockReset());

  it("设置日志级别调用 /admin/log-level", async () => {
    mockApi.mockResolvedValue({ ok: true, level: "DEBUG", logger: "" });
    render(<LogLevelControl />);

    fireEvent.change(screen.getByLabelText("diagnostics.logLevel"), {
      target: { value: "DEBUG" },
    });
    fireEvent.click(screen.getByText("diagnostics.apply"));

    await waitFor(() => {
      expect(mockApi).toHaveBeenCalledWith(
        "/api/v1/admin/log-level",
        expect.objectContaining({ method: "POST" }),
      );
    });
    const body = JSON.parse(String(mockApi.mock.calls[0][1].body));
    expect(body).toEqual({ level: "DEBUG", logger: "" });
    await waitFor(() => expect(screen.getByText(/diagnostics.logLevelCurrent/)).toBeInTheDocument());
  });

  it("可以只调某个 logger", async () => {
    mockApi.mockResolvedValue({ ok: true, level: "WARNING", logger: "yuncat.agent" });
    render(<LogLevelControl />);
    fireEvent.change(screen.getByLabelText("diagnostics.logLevel"), {
      target: { value: "WARNING" },
    });
    fireEvent.change(screen.getByPlaceholderText("diagnostics.loggerPlaceholder"), {
      target: { value: "yuncat.agent" },
    });
    fireEvent.click(screen.getByText("diagnostics.apply"));
    await waitFor(() => {
      const body = JSON.parse(String(mockApi.mock.calls[0][1].body));
      expect(body).toEqual({ level: "WARNING", logger: "yuncat.agent" });
    });
  });
});

describe("PendingTriggersCard", () => {
  beforeEach(() => mockApi.mockReset());

  it("列出待处理触发器", async () => {
    mockApi.mockResolvedValue([
      { id: "q1", loop_id: "spec_events", trigger_type: "event", priority: 5, status: "pending", created_at: 1700000000 },
    ]);
    render(<PendingTriggersCard />);
    await waitFor(() => expect(screen.getByText("spec_events")).toBeInTheDocument());
    expect(screen.getByText("event")).toBeInTheDocument();
    expect(screen.getByText("P5")).toBeInTheDocument();
  });

  it("空队列给出提示", async () => {
    mockApi.mockResolvedValue([]);
    render(<PendingTriggersCard />);
    await waitFor(() =>
      expect(screen.getByText("diagnostics.pendingTriggersEmpty")).toBeInTheDocument(),
    );
  });
});
