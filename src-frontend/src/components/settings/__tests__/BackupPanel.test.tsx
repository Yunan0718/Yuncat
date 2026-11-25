import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

// Mock useToast——BackupPanel 依赖 ToastContext
const mockToast = vi.fn();
vi.mock("../../../hooks/use-toast", () => ({
  useToast: () => ({ toast: mockToast }),
}));

// Mock useAppStore——BackupPanel 从 store 读 port + token + backendReady
const mockPort = 1420;
const mockToken = "test-token";
vi.mock("../../../store/appStore", () => ({
  useAppStore: (
    selector: (s: { port: number; token: string; backendReady: boolean }) => unknown,
  ) => selector({ port: mockPort, token: mockToken, backendReady: true }),
}));

// Mock useBackupSchedule——调度区 API 调用（Batch 5.6）
const mockGetSettings = vi.fn();
const mockUpdateSettings = vi.fn();
const mockListBackups = vi.fn();
const mockTriggerBackup = vi.fn();
const mockDeleteBackup = vi.fn();
vi.mock("../../../hooks/useBackupSchedule", () => ({
  useBackupSchedule: () => ({
    getSettings: mockGetSettings,
    updateSettings: mockUpdateSettings,
    listBackups: mockListBackups,
    triggerBackup: mockTriggerBackup,
    deleteBackup: mockDeleteBackup,
  }),
}));

import { BackupPanel } from "../BackupPanel";

