import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

// Mock useToast——ErrorLogPanel 用 toast(message, type) 形式
const mockToast = vi.fn();
vi.mock("../../hooks/use-toast", () => ({
  useToast: () => ({ toast: mockToast }),
}));

// Mock useTranslation——稳定 t 引用（与 AuditLogPanel.test.tsx 一致）
vi.mock("react-i18next", () => {
  const stableT = (key: string, options?: Record<string, unknown>) => {
    if (options && typeof options.returnObjects === "boolean") return [];
    if (options && typeof options.count === "number") {
      return `${key}:${options.count}`;
    }
    return String(key);
  };
  return {
    useTranslation: () => ({ t: stableT, i18n: { language: "zh" } }),
    initReactI18next: { type: "3rdParty", init: vi.fn() },
  };
});

// Mock useAppStore——ErrorLogPanel 从 store 读 port + token
vi.mock("../../store/appStore", () => ({
  useAppStore: (selector: (s: { port: number; token: string }) => unknown) =>
    selector({ port: 1420, token: "test-token" }),
}));

import { ErrorLogPanel } from "../ErrorLogPanel";

// Batch 8.6: ErrorLogPanel 测试——mock fetch 验证加载/过滤/刷新/展开/清空全流程
describe("ErrorLogPanel", () => {
  let mockFetch: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    vi.clearAllMocks();
    mockFetch = vi.fn();
    vi.stubGlobal("fetch", mockFetch);
    // window.confirm 默认返回 true
    vi.spyOn(window, "confirm").mockReturnValue(true);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  // ── 初始加载 ──────────────────────────────────────────────

  it("挂载时自动调 /api/v1/errors 加载错误事件", async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      json: async () => ({ errors: [], count: 0, total: 0, server_ts: 1000 }),
    });
    render(<ErrorLogPanel />);
    await waitFor(() => expect(mockFetch).toHaveBeenCalled());
    const [url] = mockFetch.mock.calls[0];
    expect(url).toContain("/api/v1/errors");
    expect(url).toContain("limit=50");
  });

  it("空列表时显示 empty 提示", async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      json: async () => ({ errors: [], count: 0, total: 0, server_ts: 1000 }),
    });
    render(<ErrorLogPanel />);
    await waitFor(() => expect(mockFetch).toHaveBeenCalled());
    await waitFor(() => expect(screen.getByText("errorLog.empty")).toBeInTheDocument());
  });

  // ── 渲染错误表格 ──────────────────────────────────────────────

  it("渲染错误表格——时间/状态码/错误码/方法/路径", async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      json: async () => ({
        errors: [
          {
            id: 1, error_code: "NOT_FOUND", status_code: 404, message: "Not found",
            path: "/api/v1/tasks/abc", method: "GET", trace_id: "t1", task_id: "tk1",
            agent_id: "hermes", stack_trace: "", created_at: 1700000000,
          },
        ],
        count: 1, total: 1, server_ts: 1700000001,
      }),
    });
    render(<ErrorLogPanel />);
    await waitFor(() => expect(mockFetch).toHaveBeenCalled());
    await waitFor(() => expect(screen.getByText("NOT_FOUND")).toBeInTheDocument());
    expect(screen.getByText("404")).toBeInTheDocument();
    expect(screen.getByText("GET")).toBeInTheDocument();
    expect(screen.getByText("/api/v1/tasks/abc")).toBeInTheDocument();
  });

  it("5xx 状态码显示红色徽章（destructive 配色）", async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      json: async () => ({
        errors: [
          {
            id: 1, error_code: "INTERNAL_ERROR", status_code: 500, message: "boom",
            path: "/api/v1/test", method: "POST", trace_id: "", task_id: "", agent_id: "",
            stack_trace: "Traceback (most recent call last):\n  File ...", created_at: 1700000000,
          },
        ],
        count: 1, total: 1, server_ts: 1700000001,
      }),
    });
    render(<ErrorLogPanel />);
    await waitFor(() => expect(mockFetch).toHaveBeenCalled());
    await waitFor(() => expect(screen.getByText("500")).toBeInTheDocument());
    const badge = screen.getByText("500").closest("span");
    expect(badge?.className).toContain("destructive");
  });

  it("4xx 状态码显示琥珀色徽章（amber 配色）", async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      json: async () => ({
        errors: [
          {
            id: 2, error_code: "BAD_REQUEST", status_code: 400, message: "bad",
            path: "/api/v1/x", method: "GET", trace_id: "", task_id: "", agent_id: "",
            stack_trace: "", created_at: 1700000000,
          },
        ],
        count: 1, total: 1, server_ts: 1700000001,
      }),
    });
    render(<ErrorLogPanel />);
    await waitFor(() => expect(mockFetch).toHaveBeenCalled());
    await waitFor(() => expect(screen.getByText("400")).toBeInTheDocument());
    const badge = screen.getByText("400").closest("span");
    expect(badge?.className).toContain("amber");
  });

  it("5xx 行可点击展开显示 stack_trace", async () => {
    const stack = "Traceback (most recent call last):\n  File 'x.py' line 1";
    mockFetch.mockResolvedValue({
      ok: true,
      json: async () => ({
        errors: [
          {
            id: 1, error_code: "INTERNAL_ERROR", status_code: 500, message: "boom",
            path: "/api/v1/test", method: "POST", trace_id: "", task_id: "", agent_id: "",
            stack_trace: stack, created_at: 1700000000,
          },
        ],
        count: 1, total: 1, server_ts: 1700000001,
      }),
    });
    render(<ErrorLogPanel />);
    await waitFor(() => expect(mockFetch).toHaveBeenCalled());
    await waitFor(() => expect(screen.getByText("500")).toBeInTheDocument());

    // 初始 stack_trace 不显示
    expect(screen.queryByText(stack)).not.toBeInTheDocument();

    // 点击行展开
    const row = screen.getByText("500").closest("tr");
    if (row) fireEvent.click(row);

    // 展开后 <pre> 应包含 stack_trace 内容（用 toHaveTextContent 避免多行文本匹配歧义）
    await waitFor(() => {
      const pre = document.querySelector("pre");
      expect(pre).not.toBeNull();
      expect(pre).toHaveTextContent("File 'x.py' line 1");
    });
  });

  // ── level 过滤 ──────────────────────────────────────────────

  it("切换 level 过滤下拉框触发重新加载（带 level=4xx 参数）", async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      json: async () => ({ errors: [], count: 0, total: 0, server_ts: 1000 }),
    });
    render(<ErrorLogPanel />);
    await waitFor(() => expect(mockFetch).toHaveBeenCalled());

    // select 通过 role=combobox 定位（getByDisplayValue 对 select 匹配 option 文本，语义易混淆）
    const select = screen.getByRole("combobox");
    fireEvent.change(select, { target: { value: "4xx" } });

    await waitFor(() => {
      const calls = mockFetch.mock.calls;
      const lastUrl = calls[calls.length - 1][0] as string;
      expect(lastUrl).toContain("level=4xx");
    });
  });

  // ── 清空 ──────────────────────────────────────────────────

  it("点击清空按钮触发 window.confirm，确认后调 DELETE", async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      json: async () => ({
        errors: [
          {
            id: 1, error_code: "BAD_REQUEST", status_code: 400, message: "bad",
            path: "/x", method: "GET", trace_id: "", task_id: "", agent_id: "",
            stack_trace: "", created_at: 1700000000,
          },
        ],
        count: 1, total: 1, server_ts: 1700000001,
      }),
    });
    render(<ErrorLogPanel />);
    await waitFor(() => expect(mockFetch).toHaveBeenCalled());

    const clearBtn = screen.getByText("errorLog.clear");
    fireEvent.click(clearBtn);

    expect(window.confirm).toHaveBeenCalledWith("errorLog.confirmClear");

    await waitFor(() => {
      const deleteCall = mockFetch.mock.calls.find(
        (c) => typeof c[1] === "object" && c[1]?.method === "DELETE",
      );
      expect(deleteCall).toBeDefined();
      expect(deleteCall![0]).toContain("confirm=true");
    });
  });

  it("window.confirm 取消时不调 DELETE", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(false);
    mockFetch.mockResolvedValue({
      ok: true,
      json: async () => ({
        errors: [
          {
            id: 1, error_code: "BAD_REQUEST", status_code: 400, message: "bad",
            path: "/x", method: "GET", trace_id: "", task_id: "", agent_id: "",
            stack_trace: "", created_at: 1700000000,
          },
        ],
        count: 1, total: 1, server_ts: 1700000001,
      }),
    });
    render(<ErrorLogPanel />);
    await waitFor(() => expect(mockFetch).toHaveBeenCalled());

    const clearBtn = screen.getByText("errorLog.clear");
    fireEvent.click(clearBtn);

    const deleteCall = mockFetch.mock.calls.find(
      (c) => typeof c[1] === "object" && c[1]?.method === "DELETE",
    );
    expect(deleteCall).toBeUndefined();
  });

  // ── db_not_initialized ──────────────────────────────────────

  it("db_not_initialized error 不显示错误状态（视为空）", async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      json: async () => ({ errors: [], count: 0, total: 0, error: "db_not_initialized" }),
    });
    render(<ErrorLogPanel />);
    await waitFor(() => expect(mockFetch).toHaveBeenCalled());
    // 应显示 empty 状态，而非 error 状态
    await waitFor(() => expect(screen.getByText("errorLog.empty")).toBeInTheDocument());
  });

  // ── 刷新 ──────────────────────────────────────────────────

  it("点击刷新按钮触发重新加载", async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      json: async () => ({
        errors: [
          {
            id: 1, error_code: "BAD_REQUEST", status_code: 400, message: "bad",
            path: "/x", method: "GET", trace_id: "", task_id: "", agent_id: "",
            stack_trace: "", created_at: 1700000000,
          },
        ],
        count: 1, total: 1, server_ts: 1700000001,
      }),
    });
    render(<ErrorLogPanel />);
    await waitFor(() => expect(mockFetch).toHaveBeenCalled());
    const initialCallCount = mockFetch.mock.calls.length;

    const refreshBtn = screen.getByText("errorLog.refresh");
    fireEvent.click(refreshBtn);

    await waitFor(() => {
      expect(mockFetch.mock.calls.length).toBeGreaterThan(initialCallCount);
    });
  });
});
