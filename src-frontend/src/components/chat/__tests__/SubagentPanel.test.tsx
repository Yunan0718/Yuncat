import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { SubagentPanel } from "../SubagentPanel";

const { apiMock } = vi.hoisted(() => ({ apiMock: vi.fn() }));

vi.mock("../../../hooks/useApi", () => ({ useApi: () => ({ api: apiMock }) }));
vi.mock("react-i18next", () => ({ useTranslation: () => ({ t: (key: string) => key }) }));
vi.mock("../../common/toast-context", () => ({ emitGlobalToast: vi.fn() }));

describe("SubagentPanel", () => {
  beforeEach(() => {
    apiMock.mockReset();
    apiMock.mockImplementation((path: string) => {
      if (path.endsWith("/subagents")) {
        return Promise.resolve({
          items: [{
            id: "child-12345678", input: "检查登录流程", status: "running",
            agent_id: "yuncat", created_at: 1, output_preview: "正在读取文件",
          }],
        });
      }
      return Promise.resolve({ ok: true });
    });
  });

  it("展示运行中子 Agent 并支持停止", async () => {
    render(<SubagentPanel taskId="parent" status="running" />);
    expect(await screen.findByText("检查登录流程")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /subagents\.stop/ }));
    await waitFor(() => expect(apiMock).toHaveBeenCalledWith(
      "/api/v1/tasks/parent/subagents/child-12345678/stop", { method: "POST" },
    ));
  });

  it("可向运行中子 Agent 追加指令", async () => {
    render(<SubagentPanel taskId="parent" status="running" />);
    await screen.findByText("检查登录流程");
    fireEvent.click(screen.getByRole("button", { name: /subagents\.steer$/ }));
    const input = screen.getByPlaceholderText("subagents.steerPlaceholder");
    fireEvent.change(input, { target: { value: "先跑测试" } });
    fireEvent.click(screen.getByRole("button", { name: "subagents.steerSend" }));
    await waitFor(() => expect(apiMock).toHaveBeenCalledWith(
      "/api/v1/tasks/parent/subagents/child-12345678/steer",
      { method: "POST", body: JSON.stringify({ prompt: "先跑测试" }) },
    ));
  });
});
