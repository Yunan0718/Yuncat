import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

// Mock useToast
const mockToast = vi.fn();
vi.mock("../../hooks/use-toast", () => ({
  useToast: () => ({ toast: mockToast }),
}));

// Mock useTranslation——文件级覆盖 setup.ts，返回稳定 t 引用
// Task 6.6: setup.ts 的 mock 每次 useTranslation() 返回新 t 函数（箭头函数在工厂内定义），
// 导致 useCallback 依赖 t 时不稳定 → loadLogs 引用每次渲染都变。
// 生产环境 react-i18next 的 t 是稳定的（内部 useMemo），此处 mock 对齐生产行为。
vi.mock("react-i18next", () => {
  const stableT = (key: string, options?: Record<string, unknown>) => {
    if (options && typeof options.returnObjects === "boolean") return [];
    return String(key);
  };
  return {
    useTranslation: () => ({ t: stableT, i18n: { language: "zh" } }),
    initReactI18next: { type: "3rdParty", init: vi.fn() },
  };
});

// Mock useAppStore——AuditLogPanel 从 store 读 port + token
vi.mock("../../store/appStore", () => ({
  useAppStore: (selector: (s: { port: number; token: string }) => unknown) =>
    selector({ port: 1420, token: "test-token" }),
}));

import { AuditLogPanel } from "../AuditLogPanel";

