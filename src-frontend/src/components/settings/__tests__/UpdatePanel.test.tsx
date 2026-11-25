import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { act } from "react";

// Mock useToast——UpdatePanel 依赖 ToastContext
const mockToast = vi.fn();
vi.mock("../../../hooks/use-toast", () => ({
  useToast: () => ({ toast: mockToast }),
}));

import { UpdatePanel, isVerificationError } from "../UpdatePanel";

// Phase 94: UpdatePanel 测试——mock window.yuncat IPC 验证状态机 + 校验失败文案
describe("isVerificationError 纯函数", () => {
  it("sha512 关键字判定为校验失败", () => {
    expect(isVerificationError("sha512 verification failed")).toBe(true);
  });
  it("checksum 关键字判定为校验失败（大小写不敏感）", () => {
    expect(isVerificationError("Checksum mismatch")).toBe(true);
  });
  it("signature 关键字判定为校验失败", () => {
    expect(isVerificationError("signature is invalid")).toBe(true);
  });
  it("hash mismatch 关键字判定为校验失败", () => {
    expect(isVerificationError("hash mismatch detected")).toBe(true);
  });
  it("integrity 关键字判定为校验失败", () => {
    expect(isVerificationError("integrity check failed")).toBe(true);
  });
  it("网络错误不是校验失败", () => {
    expect(isVerificationError("network error")).toBe(false);
  });
  it("空字符串不是校验失败", () => {
    expect(isVerificationError("")).toBe(false);
  });
  it("非字符串返回 false", () => {
    expect(isVerificationError(null as unknown as string)).toBe(false);
    expect(isVerificationError(undefined as unknown as string)).toBe(false);
  });
});

