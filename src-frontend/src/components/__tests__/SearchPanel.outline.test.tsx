import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { SearchPanel } from "../SearchPanel";

const { apiMock } = vi.hoisted(() => ({ apiMock: vi.fn() }));

vi.mock("../../hooks/useApi", () => ({ useApi: () => ({ api: apiMock }) }));
vi.mock("react-i18next", () => ({ useTranslation: () => ({ t: (key: string) => key }) }));

describe("SearchPanel outline", () => {
  beforeEach(() => {
    apiMock.mockReset();
    apiMock.mockResolvedValue({
      turns: [{
        index: 1, task_id: "task-1", title: "修复登录",
        preview: "已检查认证流程", message_count: 4, tokens: 120,
      }],
    });
  });

  it("切换到会话大纲并跳回对应任务", async () => {
    const jump = vi.fn();
    render(
      <SearchPanel visible onClose={vi.fn()} onOpenFile={vi.fn()} sessionId="s1" onJumpToTask={jump} />,
    );
    fireEvent.click(screen.getByText("phase49.search.outlineTab"));
    const title = await screen.findByText("修复登录");
    fireEvent.click(title);
    expect(apiMock).toHaveBeenCalledWith("/api/v1/sessions/s1/outline");
    expect(jump).toHaveBeenCalledWith("task-1");
  });
});
