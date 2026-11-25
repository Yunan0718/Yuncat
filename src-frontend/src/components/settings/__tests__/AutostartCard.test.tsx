import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { act } from "react";

// Mock useToast before importing the component——AutostartCard 依赖 ToastContext
const mockToast = vi.fn();
vi.mock("../../../hooks/use-toast", () => ({
  useToast: () => ({ toast: mockToast }),
}));

// Mock Switch——base-ui Switch 依赖 PointerEvent，jsdom 不支持。
// 替换为简单 button：role="switch" + data-checked 反映 checked + click 调 onCheckedChange
vi.mock("../../ui/switch", () => ({
  Switch: ({ checked, onCheckedChange, disabled }: { checked: boolean; onCheckedChange?: (v: boolean) => void; disabled?: boolean }) => (
    <button
      type="button"
      role="switch"
      data-checked={checked ? "" : undefined}
      disabled={disabled}
      onClick={() => onCheckedChange?.(!checked)}
    >
      switch
    </button>
  ),
}));

import { AutostartCard } from "../AutostartCard";

// Phase 95: AutostartCard 测试——mock window.yuncat IPC 验证渲染 + 切换 + 回滚 + 托盘事件同步
describe("AutostartCard", () => {
  const mockGetAutostart = vi.fn();
  const mockSetAutostart = vi.fn();
  const mockOnAutostartChanged = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
    mockOnAutostartChanged.mockReturnValue(() => {}); // unlisten 函数
    (window as unknown as { yuncat: unknown }).yuncat = {
      getAutostart: mockGetAutostart,
      setAutostart: mockSetAutostart,
      onAutostartChanged: mockOnAutostartChanged,
    };
  });

  it("挂载时调 getAutostart 查询状态", async () => {
    mockGetAutostart.mockResolvedValue({ ok: true, enabled: false });
    render(<AutostartCard />);
    await waitFor(() => expect(mockGetAutostart).toHaveBeenCalled());
  });

  it("渲染标题 + 描述 + 开关标签", async () => {
    mockGetAutostart.mockResolvedValue({ ok: true, enabled: false });
    render(<AutostartCard />);
    await waitFor(() => expect(mockGetAutostart).toHaveBeenCalled());
    expect(screen.getByText("autostart.title")).toBeInTheDocument();
    expect(screen.getByText("autostart.desc")).toBeInTheDocument();
    expect(screen.getByText("autostart.openAtLogin")).toBeInTheDocument();
    expect(screen.getByText("autostart.openAtLoginDesc")).toBeInTheDocument();
  });

  it("getAutostart 返回 enabled=true 时开关初始为开", async () => {
    mockGetAutostart.mockResolvedValue({ ok: true, enabled: true });
    render(<AutostartCard />);
    await waitFor(() => expect(mockGetAutostart).toHaveBeenCalled());
    // base-ui Switch checked 时有 data-checked 属性
    const sw = screen.getByRole("switch");
    await waitFor(() => expect(sw).toHaveAttribute("data-checked"));
  });

  it("点击开关调用 setAutostart 并提示成功", async () => {
    mockGetAutostart.mockResolvedValue({ ok: true, enabled: false });
    mockSetAutostart.mockResolvedValue({ ok: true, enabled: true });
    render(<AutostartCard />);
    await waitFor(() => expect(mockGetAutostart).toHaveBeenCalled());

    const sw = screen.getByRole("switch");
    fireEvent.click(sw);
    await waitFor(() => expect(mockSetAutostart).toHaveBeenCalledWith(true));
    await waitFor(() => expect(mockToast).toHaveBeenCalledWith("autostart.enabled", "success"));
  });

  it("setAutostart 失败时回滚开关状态并提示错误", async () => {
    mockGetAutostart.mockResolvedValue({ ok: true, enabled: false });
    mockSetAutostart.mockResolvedValue({ ok: false, error: "denied" });
    render(<AutostartCard />);
    await waitFor(() => expect(mockGetAutostart).toHaveBeenCalled());

    const sw = screen.getByRole("switch");
    fireEvent.click(sw);
    await waitFor(() => expect(mockSetAutostart).toHaveBeenCalledWith(true));
    // 失败 → 提示错误
    await waitFor(() => expect(mockToast).toHaveBeenCalledWith("autostart.failed", "error"));
    // 回滚 → 开关回到 unchecked
    await waitFor(() => expect(sw).not.toHaveAttribute("data-checked"));
  });

  it("setAutostart 抛异常时回滚并提示错误", async () => {
    mockGetAutostart.mockResolvedValue({ ok: true, enabled: false });
    mockSetAutostart.mockRejectedValue(new Error("ipc crash"));
    render(<AutostartCard />);
    await waitFor(() => expect(mockGetAutostart).toHaveBeenCalled());

    const sw = screen.getByRole("switch");
    fireEvent.click(sw);
    await waitFor(() => expect(mockToast).toHaveBeenCalledWith("autostart.failed", "error"));
    await waitFor(() => expect(sw).not.toHaveAttribute("data-checked"));
  });

  it("挂载时注册 onAutostartChanged 监听器", async () => {
    mockGetAutostart.mockResolvedValue({ ok: true, enabled: false });
    render(<AutostartCard />);
    await waitFor(() => expect(mockOnAutostartChanged).toHaveBeenCalled());
  });

  it("托盘切换事件触发时同步开关状态", async () => {
    mockGetAutostart.mockResolvedValue({ ok: true, enabled: false });
    // 捕获 onAutostartChanged 回调
    let onChanged: ((enabled: boolean) => void) | null = null;
    mockOnAutostartChanged.mockImplementation((cb: (enabled: boolean) => void) => {
      onChanged = cb;
      return () => {};
    });
    render(<AutostartCard />);
    await waitFor(() => expect(mockGetAutostart).toHaveBeenCalled());

    const sw = screen.getByRole("switch");
    expect(sw).not.toHaveAttribute("data-checked");

    // 模拟用户在托盘勾选开机自启
    act(() => {
      onChanged?.(true);
    });
    await waitFor(() => expect(sw).toHaveAttribute("data-checked"));
  });

  it("getAutostart 失败时不阻塞渲染（保持默认 false）", async () => {
    mockGetAutostart.mockRejectedValue(new Error("ipc unavailable"));
    render(<AutostartCard />);
    await waitFor(() => expect(mockGetAutostart).toHaveBeenCalled());
    // 标题仍渲染
    expect(screen.getByText("autostart.title")).toBeInTheDocument();
    // 开关默认未开
    const sw = screen.getByRole("switch");
    expect(sw).not.toHaveAttribute("data-checked");
  });

  it("window.yuncat 不存在时不报错", () => {
    delete (window as unknown as { yuncat?: unknown }).yuncat;
    // 不应抛异常
    const { container } = render(<AutostartCard />);
    expect(container).toBeInTheDocument();
  });
});