// H4: AuditLogPanel 测试——mock fetch 验证加载/过滤/刷新/空态全流程
describe("AuditLogPanel", () => {
  let mockFetch: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    vi.clearAllMocks();
    mockFetch = vi.fn();
    vi.stubGlobal("fetch", mockFetch);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  // ── 初始加载 ──────────────────────────────────────────────

  it("挂载时自动调 /api/v1/audit-log 加载日志", async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      json: async () => ({ logs: [], count: 0, server_ts: 1000, query: { limit: 100, tool_name: "", since_ts: 0 } }),
    });
    render(<AuditLogPanel />);
    await waitFor(() => expect(mockFetch).toHaveBeenCalled());
    const [url] = mockFetch.mock.calls[0];
    expect(url).toContain("/api/v1/audit-log");
    expect(url).toContain("limit=100");
  });

  it("空日志时显示 empty 提示", async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      json: async () => ({ logs: [], count: 0, server_ts: 1000, query: { limit: 100, tool_name: "", since_ts: 0 } }),
    });
    render(<AuditLogPanel />);
    await waitFor(() => expect(mockFetch).toHaveBeenCalled());
    await waitFor(() => expect(screen.getByText("audit.empty")).toBeInTheDocument());
  });

  // ── 渲染日志 ──────────────────────────────────────────────

  it("渲染日志表格——时间/Agent/工具/阶段/结果/参数", async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      json: async () => ({
        logs: [
          {
            id: 1, ts: 1700000000, agent_id: "hermes", tool_name: "run_command",
            args_summary: "{'command': 'dir'}", success: 1, phase: "execute",
            session_id: "s1", task_id: "t1",
          },
        ],
        count: 1,
        server_ts: 1700000001,
        query: { limit: 100, tool_name: "", since_ts: 0 },
      }),
    });
    render(<AuditLogPanel />);
    await waitFor(() => expect(mockFetch).toHaveBeenCalled());
    // 工具名渲染
    await waitFor(() => expect(screen.getByText("run_command")).toBeInTheDocument());
    // Agent 渲染
    expect(screen.getByText("hermes")).toBeInTheDocument();
    // 阶段徽章
    expect(screen.getByText("execute")).toBeInTheDocument();
    // 参数摘要
    expect(screen.getByText(/dir/)).toBeInTheDocument();
  });

  it("success=1 显示绿色 ✓ 图标（CheckCircle2）", async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      json: async () => ({
        logs: [{
          id: 1, ts: 1700000000, agent_id: "hermes", tool_name: "run_command",
          args_summary: "{}", success: 1, phase: "execute", session_id: "", task_id: "",
        }],
        count: 1, server_ts: 1700000001,
        query: { limit: 100, tool_name: "", since_ts: 0 },
      }),
    });
    render(<AuditLogPanel />);
    await waitFor(() => expect(mockFetch).toHaveBeenCalled());
    // 表格中应有一个 CheckCircle2 图标（class 含 text-success）
    await waitFor(() => {
      const icon = document.querySelector(".text-success");
      expect(icon).not.toBeNull();
    });
  });

  it("success=0 显示红色 ✗ 图标（XCircle）", async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      json: async () => ({
        logs: [{
          id: 2, ts: 1700000000, agent_id: "openclaw", tool_name: "write_file",
          args_summary: "{}", success: 0, phase: "execute", session_id: "", task_id: "",
        }],
        count: 1, server_ts: 1700000001,
        query: { limit: 100, tool_name: "", since_ts: 0 },
      }),
    });
    render(<AuditLogPanel />);
    await waitFor(() => expect(mockFetch).toHaveBeenCalled());
    await waitFor(() => {
      const icon = document.querySelector(".text-destructive");
      expect(icon).not.toBeNull();
    });
  });

  it("blocked 阶段显示红色徽章", async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      json: async () => ({
        logs: [{
          id: 3, ts: 1700000000, agent_id: "hermes", tool_name: "run_command",
          args_summary: "{}", success: 0, phase: "blocked", session_id: "", task_id: "",
        }],
        count: 1, server_ts: 1700000001,
        query: { limit: 100, tool_name: "", since_ts: 0 },
      }),
    });
    render(<AuditLogPanel />);
    await waitFor(() => expect(mockFetch).toHaveBeenCalled());
    // blocked 阶段徽章应有 destructive 配色
    await waitFor(() => expect(screen.getByText("blocked")).toBeInTheDocument());
    const badge = screen.getByText("blocked").closest("span");
    expect(badge?.className).toContain("destructive");
  });

  // ── 工具名过滤 ──────────────────────────────────────────────

  it("输入 tool_name 过滤会带 tool_name 查询参数", async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      json: async () => ({
        logs: [{
          id: 1, ts: 1700000000, agent_id: "hermes", tool_name: "run_command",
          args_summary: "{}", success: 1, phase: "execute", session_id: "", task_id: "",
        }],
        count: 1, server_ts: 1700000001,
        query: { limit: 100, tool_name: "", since_ts: 0 },
      }),
    });
    render(<AuditLogPanel />);
    await waitFor(() => expect(mockFetch).toHaveBeenCalled());

    const input = screen.getByPlaceholderText("audit.filterPlaceholder");
    fireEvent.change(input, { target: { value: "run_command" } });

    await waitFor(() => {
      const calls = mockFetch.mock.calls;
      const lastUrl = calls[calls.length - 1][0] as string;
      expect(lastUrl).toContain("tool_name=run_command");
    });
  });

  // ── 刷新 ──────────────────────────────────────────────────

  it("点击刷新按钮触发重新加载", async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      json: async () => ({
        logs: [{
          id: 1, ts: 1700000000, agent_id: "hermes", tool_name: "run_command",
          args_summary: "{}", success: 1, phase: "execute", session_id: "", task_id: "",
        }],
        count: 1, server_ts: 1700000001,
        query: { limit: 100, tool_name: "", since_ts: 0 },
      }),
    });
    render(<AuditLogPanel />);
    await waitFor(() => expect(mockFetch).toHaveBeenCalled());
    const initialCallCount = mockFetch.mock.calls.length;

    // 点击刷新按钮（toolbar 中的 refresh 按钮 title=common.refresh）
    const refreshBtn = screen.getByTitle("common.refresh");
    fireEvent.click(refreshBtn);

    await waitFor(() => {
      expect(mockFetch.mock.calls.length).toBeGreaterThan(initialCallCount);
    });
  });

  // ── 错误处理 ──────────────────────────────────────────────

  it("fetch 抛异常时显示错误状态 + toast", async () => {
    mockFetch.mockRejectedValue(new Error("network error"));
    render(<AuditLogPanel />);
    await waitFor(() => expect(mockFetch).toHaveBeenCalled());
    await waitFor(() => expect(mockToast).toHaveBeenCalledWith("audit.loadFailed", "error"));
    // 错误状态显示
    await waitFor(() => expect(screen.getByText(/audit\.loadFailed/)).toBeInTheDocument());
    // 重试按钮
    expect(screen.getByText("common.retry")).toBeInTheDocument();
  });

  it("db_not_initialized error 不显示错误状态（视为空）", async () => {
    // 后端 DB 未初始化时返回 error=db_not_initialized——前端应作为空态处理，不显示错误
    mockFetch.mockResolvedValue({
      ok: true,
      json: async () => ({
        logs: [], count: 0, error: "db_not_initialized",
        query: { limit: 100, tool_name: "", since_ts: 0 },
      }),
    });
    render(<AuditLogPanel />);
    await waitFor(() => expect(mockFetch).toHaveBeenCalled());
    // 应显示 empty 状态，而非 error 状态
    await waitFor(() => expect(screen.getByText("audit.empty")).toBeInTheDocument());
  });

  // ── 状态栏 ──────────────────────────────────────────────

  it("状态栏显示记录条数", async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      json: async () => ({
        logs: [
          { id: 1, ts: 1700000000, agent_id: "h", tool_name: "t1", args_summary: "", success: 1, phase: "execute", session_id: "", task_id: "" },
          { id: 2, ts: 1700000001, agent_id: "h", tool_name: "t2", args_summary: "", success: 0, phase: "execute", session_id: "", task_id: "" },
        ],
        count: 2, server_ts: 1700000002,
        query: { limit: 100, tool_name: "", since_ts: 0 },
      }),
    });
    render(<AuditLogPanel />);
    await waitFor(() => expect(mockFetch).toHaveBeenCalled());
    // 状态栏显示 "2 audit.entries"
    await waitFor(() => expect(screen.getByText(/2\s+audit\.entries/)).toBeInTheDocument());
  });

  it("自动刷新关闭时状态栏显示 autoRefreshOff", async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      json: async () => ({
        logs: [{
          id: 1, ts: 1700000000, agent_id: "hermes", tool_name: "run_command",
          args_summary: "{}", success: 1, phase: "execute", session_id: "", task_id: "",
        }],
        count: 1, server_ts: 1700000001,
        query: { limit: 100, tool_name: "", since_ts: 0 },
      }),
    });
    render(<AuditLogPanel />);
    await waitFor(() => expect(mockFetch).toHaveBeenCalled());
    // 初始 autoRefresh=false
    await waitFor(() => expect(screen.getByText("audit.autoRefreshOff")).toBeInTheDocument());
  });

  it("点击自动刷新按钮切换为开", async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      json: async () => ({
        logs: [{
          id: 1, ts: 1700000000, agent_id: "hermes", tool_name: "run_command",
          args_summary: "{}", success: 1, phase: "execute", session_id: "", task_id: "",
        }],
        count: 1, server_ts: 1700000001,
        query: { limit: 100, tool_name: "", since_ts: 0 },
      }),
    });
    render(<AuditLogPanel />);
    await waitFor(() => expect(mockFetch).toHaveBeenCalled());

    const autoRefreshBtn = screen.getByTitle("audit.autoRefresh");
    fireEvent.click(autoRefreshBtn);

    await waitFor(() => expect(screen.getByText("audit.autoRefreshOn")).toBeInTheDocument());
  });

  // ── Task 6.6: interval 重建修复 ──────────────────────────

  it("Task 6.6: autoRefresh 开启后 logs 更新不重建 setInterval", async () => {
    // 精确跟踪 delay=AUTO_REFRESH_INTERVAL_MS(5000) 的 timer。
    // 全局 spy 会捕获 @testing-library/waitFor 内部的 50ms 轮询 timer，
    // 它在 waitFor 完成后会 clearInterval，与我们的 useEffect 无关——需过滤。
    const realSetInterval = global.setInterval.bind(global);
    const realClearInterval = global.clearInterval.bind(global);
    // jsdom 环境下 setInterval 返回 number，@types/node 下返回 NodeJS.Timeout——
    // 两者类型冲突，用 unknown 存储 + 断言调用，避免 TS 推断歧义。
    const trackedIds = new Set<unknown>();
    let setIntervalCount = 0;
    let clearIntervalCount = 0;

    vi.spyOn(global, "setInterval").mockImplementation(((fn: TimerHandler, delay?: number) => {
      const id = realSetInterval(fn, delay) as unknown;
      if (delay === 5000) {
        setIntervalCount++;
        trackedIds.add(id);
      }
      return id;
    }) as typeof setInterval);
    vi.spyOn(global, "clearInterval").mockImplementation(((id?: unknown) => {
      if (id !== undefined && trackedIds.has(id)) {
        clearIntervalCount++;
        trackedIds.delete(id);
      }
      return realClearInterval(id as Parameters<typeof realClearInterval>[0]);
    }) as typeof clearInterval);

    mockFetch.mockResolvedValue({
      ok: true,
      json: async () => ({
        logs: [{ id: 1, ts: 1700000000, agent_id: "h", tool_name: "t1", args_summary: "", success: 1, phase: "execute", session_id: "", task_id: "" }],
        count: 1, server_ts: 1700000001,
        query: { limit: 100, tool_name: "", since_ts: 0 },
      }),
    });

    render(<AuditLogPanel />);
    await waitFor(() => expect(mockFetch).toHaveBeenCalled());

    // 开启 autoRefresh → useEffect 设置 setInterval(5000)
    fireEvent.click(screen.getByTitle("audit.autoRefresh"));
    await waitFor(() => expect(screen.getByText("audit.autoRefreshOn")).toBeInTheDocument());

    // autoRefresh 开启后：5000ms interval 设置 1 次，未清理
    expect(setIntervalCount).toBe(1);
    expect(clearIntervalCount).toBe(0);

    const baselineSet = setIntervalCount;
    const baselineClear = clearIntervalCount;

    // 触发 logs 变化：点击 refresh 按钮 → loadLogs(false) → setLogs(新数组) → logs 状态更新
    fireEvent.click(screen.getByTitle("common.refresh"));
    await waitFor(() => expect(mockFetch.mock.calls.length).toBeGreaterThanOrEqual(2));

    // 修复后：logs 变化不影响 loadLogs 引用 → useEffect [autoRefresh, loadLogs] 不重跑
    // → 5000ms interval 不被再次设置，对应 clearInterval 不被调用（interval 不重建）
    expect(setIntervalCount).toBe(baselineSet);
    expect(clearIntervalCount).toBe(baselineClear);

    vi.restoreAllMocks();
  });
});