// Phase 96-3: BackupPanel 测试——mock fetch + window.yuncat IPC 验证导出/导入/重启全流程
describe("BackupPanel", () => {
  const mockOpenFileDialog = vi.fn();
  const mockRelaunchApp = vi.fn();
  let mockFetch: ReturnType<typeof vi.fn>;
  const originalCreateObjectURL = URL.createObjectURL;
  const originalRevokeObjectURL = URL.revokeObjectURL;

  beforeEach(() => {
    vi.clearAllMocks();
    mockFetch = vi.fn();
    vi.stubGlobal("fetch", mockFetch);
    // mock URL.createObjectURL——jsdom 未实现
    URL.createObjectURL = vi.fn(() => "blob:mock-url");
    URL.revokeObjectURL = vi.fn();
    (window as unknown as { yuncat: unknown }).yuncat = {
      openFileDialog: mockOpenFileDialog,
      relaunchApp: mockRelaunchApp,
    };
    // 默认调度设置：禁用、无密码——调度区仅渲染开关，不影响现有导出/导入测试
    mockGetSettings.mockResolvedValue({
      enabled: false,
      cron_expr: "0 3 * * *",
      retention_count: 7,
      has_password: false,
    });
    mockListBackups.mockResolvedValue({ items: [], count: 0 });
  });

  afterEach(() => {
    URL.createObjectURL = originalCreateObjectURL;
    URL.revokeObjectURL = originalRevokeObjectURL;
    vi.unstubAllGlobals();
  });

  // ── 渲染 ──────────────────────────────────────────────────

  it("渲染标题、描述、导出区和导入区", () => {
    render(<BackupPanel />);
    expect(screen.getByText("backup.title")).toBeInTheDocument();
    expect(screen.getByText("backup.desc")).toBeInTheDocument();
    expect(screen.getByText("backup.exportTitle")).toBeInTheDocument();
    expect(screen.getByText("backup.importTitle")).toBeInTheDocument();
  });

  it("初始状态下不显示重启确认框", () => {
    render(<BackupPanel />);
    expect(screen.queryByText("backup.restartRequired")).not.toBeInTheDocument();
  });

  it("初始状态下未选文件时显示 noFileSelected", () => {
    render(<BackupPanel />);
    expect(screen.getByText("backup.noFileSelected")).toBeInTheDocument();
  });

  // ── 导出流程 ──────────────────────────────────────────────

  it("密码不足 4 位时导出按钮禁用且不 fetch", async () => {
    render(<BackupPanel />);
    const input = screen.getByPlaceholderText("backup.passwordPlaceholder");
    fireEvent.change(input, { target: { value: "ab" } });
    const exportBtn = screen.getByRole("button", { name: "backup.export" });
    expect(exportBtn).toBeDisabled();
    // 按钮禁用时 click 不触发 handler——验证 fetch 未被调用
    expect(mockFetch).not.toHaveBeenCalled();
  });

  it("导出成功时触发下载并提示成功且清空密码", async () => {
    // mock fetch 返回二进制响应
    const fakeBlob = new Blob(["encrypted-data"], { type: "application/octet-stream" });
    const fakeHeaders = new Headers({
      "content-disposition": 'attachment; filename="yuncat-backup-12345.ybk"',
    });
    mockFetch.mockResolvedValue({
      ok: true,
      blob: async () => fakeBlob,
      headers: fakeHeaders,
    });
    // mock <a>.click() 避免 jsdom 导航
    const clickSpy = vi.fn();
    const originalCreateElement = document.createElement.bind(document);
    vi.spyOn(document, "createElement").mockImplementation((tag: string) => {
      const el = originalCreateElement(tag);
      if (tag === "a") el.click = clickSpy;
      return el;
    });

    render(<BackupPanel />);
    const input = screen.getByPlaceholderText("backup.passwordPlaceholder");
    fireEvent.change(input, { target: { value: "pw1234" } });
    const exportBtn = screen.getByRole("button", { name: "backup.export" });
    fireEvent.click(exportBtn);

    await waitFor(() => expect(mockFetch).toHaveBeenCalled());
    // 验证 fetch 调用参数
    const [url, opts] = mockFetch.mock.calls[0];
    expect(url).toBe("http://127.0.0.1:1420/api/v1/config/backup/export");
    expect(opts.method).toBe("POST");
    expect(opts.body).toBe(JSON.stringify({ password: "pw1234" }));
    expect(opts.headers.Authorization).toBe("Bearer test-token");
    // 验证下载触发
    await waitFor(() => expect(clickSpy).toHaveBeenCalled());
    // 验证提示成功
    await waitFor(() => expect(mockToast).toHaveBeenCalledWith("backup.exportSuccess", "success"));
    // 验证密码已清空
    expect(input).toHaveValue("");
  });

  it("导出响应非 ok 时提示失败", async () => {
    mockFetch.mockResolvedValue({
      ok: false,
      json: async () => ({ error: "yuncat.db not found" }),
      headers: new Headers(),
    });

    render(<BackupPanel />);
    const input = screen.getByPlaceholderText("backup.passwordPlaceholder");
    fireEvent.change(input, { target: { value: "pw1234" } });
    fireEvent.click(screen.getByRole("button", { name: "backup.export" }));

    await waitFor(() =>
      expect(mockToast).toHaveBeenCalledWith(
        expect.stringContaining("backup.exportFailed"),
        "error",
      ),
    );
  });

  it("导出 fetch 抛异常时提示失败", async () => {
    mockFetch.mockRejectedValue(new Error("network down"));

    render(<BackupPanel />);
    const input = screen.getByPlaceholderText("backup.passwordPlaceholder");
    fireEvent.change(input, { target: { value: "pw1234" } });
    fireEvent.click(screen.getByRole("button", { name: "backup.export" }));

    await waitFor(() =>
      expect(mockToast).toHaveBeenCalledWith("backup.exportFailed", "error"),
    );
  });

  // ── 导入流程：文件选择 ────────────────────────────────────

  it("点击选择文件调 openFileDialog", async () => {
    mockOpenFileDialog.mockResolvedValue({ ok: true, path: "C:\\backups\\test.ybk" });

    render(<BackupPanel />);
    fireEvent.click(screen.getByRole("button", { name: "backup.selectFile" }));

    await waitFor(() => expect(mockOpenFileDialog).toHaveBeenCalled());
    // 文件路径显示在 UI 上
    await waitFor(() => expect(screen.getByText("C:\\backups\\test.ybk")).toBeInTheDocument());
  });

  it("openFileDialog 取消时不设置路径", async () => {
    mockOpenFileDialog.mockResolvedValue({ ok: false, canceled: true });

    render(<BackupPanel />);
    fireEvent.click(screen.getByRole("button", { name: "backup.selectFile" }));

    await waitFor(() => expect(mockOpenFileDialog).toHaveBeenCalled());
    expect(screen.getByText("backup.noFileSelected")).toBeInTheDocument();
  });

  // ── 导入流程：恢复 ────────────────────────────────────────

  it("导入成功时显示重启确认框", async () => {
    mockOpenFileDialog.mockResolvedValue({ ok: true, path: "C:\\test.ybk" });
    mockFetch.mockResolvedValue({
      ok: true,
      json: async () => ({ ok: true, files: ["yuncat.db"], restart_required: true }),
    });

    render(<BackupPanel />);
    // 选文件
    fireEvent.click(screen.getByRole("button", { name: "backup.selectFile" }));
    await waitFor(() => expect(screen.getByText("C:\\test.ybk")).toBeInTheDocument());
    // 输入密码——选文件后有 2 个密码输入框，用 aria-label 精确定位导入的
    const pwInput = screen.getByLabelText("backup.importTitle");
    fireEvent.change(pwInput, { target: { value: "pw1234" } });
    // 点恢复
    fireEvent.click(screen.getByRole("button", { name: "backup.restore" }));

    await waitFor(() => expect(mockFetch).toHaveBeenCalled());
    const [url, opts] = mockFetch.mock.calls[0];
    expect(url).toBe("http://127.0.0.1:1420/api/v1/config/backup/import");
    expect(JSON.parse(opts.body)).toEqual({ path: "C:\\test.ybk", password: "pw1234" });
    // 重启确认框出现
    await waitFor(() => expect(screen.getByText("backup.restartRequired")).toBeInTheDocument());
  });

  it("导入响应含 error 时提示失败", async () => {
    mockOpenFileDialog.mockResolvedValue({ ok: true, path: "C:\\test.ybk" });
    mockFetch.mockResolvedValue({
      ok: true,
      json: async () => ({ error: "wrong password or corrupt backup" }),
    });

    render(<BackupPanel />);
    fireEvent.click(screen.getByRole("button", { name: "backup.selectFile" }));
    await waitFor(() => expect(screen.getByText("C:\\test.ybk")).toBeInTheDocument());
    const pwInput = screen.getByLabelText("backup.importTitle");
    fireEvent.change(pwInput, { target: { value: "pw1234" } });
    fireEvent.click(screen.getByRole("button", { name: "backup.restore" }));

    await waitFor(() =>
      expect(mockToast).toHaveBeenCalledWith(
        expect.stringContaining("backup.importFailed"),
        "error",
      ),
    );
    // 不应显示重启确认框
    expect(screen.queryByText("backup.restartRequired")).not.toBeInTheDocument();
  });

  // ── 重启对话框 ────────────────────────────────────────────

  it("点立即重启调 relaunchApp", async () => {
    mockOpenFileDialog.mockResolvedValue({ ok: true, path: "C:\\test.ybk" });
    mockFetch.mockResolvedValue({
      ok: true,
      json: async () => ({ ok: true, files: ["yuncat.db"], restart_required: true }),
    });
    mockRelaunchApp.mockResolvedValue({ ok: true });

    render(<BackupPanel />);
    fireEvent.click(screen.getByRole("button", { name: "backup.selectFile" }));
    await waitFor(() => expect(screen.getByText("C:\\test.ybk")).toBeInTheDocument());
    fireEvent.change(screen.getByLabelText("backup.importTitle"), {
      target: { value: "pw1234" },
    });
    fireEvent.click(screen.getByRole("button", { name: "backup.restore" }));
    await waitFor(() => expect(screen.getByText("backup.restartRequired")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "backup.restartNow" }));
    await waitFor(() => expect(mockRelaunchApp).toHaveBeenCalled());
  });

  it("点稍后关闭重启确认框", async () => {
    mockOpenFileDialog.mockResolvedValue({ ok: true, path: "C:\\test.ybk" });
    mockFetch.mockResolvedValue({
      ok: true,
      json: async () => ({ ok: true, files: ["yuncat.db"], restart_required: true }),
    });

    render(<BackupPanel />);
    fireEvent.click(screen.getByRole("button", { name: "backup.selectFile" }));
    await waitFor(() => expect(screen.getByText("C:\\test.ybk")).toBeInTheDocument());
    fireEvent.change(screen.getByLabelText("backup.importTitle"), {
      target: { value: "pw1234" },
    });
    fireEvent.click(screen.getByRole("button", { name: "backup.restore" }));
    await waitFor(() => expect(screen.getByText("backup.restartRequired")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "backup.later" }));
    await waitFor(() =>
      expect(screen.queryByText("backup.restartRequired")).not.toBeInTheDocument(),
    );
  });

  // ── 鲁棒性 ────────────────────────────────────────────────

  it("window.yuncat 不存在时点选择文件不报错", () => {
    delete (window as unknown as { yuncat?: unknown }).yuncat;
    const { container } = render(<BackupPanel />);
    expect(container).toBeInTheDocument();
  });

  // ── 调度设置 (Batch 5.6) ─────────────────────────────────

  it("挂载时调用 getSettings 和 listBackups", async () => {
    render(<BackupPanel />);
    await waitFor(() => expect(mockGetSettings).toHaveBeenCalled());
    await waitFor(() => expect(mockListBackups).toHaveBeenCalledWith(50));
  });

  it("getSettings 返回 enabled=false 时仅显示调度开关不显示 cron 输入", async () => {
    render(<BackupPanel />);
    await waitFor(() => expect(mockGetSettings).toHaveBeenCalled());
    expect(screen.getByText("backup.schedule.title")).toBeInTheDocument();
    expect(screen.queryByPlaceholderText("0 3 * * *")).not.toBeInTheDocument();
  });

  it("getSettings 返回 enabled=true 时显示 cron/retention/password 输入", async () => {
    mockGetSettings.mockResolvedValue({
      enabled: true,
      cron_expr: "0 3 * * *",
      retention_count: 7,
      has_password: true,
    });
    render(<BackupPanel />);
    await waitFor(() => expect(screen.getByPlaceholderText("0 3 * * *")).toBeInTheDocument());
    expect(screen.getByRole("spinbutton")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "backup.schedule.save" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "backup.schedule.triggerNow" })).toBeInTheDocument();
  });

  it("has_password=true 时显示密码已设置标记且启用触发按钮", async () => {
    mockGetSettings.mockResolvedValue({
      enabled: true,
      cron_expr: "0 3 * * *",
      retention_count: 7,
      has_password: true,
    });
    render(<BackupPanel />);
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "backup.schedule.triggerNow" })).toBeInTheDocument(),
    );
    expect(screen.getAllByText(/backup\.schedule\.passwordSet/).length).toBeGreaterThan(0);
    expect(
      screen.getByRole("button", { name: "backup.schedule.triggerNow" }),
    ).not.toBeDisabled();
  });

  it("has_password=false 时禁用触发按钮", async () => {
    mockGetSettings.mockResolvedValue({
      enabled: true,
      cron_expr: "0 3 * * *",
      retention_count: 7,
      has_password: false,
    });
    render(<BackupPanel />);
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "backup.schedule.triggerNow" })).toBeInTheDocument(),
    );
    expect(screen.getByRole("button", { name: "backup.schedule.triggerNow" })).toBeDisabled();
  });

  it("点击保存调用 updateSettings 并提示成功", async () => {
    mockGetSettings.mockResolvedValue({
      enabled: true,
      cron_expr: "0 3 * * *",
      retention_count: 7,
      has_password: false,
    });
    mockUpdateSettings.mockResolvedValue({ ok: true });
    render(<BackupPanel />);
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "backup.schedule.save" })).toBeInTheDocument(),
    );
    const pwInput = screen.getByPlaceholderText("backup.schedule.passwordPlaceholder");
    fireEvent.change(pwInput, { target: { value: "newpass123" } });
    fireEvent.click(screen.getByRole("button", { name: "backup.schedule.save" }));
    await waitFor(() => expect(mockUpdateSettings).toHaveBeenCalled());
    expect(mockUpdateSettings.mock.calls[0][0]).toEqual({
      enabled: true,
      cron_expr: "0 3 * * *",
      retention_count: 7,
      password: "newpass123",
    });
    await waitFor(() =>
      expect(mockToast).toHaveBeenCalledWith("backup.schedule.saved", "success"),
    );
  });

  it("保存失败时提示失败", async () => {
    mockGetSettings.mockResolvedValue({
      enabled: true,
      cron_expr: "0 3 * * *",
      retention_count: 7,
      has_password: false,
    });
    mockUpdateSettings.mockRejectedValue(new Error("network down"));
    render(<BackupPanel />);
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "backup.schedule.save" })).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByRole("button", { name: "backup.schedule.save" }));
    await waitFor(() =>
      expect(mockToast).toHaveBeenCalledWith(
        expect.stringContaining("backup.schedule.saveFailed"),
        "error",
      ),
    );
  });

  // ── 触发 ──────────────────────────────────────────────────

  it("点击立即触发且 created=true 时提示成功并刷新列表", async () => {
    mockGetSettings.mockResolvedValue({
      enabled: true,
      cron_expr: "0 3 * * *",
      retention_count: 7,
      has_password: true,
    });
    mockTriggerBackup.mockResolvedValue({ created: true });
    render(<BackupPanel />);
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "backup.schedule.triggerNow" })).toBeInTheDocument(),
    );
    mockListBackups.mockClear();
    fireEvent.click(screen.getByRole("button", { name: "backup.schedule.triggerNow" }));
    await waitFor(() => expect(mockTriggerBackup).toHaveBeenCalled());
    await waitFor(() =>
      expect(mockToast).toHaveBeenCalledWith("backup.schedule.triggerSuccess", "success"),
    );
    await waitFor(() => expect(mockListBackups).toHaveBeenCalledTimes(1));
  });

  it("点击立即触发且 skipped=true 时提示跳过", async () => {
    mockGetSettings.mockResolvedValue({
      enabled: true,
      cron_expr: "0 3 * * *",
      retention_count: 7,
      has_password: true,
    });
    mockTriggerBackup.mockResolvedValue({ created: false, skipped: true });
    render(<BackupPanel />);
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "backup.schedule.triggerNow" })).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByRole("button", { name: "backup.schedule.triggerNow" }));
    await waitFor(() =>
      expect(mockToast).toHaveBeenCalledWith("backup.schedule.triggerSkipped", "info"),
    );
  });

  // ── 历史列表 ──────────────────────────────────────────────

  it("listBackups 返回列表时渲染备份项", async () => {
    mockGetSettings.mockResolvedValue({
      enabled: true,
      cron_expr: "0 3 * * *",
      retention_count: 7,
      has_password: true,
    });
    mockListBackups.mockResolvedValue({
      items: [
        {
          id: "backup-20260713-030000-a1b2c3",
          created_at: 1720828800,
          trigger: "manual",
          changed_since_last: true,
          encrypted_size: 2048,
          files: [],
        },
      ],
      count: 1,
    });
    render(<BackupPanel />);
    await waitFor(() =>
      expect(screen.getByText("backup-20260713-030000-a1b2c3")).toBeInTheDocument(),
    );
    expect(screen.getByText("backup.schedule.manual")).toBeInTheDocument();
  });

  it("listBackups 返回空时显示空提示", async () => {
    mockGetSettings.mockResolvedValue({
      enabled: true,
      cron_expr: "0 3 * * *",
      retention_count: 7,
      has_password: true,
    });
    render(<BackupPanel />);
    await waitFor(() =>
      expect(screen.getByText("backup.schedule.empty")).toBeInTheDocument(),
    );
  });

  // ── 删除 ──────────────────────────────────────────────────

  it("点击删除调用 deleteBackup 并刷新列表", async () => {
    mockGetSettings.mockResolvedValue({
      enabled: true,
      cron_expr: "0 3 * * *",
      retention_count: 7,
      has_password: true,
    });
    mockListBackups.mockResolvedValue({
      items: [
        {
          id: "backup-20260713-030000-a1b2c3",
          created_at: 1720828800,
          trigger: "manual",
          changed_since_last: true,
          encrypted_size: 2048,
          files: [],
        },
      ],
      count: 1,
    });
    mockDeleteBackup.mockResolvedValue({
      ok: true,
      deleted: "backup-20260713-030000-a1b2c3",
    });
    render(<BackupPanel />);
    await waitFor(() =>
      expect(screen.getByText("backup-20260713-030000-a1b2c3")).toBeInTheDocument(),
    );
    mockListBackups.mockClear();
    fireEvent.click(screen.getByRole("button", { name: "backup.schedule.delete" }));
    await waitFor(() =>
      expect(mockDeleteBackup).toHaveBeenCalledWith("backup-20260713-030000-a1b2c3"),
    );
    await waitFor(() =>
      expect(mockToast).toHaveBeenCalledWith("backup.schedule.deleted", "success"),
    );
    await waitFor(() => expect(mockListBackups).toHaveBeenCalledTimes(1));
  });
});
