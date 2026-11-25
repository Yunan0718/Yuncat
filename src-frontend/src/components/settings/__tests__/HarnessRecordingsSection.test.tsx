import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

const { mockApi } = vi.hoisted(() => ({ mockApi: vi.fn() }));

vi.mock("../../../hooks/useApi", () => ({ useApi: () => ({ api: mockApi }) }));

import { HarnessRecordingsSection } from "../HarnessRecordingsSection";

const ROWS = [
  { id: "rid-0001-aaaa", task_id: "task-0001-bbbb", created_at: 1700000000 },
];

const DETAIL = {
  ...ROWS[0],
  turn_data: {
    tool_name: "read_file",
    arguments: { path: "a.txt" },
    result: { success: true, data: { content: "x" } },
  },
};

describe("HarnessRecordingsSection", () => {
  beforeEach(() => {
    mockApi.mockReset();
  });

  it("没有录制时给出引导（说明需要 YUNCAT_HARNESS_RECORD=1）", async () => {
    mockApi.mockResolvedValue([]);
    render(<HarnessRecordingsSection />);
    await waitFor(() => expect(screen.getByText("harness.empty")).toBeInTheDocument());
    expect(screen.getByText("harness.emptyHint")).toBeInTheDocument();
  });

  it("列出录制并可查看参数", async () => {
    mockApi.mockImplementation((url: string) => {
      if (url.includes("/harness/recordings/")) return Promise.resolve(DETAIL);
      return Promise.resolve(ROWS);
    });
    render(<HarnessRecordingsSection />);
    await waitFor(() => expect(screen.getByText("rid-0001…")).toBeInTheDocument());

    fireEvent.click(screen.getByText("rid-0001…"));
    await waitFor(() => expect(screen.getByText("read_file")).toBeInTheDocument());
    expect(screen.getByText(/"path": "a.txt"/)).toBeInTheDocument();
  });

  it("回放结果一致时显示 match", async () => {
    mockApi.mockImplementation((url: string, _opts?: { method?: string }) => {
      if (url.endsWith("/replay")) {
        return Promise.resolve({ status: "ok", match: true, diffs: [] });
      }
      if (url.includes("/harness/recordings/")) return Promise.resolve(DETAIL);
      return Promise.resolve(ROWS);
    });
    render(<HarnessRecordingsSection />);
    await waitFor(() => expect(screen.getByText("rid-0001…")).toBeInTheDocument());
    fireEvent.click(screen.getByText("rid-0001…"));
    await waitFor(() => expect(screen.getByText("harness.replay")).toBeInTheDocument());

    fireEvent.click(screen.getByText("harness.replay"));
    await waitFor(() => expect(screen.getByText("harness.replayMatch")).toBeInTheDocument());
    expect(mockApi).toHaveBeenCalledWith(
      "/api/v1/harness/recordings/rid-0001-aaaa/replay",
      { method: "POST" },
    );
  });

  it("回放有差异时列出 diff", async () => {
    mockApi.mockImplementation((url: string) => {
      if (url.endsWith("/replay")) {
        return Promise.resolve({ status: "ok", match: false, diffs: ["data changed"] });
      }
      if (url.includes("/harness/recordings/")) return Promise.resolve(DETAIL);
      return Promise.resolve(ROWS);
    });
    render(<HarnessRecordingsSection />);
    await waitFor(() => expect(screen.getByText("rid-0001…")).toBeInTheDocument());
    fireEvent.click(screen.getByText("rid-0001…"));
    await waitFor(() => expect(screen.getByText("harness.replay")).toBeInTheDocument());
    fireEvent.click(screen.getByText("harness.replay"));
    await waitFor(() => expect(screen.getByText("harness.replayDiff")).toBeInTheDocument());
    expect(screen.getByText("data changed")).toBeInTheDocument();
  });
});
