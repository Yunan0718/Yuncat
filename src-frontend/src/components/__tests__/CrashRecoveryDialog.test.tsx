import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { CrashRecoveryDialog } from "../CrashRecoveryDialog";

// Phase 92: CrashRecoveryDialog 测试——mock window.yuncat IPC 验证渲染 + 按钮交互
describe("CrashRecoveryDialog", () => {
  const mockGetLastCrash = vi.fn();
  const mockOpenCrashDir = vi.fn();
  const mockDismissCrash = vi.fn();
  const mockRelaunchApp = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
    // mock window.yuncat IPC
    (window as unknown as { yuncat: unknown }).yuncat = {
      getLastCrash: mockGetLastCrash,
      openCrashDir: mockOpenCrashDir,
      dismissCrash: mockDismissCrash,
      relaunchApp: mockRelaunchApp,
    };
  });

  it("无 crash 时返回 null（不渲染）", async () => {
    mockGetLastCrash.mockResolvedValue({ ok: true, crash: null });
    const { container } = render(<CrashRecoveryDialog />);
    await waitFor(() => expect(mockGetLastCrash).toHaveBeenCalled());
    expect(container.firstChild).toBeNull();
  });

  it("getLastCrash 返回失败时不渲染", async () => {
    mockGetLastCrash.mockResolvedValue({ ok: false, error: "io" });
    const { container } = render(<CrashRecoveryDialog />);
    await waitFor(() => expect(mockGetLastCrash).toHaveBeenCalled());
    expect(container.firstChild).toBeNull();
  });

  it("有 crash 时渲染标题 + 崩溃时间 + 三按钮", async () => {
    const crashInfo = {
      timestamp: 1700000000000,
      time: "2023-11-14T22:13:20.000Z",
      file: "crash-1700000000000-renderer-gone_crashed.log",
      path: "/tmp/crash-reports/crash-1700000000000.log",
      size: 1024,
    };
    mockGetLastCrash.mockResolvedValue({ ok: true, crash: crashInfo });

    render(<CrashRecoveryDialog />);
    await waitFor(() => {
      expect(screen.getByText("crashRecovery.title")).toBeInTheDocument();
    });

    // 标题 + 描述 + 崩溃时间标签
    expect(screen.getByText("crashRecovery.description")).toBeInTheDocument();
    expect(screen.getByText(/crashRecovery\.crashTime/)).toBeInTheDocument();
    // 三按钮
    expect(screen.getByText("crashRecovery.relaunch")).toBeInTheDocument();
    expect(screen.getByText("crashRecovery.openDir")).toBeInTheDocument();
    expect(screen.getByText("crashRecovery.dismiss")).toBeInTheDocument();
  });

  it("点击忽略按钮调用 dismissCrash 并隐藏对话框", async () => {
    const crashInfo = {
      timestamp: 1700000000000,
      time: "2023-11-14T22:13:20.000Z",
      file: "crash-1.log",
      path: "/tmp/crash-1.log",
      size: 100,
    };
    mockGetLastCrash.mockResolvedValue({ ok: true, crash: crashInfo });
    mockDismissCrash.mockResolvedValue({ ok: true });

    const { container } = render(<CrashRecoveryDialog />);
    await waitFor(() => {
      expect(screen.getByText("crashRecovery.dismiss")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByText("crashRecovery.dismiss"));
    await waitFor(() => expect(mockDismissCrash).toHaveBeenCalled());
    // dismissCrash resolve 后组件隐藏
    await waitFor(() => {
      expect(container.firstChild).toBeNull();
    });
  });

  it("点击重启按钮调用 relaunchApp", async () => {
    const crashInfo = {
      timestamp: 1700000000000,
      time: "2023-11-14T22:13:20.000Z",
      file: "crash-1.log",
      path: "/tmp/crash-1.log",
      size: 100,
    };
    mockGetLastCrash.mockResolvedValue({ ok: true, crash: crashInfo });
    mockRelaunchApp.mockResolvedValue({ ok: true });

    render(<CrashRecoveryDialog />);
    await waitFor(() => {
      expect(screen.getByText("crashRecovery.relaunch")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByText("crashRecovery.relaunch"));
    await waitFor(() => expect(mockRelaunchApp).toHaveBeenCalled());
  });

  it("点击打开目录按钮调用 openCrashDir + dismissCrash", async () => {
    const crashInfo = {
      timestamp: 1700000000000,
      time: "2023-11-14T22:13:20.000Z",
      file: "crash-1.log",
      path: "/tmp/crash-1.log",
      size: 100,
    };
    mockGetLastCrash.mockResolvedValue({ ok: true, crash: crashInfo });
    mockOpenCrashDir.mockResolvedValue({ ok: true });
    mockDismissCrash.mockResolvedValue({ ok: true });

    const { container } = render(<CrashRecoveryDialog />);
    await waitFor(() => {
      expect(screen.getByText("crashRecovery.openDir")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByText("crashRecovery.openDir"));
    await waitFor(() => expect(mockOpenCrashDir).toHaveBeenCalled());
    await waitFor(() => expect(mockDismissCrash).toHaveBeenCalled());
    // dismissCrash 后组件隐藏
    await waitFor(() => {
      expect(container.firstChild).toBeNull();
    });
  });

  it("window.yuncat 不存在时不报错", async () => {
    delete (window as unknown as { yuncat?: unknown }).yuncat;
    const { container } = render(<CrashRecoveryDialog />);
    // 无 yuncat 时 effect 直接 return，组件保持 null
    expect(container.firstChild).toBeNull();
  });
});
