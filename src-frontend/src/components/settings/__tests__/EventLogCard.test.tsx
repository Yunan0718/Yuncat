import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";

const { mockApi } = vi.hoisted(() => ({ mockApi: vi.fn() }));

vi.mock("../../../hooks/useApi", () => ({ useApi: () => ({ api: mockApi }) }));

import { EventLogCard } from "../EventLogCard";

describe("EventLogCard", () => {
  beforeEach(() => mockApi.mockReset());

  it("先取 last_seq，再拉最近一批事件（新的在上面）", async () => {
    mockApi.mockImplementation((...args: unknown[]) => {
      const url = String(args[0] ?? "");
      // 注意用 endsWith：`limit=100` 也包含子串 "limit=1"
      if (url.endsWith("limit=1")) return Promise.resolve({ events: [], last_seq: 250 });
      return Promise.resolve({
        last_seq: 250,
        events: [
          { seq: 149, id: "e1", topic: "task_submitted", payload: { task_id: "t1" }, created_at: 1700000000 },
          { seq: 150, id: "e2", topic: "task_completed", payload: { task_id: "t1", ok: true }, created_at: 1700000001 },
        ],
      });
    });
    render(<EventLogCard />);

    await waitFor(() => expect(screen.getByText("task_completed")).toBeInTheDocument());
    expect(screen.getByText("task_submitted")).toBeInTheDocument();
    // 最近一批：since = last_seq - 100
    expect(mockApi).toHaveBeenCalledWith("/api/v1/events/replay?since_seq=150&limit=100");
    // 新的在 DOM 里更靠前
    const topics = screen.getAllByText(/task_(submitted|completed)/).map((el) => el.textContent);
    expect(topics[0]).toBe("task_completed");
  });

  it("没有事件时给出空态", async () => {
    mockApi.mockResolvedValue({ events: [], last_seq: 0 });
    render(<EventLogCard />);
    await waitFor(() => expect(screen.getByText("diagnostics.eventLogEmpty")).toBeInTheDocument());
  });
});
