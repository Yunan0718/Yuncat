import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { JobsPanel } from "../JobsPanel";

const { apiMock } = vi.hoisted(() => ({ apiMock: vi.fn() }));

vi.mock("../../hooks/useApi", () => ({ useApi: () => ({ api: apiMock }) }));
vi.mock("react-i18next", () => ({ useTranslation: () => ({ t: (key: string) => key }) }));
vi.mock("../common/toast-context", () => ({ emitGlobalToast: vi.fn() }));

describe("JobsPanel", () => {
  beforeEach(() => {
    apiMock.mockReset();
    apiMock.mockImplementation((path: string) => {
      if (path.startsWith("/api/v1/jobs?")) {
        return Promise.resolve({ items: [{
          id: "job-1", prompt: "运行测试", status: "running", agent_id: "yuncat",
          session_id: "s1", output_preview: "正在执行", round: 2,
        }] });
      }
      return Promise.resolve({ status: "ok" });
    });
  });

  it("展示 Job、打开任务并暂停", async () => {
    const open = vi.fn();
    render(<JobsPanel sessionId="s1" onOpenTask={open} />);
    const prompt = await screen.findByText("运行测试");
    fireEvent.click(prompt);
    expect(open).toHaveBeenCalledWith("job-1");
    fireEvent.click(screen.getByRole("button", { name: /jobs\.pause/ }));
    await waitFor(() => expect(apiMock).toHaveBeenCalledWith(
      "/api/v1/tasks/job-1/pause", { method: "POST" },
    ));
  });
});