describe("UpdatePanel", () => {
  const mockCheckForUpdates = vi.fn();
  const mockDownloadUpdate = vi.fn();
  const mockInstallUpdate = vi.fn();
  let onUpdateAvailableCb: ((data: unknown) => void) | null = null;
  let onUpdateNotAvailableCb: ((data: unknown) => void) | null = null;
  let onUpdateDownloadedCb: ((data: unknown) => void) | null = null;
  let onUpdateErrorCb:
    | ((data: { message: string; isVerificationError: boolean }) => void)
    | null = null;
  let onDownloadProgressCb: ((data: { percent: number }) => void) | null = null;

  beforeEach(() => {
    vi.clearAllMocks();
    onUpdateAvailableCb = null;
    onUpdateNotAvailableCb = null;
    onUpdateDownloadedCb = null;
    onUpdateErrorCb = null;
    onDownloadProgressCb = null;
    (window as unknown as { yuncat: unknown }).yuncat = {
      checkForUpdates: mockCheckForUpdates,
      downloadUpdate: mockDownloadUpdate,
      installUpdate: mockInstallUpdate,
      onUpdateAvailable: (cb: (data: unknown) => void) => {
        onUpdateAvailableCb = cb;
        return () => {};
      },
      onUpdateNotAvailable: (cb: (data: unknown) => void) => {
        onUpdateNotAvailableCb = cb;
        return () => {};
      },
      onUpdateDownloaded: (cb: (data: unknown) => void) => {
        onUpdateDownloadedCb = cb;
        return () => {};
      },
      onUpdateError: (
        cb: (data: { message: string; isVerificationError: boolean }) => void,
      ) => {
        onUpdateErrorCb = cb;
        return () => {};
      },
      onDownloadProgress: (cb: (data: { percent: number }) => void) => {
        onDownloadProgressCb = cb;
        return () => {};
      },
    };
  });

  // ── 渲染 ──────────────────────────────────────────

  it("渲染标题 + 描述 + 检查更新按钮", () => {
    render(<UpdatePanel />);
    expect(screen.getByText("update.title")).toBeInTheDocument();
    expect(screen.getByText("update.desc")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "update.checkNow" })).toBeInTheDocument();
  });

  // ── 检查更新流程 ──────────────────────────────────

  it("点击检查更新 → 调 checkForUpdates → 显示 checking 状态", async () => {
    // 不 resolve 事件，让状态停留在 checking
    mockCheckForUpdates.mockReturnValue(new Promise(() => {}));
    render(<UpdatePanel />);
    fireEvent.click(screen.getByRole("button", { name: "update.checkNow" }));
    await waitFor(() => expect(mockCheckForUpdates).toHaveBeenCalled());
    expect(screen.getByText("update.checking")).toBeInTheDocument();
  });

  it("检查后无更新（onUpdateNotAvailable）→ 显示已是最新", async () => {
    mockCheckForUpdates.mockResolvedValue({ ok: true });
    render(<UpdatePanel />);
    fireEvent.click(screen.getByRole("button", { name: "update.checkNow" }));
    await waitFor(() => expect(mockCheckForUpdates).toHaveBeenCalled());
    act(() => onUpdateNotAvailableCb?.({}));
    await waitFor(() => expect(screen.getByText("update.noUpdate")).toBeInTheDocument());
  });

  it("检查后有更新（onUpdateAvailable 含 version）→ 显示新版本 + 下载按钮", async () => {
    mockCheckForUpdates.mockResolvedValue({ ok: true });
    render(<UpdatePanel />);
    fireEvent.click(screen.getByRole("button", { name: "update.checkNow" }));
    await waitFor(() => expect(mockCheckForUpdates).toHaveBeenCalled());
    act(() => onUpdateAvailableCb?.({ version: "0.2.0" }));
    await waitFor(() =>
      expect(screen.getByText("update.available")).toBeInTheDocument(),
    );
    expect(
      screen.getByRole("button", { name: "update.downloadInstall" }),
    ).toBeInTheDocument();
  });

  it("非 packaged 环境 → 提示 notPackaged", async () => {
    mockCheckForUpdates.mockResolvedValue({ ok: false, error: "not packaged" });
    render(<UpdatePanel />);
    fireEvent.click(screen.getByRole("button", { name: "update.checkNow" }));
    await waitFor(() => expect(mockToast).toHaveBeenCalledWith("update.notPackaged", "error"));
  });

  // ── 下载流程 ──────────────────────────────────────

  it("点击下载并安装 → 调 downloadUpdate → 显示下载进度", async () => {
    mockCheckForUpdates.mockResolvedValue({ ok: true });
    mockDownloadUpdate.mockReturnValue(new Promise(() => {}));
    render(<UpdatePanel />);
    fireEvent.click(screen.getByRole("button", { name: "update.checkNow" }));
    await waitFor(() => expect(mockCheckForUpdates).toHaveBeenCalled());
    act(() => onUpdateAvailableCb?.({ version: "0.2.0" }));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "update.downloadInstall" })).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByRole("button", { name: "update.downloadInstall" }));
    await waitFor(() => expect(mockDownloadUpdate).toHaveBeenCalled());
    expect(screen.getByText("update.downloading")).toBeInTheDocument();
    // 进度事件更新 percent
    act(() => onDownloadProgressCb?.({ percent: 42 }));
    await waitFor(() => expect(screen.getByText("42%")).toBeInTheDocument());
  });

  it("下载完成（onUpdateDownloaded）→ 显示重启安装按钮", async () => {
    mockCheckForUpdates.mockResolvedValue({ ok: true });
    mockDownloadUpdate.mockResolvedValue({ ok: true });
    render(<UpdatePanel />);
    fireEvent.click(screen.getByRole("button", { name: "update.checkNow" }));
    await waitFor(() => expect(mockCheckForUpdates).toHaveBeenCalled());
    act(() => onUpdateAvailableCb?.({ version: "0.2.0" }));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "update.downloadInstall" })).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByRole("button", { name: "update.downloadInstall" }));
    await waitFor(() => expect(mockDownloadUpdate).toHaveBeenCalled());
    act(() => onUpdateDownloadedCb?.({}));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "update.restartInstall" })).toBeInTheDocument(),
    );
  });

  it("点击重启安装 → 调 installUpdate", async () => {
    mockCheckForUpdates.mockResolvedValue({ ok: true });
    mockDownloadUpdate.mockResolvedValue({ ok: true });
    mockInstallUpdate.mockResolvedValue({ ok: true });
    render(<UpdatePanel />);
    fireEvent.click(screen.getByRole("button", { name: "update.checkNow" }));
    await waitFor(() => expect(mockCheckForUpdates).toHaveBeenCalled());
    act(() => onUpdateAvailableCb?.({ version: "0.2.0" }));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "update.downloadInstall" })).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByRole("button", { name: "update.downloadInstall" }));
    await waitFor(() => expect(mockDownloadUpdate).toHaveBeenCalled());
    act(() => onUpdateDownloadedCb?.({}));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "update.restartInstall" })).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByRole("button", { name: "update.restartInstall" }));
    await waitFor(() => expect(mockInstallUpdate).toHaveBeenCalled());
  });

  // ── 错误处理 ──────────────────────────────────────

  it("校验失败错误（isVerificationError=true）→ 显示校验失败专门文案", async () => {
    mockCheckForUpdates.mockResolvedValue({ ok: true });
    render(<UpdatePanel />);
    fireEvent.click(screen.getByRole("button", { name: "update.checkNow" }));
    await waitFor(() => expect(mockCheckForUpdates).toHaveBeenCalled());
    act(() =>
      onUpdateErrorCb?.({ message: "sha512 verification failed", isVerificationError: true }),
    );
    await waitFor(() => expect(screen.getByText("update.verifyFailed")).toBeInTheDocument());
    expect(screen.getByText("update.verifyFailedDesc")).toBeInTheDocument();
  });

  it("通用错误（isVerificationError=false）→ 显示通用错误文案 + 错误消息", async () => {
    mockCheckForUpdates.mockResolvedValue({ ok: true });
    render(<UpdatePanel />);
    fireEvent.click(screen.getByRole("button", { name: "update.checkNow" }));
    await waitFor(() => expect(mockCheckForUpdates).toHaveBeenCalled());
    act(() =>
      onUpdateErrorCb?.({ message: "network timeout", isVerificationError: false }),
    );
    await waitFor(() => expect(screen.getByText("update.error")).toBeInTheDocument());
    expect(screen.getByText("network timeout")).toBeInTheDocument();
  });

  it("checkForUpdates 抛异常 → 显示错误状态", async () => {
    mockCheckForUpdates.mockRejectedValue(new Error("ipc crash"));
    render(<UpdatePanel />);
    fireEvent.click(screen.getByRole("button", { name: "update.checkNow" }));
    await waitFor(() => expect(screen.getByText("update.error")).toBeInTheDocument());
  });

  it("downloadUpdate 返回 ok:false → 显示错误状态", async () => {
    mockCheckForUpdates.mockResolvedValue({ ok: true });
    mockDownloadUpdate.mockResolvedValue({ ok: false, error: "disk full" });
    render(<UpdatePanel />);
    fireEvent.click(screen.getByRole("button", { name: "update.checkNow" }));
    await waitFor(() => expect(mockCheckForUpdates).toHaveBeenCalled());
    act(() => onUpdateAvailableCb?.({ version: "0.2.0" }));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "update.downloadInstall" })).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByRole("button", { name: "update.downloadInstall" }));
    await waitFor(() => expect(screen.getByText("update.error")).toBeInTheDocument());
    expect(screen.getByText("disk full")).toBeInTheDocument();
  });

  // ── 防御性 ────────────────────────────────────────

  it("window.yuncat 不存在时不报错", () => {
    delete (window as unknown as { yuncat?: unknown }).yuncat;
    const { container } = render(<UpdatePanel />);
    expect(container).toBeInTheDocument();
  });
});
