import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

// Mock useToast——WorkspaceSection 依赖 ToastContext
const mockToast = vi.fn();
vi.mock("../../../hooks/use-toast", () => ({
  useToast: () => ({ toast: mockToast }),
}));

import { WorkspaceSection } from "../WorkspaceSection";

// Task 7.22: WorkspaceSection 测试——验证目录浏览器按钮渲染 + openFileDialog IPC 调用 + 选中路径回填
describe("WorkspaceSection", () => {
  const mockOpenFileDialog = vi.fn();
  let mockApi: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    vi.clearAllMocks();
    mockApi = vi.fn();
    // 首次挂载时 GET /api/v1/settings 返回空对象
    mockApi.mockResolvedValue({});
    (window as unknown as { yuncat: unknown }).yuncat = {
      openFileDialog: mockOpenFileDialog,
    };
  });

  afterEach(() => {
    delete (window as unknown as { yuncat?: unknown }).yuncat;
  });

  // ── 渲染 ──────────────────────────────────────────────────

  it("渲染路径输入框和浏览按钮", async () => {
    render(<WorkspaceSection api={mockApi} />);
    await waitFor(() => {
      expect(screen.getByPlaceholderText("settings.workspaceDefault")).toBeInTheDocument();
    });
    expect(screen.getByText("settings.browse")).toBeInTheDocument();
  });

  // ── 目录浏览器 ────────────────────────────────────────────

  it("点击浏览按钮调用 openFileDialog 且 selectFolder=true", async () => {
    mockOpenFileDialog.mockResolvedValue({ ok: true, canceled: true });

    render(<WorkspaceSection api={mockApi} />);
    await waitFor(() => {
      expect(screen.getByText("settings.browse")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByText("settings.browse"));

    await waitFor(() => {
      expect(mockOpenFileDialog).toHaveBeenCalledTimes(1);
    });
    const callArgs = mockOpenFileDialog.mock.calls[0][0];
    expect(callArgs.selectFolder).toBe(true);
    expect(callArgs.title).toBe("settings.selectWorkspaceDir");
  });

  it("选中目录后回填到输入框", async () => {
    mockOpenFileDialog.mockResolvedValue({
      ok: true,
      canceled: false,
      path: "D:\\TestWorkspace",
    });

    render(<WorkspaceSection api={mockApi} />);
    await waitFor(() => {
      expect(screen.getByText("settings.browse")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByText("settings.browse"));

    await waitFor(() => {
      const input = screen.getByPlaceholderText("settings.workspaceDefault") as HTMLInputElement;
      expect(input.value).toBe("D:\\TestWorkspace");
    });
  });

  it("取消选择时不修改输入框", async () => {
    mockOpenFileDialog.mockResolvedValue({ ok: false, canceled: true });

    render(<WorkspaceSection api={mockApi} />);
    await waitFor(() => {
      expect(screen.getByText("settings.browse")).toBeInTheDocument();
    });

    const inputBefore = screen.getByPlaceholderText("settings.workspaceDefault") as HTMLInputElement;
    const valueBefore = inputBefore.value;
    fireEvent.click(screen.getByText("settings.browse"));

    await waitFor(() => {
      expect(mockOpenFileDialog).toHaveBeenCalledTimes(1);
    });
    const inputAfter = screen.getByPlaceholderText("settings.workspaceDefault") as HTMLInputElement;
    expect(inputAfter.value).toBe(valueBefore);
  });

  it("Electron 不可用时静默忽略不报错", async () => {
    // 移除 window.yuncat 模拟 Electron 不可用场景
    delete (window as unknown as { yuncat?: unknown }).yuncat;

    render(<WorkspaceSection api={mockApi} />);
    await waitFor(() => {
      expect(screen.getByText("settings.browse")).toBeInTheDocument();
    });

    // 不应抛出异常
    fireEvent.click(screen.getByText("settings.browse"));
    await waitFor(() => {
      expect(screen.getByText("settings.browse")).toBeInTheDocument();
    });
  });
});
