import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

// Mock useToast
const mockToast = vi.fn();
vi.mock("../../../hooks/use-toast", () => ({
  useToast: () => ({ toast: mockToast }),
}));

// Mock useAppStore——LogViewer 从 store 读 port + token
vi.mock("../../../store/appStore", () => ({
  useAppStore: (selector: (s: { port: number; token: string }) => unknown) =>
    selector({ port: 1420, token: "test-token" }),
}));

import { LogViewer } from "../LogViewer";

// Phase 96-5: LogViewer 测试——mock fetch 验证加载/过滤/导出全流程
describe("LogViewer", () => {
  let mockFetch: ReturnType<typeof vi.fn>;
  const originalCreateObjectURL = URL.createObjectURL;
  const originalRevokeObjectURL = URL.revokeObjectURL;

  beforeEach(() => {
    vi.clearAllMocks();
    mockFetch = vi.fn();
    vi.stubGlobal("fetch", mockFetch);
    URL.createObjectURL = vi.fn(() => "blob:mock-url");
    URL.revokeObjectURL = vi.fn();
  });

  afterEach(() => {
    URL.createObjectURL = originalCreateObjectURL;
    URL.revokeObjectURL = originalRevokeObjectURL;
    vi.unstubAllGlobals();
  });

  // ── 渲染 + 初始加载 ──────────────────────────────────────

  it("渲染标题、描述、工具栏", async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      json: async () => ({ lines: [], total: 0, filtered: 0, truncated: false }),
    });
    render(<LogViewer />);
    expect(screen.getByText("logviewer.title")).toBeInTheDocument();
    expect(screen.getByText("logviewer.desc")).toBeInTheDocument();
    expect(screen.getByText("logviewer.all")).toBeInTheDocument();
    // 等待加载完成——loading=true 时按钮显示 logviewer.loading，完成后显示 logviewer.refresh
    await waitFor(() => expect(mockFetch).toHaveBeenCalled());
    await waitFor(() => expect(screen.getByText("logviewer.refresh")).toBeInTheDocument());
    expect(screen.getByText("logviewer.export")).toBeInTheDocument();
  });

  it("挂载时自动调 /api/v1/logs/tail 加载日志", async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      json: async () => ({
        lines: ["2026-06-30 [INFO] test: line1", "2026-06-30 [ERROR] test: line2"],
        total: 2,
        filtered: 2,
        truncated: false,
      }),
    });
    render(<LogViewer />);
    await waitFor(() => expect(mockFetch).toHaveBeenCalled());
    const [url] = mockFetch.mock.calls[0];
    expect(url).toContain("/api/v1/logs/tail");
    expect(url).toContain("lines=500");
    // 日志行渲染
    await waitFor(() => expect(screen.getByText(/line1/)).toBeInTheDocument());
    expect(screen.getByText(/line2/)).toBeInTheDocument();
  });

  it("空日志时显示 empty 提示", async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      json: async () => ({ lines: [], total: 0, filtered: 0, truncated: false }),
    });
    render(<LogViewer />);
    await waitFor(() => expect(mockFetch).toHaveBeenCalled());
    await waitFor(() => expect(screen.getByText("logviewer.empty")).toBeInTheDocument());
  });

  it("truncated=true 时显示截断提示", async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      json: async () => ({ lines: ["line1"], total: 1000, filtered: 1, truncated: true }),
    });
    render(<LogViewer />);
    await waitFor(() => expect(screen.getByText("logviewer.truncated")).toBeInTheDocument());
  });

  // ── 等级过滤 ──────────────────────────────────────────────

  it("点击 ERROR 按钮带 level=ERROR 重新加载", async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      json: async () => ({ lines: [], total: 0, filtered: 0, truncated: false }),
    });
    render(<LogViewer />);
    await waitFor(() => expect(mockFetch).toHaveBeenCalled());
    mockFetch.mockClear();

    const errorBtn = screen.getByText("ERROR");
    fireEvent.click(errorBtn);
    await waitFor(() => expect(mockFetch).toHaveBeenCalled());
    const [url] = mockFetch.mock.calls[0];
    expect(url).toContain("level=ERROR");
  });

  it("点击 ALL 按钮不带 level 参数", async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      json: async () => ({ lines: [], total: 0, filtered: 0, truncated: false }),
    });
    render(<LogViewer />);
    await waitFor(() => expect(mockFetch).toHaveBeenCalled());
    mockFetch.mockClear();

    // 先选 ERROR 再切回 ALL
    fireEvent.click(screen.getByText("ERROR"));
    await waitFor(() => expect(mockFetch).toHaveBeenCalled());
    mockFetch.mockClear();

    fireEvent.click(screen.getByText("logviewer.all"));
    await waitFor(() => expect(mockFetch).toHaveBeenCalled());
    const [url] = mockFetch.mock.calls[0];
    expect(url).not.toContain("level=");
  });

  // ── 关键字搜索 ────────────────────────────────────────────

  it("输入关键字带 q 参数重新加载", async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      json: async () => ({ lines: [], total: 0, filtered: 0, truncated: false }),
    });
    render(<LogViewer />);
    await waitFor(() => expect(mockFetch).toHaveBeenCalled());
    mockFetch.mockClear();

    const searchInput = screen.getByLabelText("logviewer.searchPlaceholder");
    fireEvent.change(searchInput, { target: { value: "OpenClaw" } });
    // React 18 StrictMode 下 effect 可能被触发多次，等待最近一次调用包含 q 参数
    await waitFor(() => {
      expect(mockFetch).toHaveBeenCalled();
      const calls = mockFetch.mock.calls;
      const [url] = calls[calls.length - 1];
      expect(url).toContain("q=OpenClaw");
    });
  });

  // ── 刷新按钮 ──────────────────────────────────────────────

  it("点击刷新按钮重新加载", async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      json: async () => ({ lines: [], total: 0, filtered: 0, truncated: false }),
    });
    render(<LogViewer />);
    await waitFor(() => expect(mockFetch).toHaveBeenCalled());
    // 清除初始加载的调用记录——不断言精确次数（React StrictMode 可能多次触发 effect）
    mockFetch.mockClear();

    // 等待 loading 结束（按钮变回 refresh）再点击
    await waitFor(() => expect(screen.getByText("logviewer.refresh")).toBeInTheDocument());
    fireEvent.click(screen.getByText("logviewer.refresh"));
    await waitFor(() => expect(mockFetch).toHaveBeenCalled());
  });

  // ── 导出 ──────────────────────────────────────────────────

  it("点击导出触发下载", async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      json: async () => ({ lines: [], total: 0, filtered: 0, truncated: false }),
    });
    // mock <a>.click()
    const clickSpy = vi.fn();
    const originalCreateElement = document.createElement.bind(document);
    vi.spyOn(document, "createElement").mockImplementation((tag: string) => {
      const el = originalCreateElement(tag);
      if (tag === "a") el.click = clickSpy;
      return el;
    });

    render(<LogViewer />);
    await waitFor(() => expect(mockFetch).toHaveBeenCalled());
    mockFetch.mockClear();

    // 导出按钮的第二次 fetch——download 端点
    mockFetch.mockResolvedValue({
      ok: true,
      blob: async () => new Blob(["log content"], { type: "text/plain" }),
    });

    fireEvent.click(screen.getByText("logviewer.export"));
    await waitFor(() => expect(clickSpy).toHaveBeenCalled());
    await waitFor(() => expect(mockToast).toHaveBeenCalledWith("logviewer.exportSuccess", "success"));
  });

  it("导出响应非 ok 时提示失败", async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      json: async () => ({ lines: [], total: 0, filtered: 0, truncated: false }),
    });
    render(<LogViewer />);
    await waitFor(() => expect(mockFetch).toHaveBeenCalled());
    mockFetch.mockClear();
    mockFetch.mockResolvedValue({ ok: false });

    fireEvent.click(screen.getByText("logviewer.export"));
    await waitFor(() => expect(mockToast).toHaveBeenCalledWith("logviewer.exportFailed", "error"));
  });

  // ── 错误处理 ──────────────────────────────────────────────

  it("加载日志响应含 error 时提示失败", async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      json: async () => ({ error: "read failed" }),
    });
    render(<LogViewer />);
    await waitFor(() =>
      expect(mockToast).toHaveBeenCalledWith(
        expect.stringContaining("logviewer.loadFailed"),
        "error",
      ),
    );
  });

  it("加载日志 fetch 抛异常时提示失败", async () => {
    mockFetch.mockRejectedValue(new Error("network down"));
    render(<LogViewer />);
    await waitFor(() => expect(mockToast).toHaveBeenCalledWith("logviewer.loadFailed", "error"));
  });

  // ── 日志行颜色 ────────────────────────────────────────────

  it("ERROR 日志行渲染为 destructive 颜色", async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      json: async () => ({
        lines: ["2026-06-30 [ERROR] test: something broke"],
        total: 1,
        filtered: 1,
        truncated: false,
      }),
    });
    render(<LogViewer />);
    await waitFor(() => expect(screen.getByText(/something broke/)).toBeInTheDocument());
    // 含 [ERROR] 的行应有 text-destructive class
    const errorLine = screen.getByText(/something broke/);
    expect(errorLine.className).toContain("text-destructive");
  });

  it("WARNING 日志行渲染为黄色", async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      json: async () => ({
        lines: ["2026-06-30 [WARNING] test: careful"],
        total: 1,
        filtered: 1,
        truncated: false,
      }),
    });
    render(<LogViewer />);
    await waitFor(() => expect(screen.getByText(/careful/)).toBeInTheDocument());
    const warnLine = screen.getByText(/careful/);
    expect(warnLine.className).toContain("text-yellow-600");
  });
});
