import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, cleanup } from "@testing-library/react";

// Mock useAppStore——可变 store 对象，测试中修改后 rerender 触发更新
const mockStore = {
  backendReady: false,
  backendDead: false,
  port: 1420,
  token: "test-token",
};

vi.mock("../../store/appStore", () => ({
  useAppStore: (selector: (s: typeof mockStore) => unknown) => selector(mockStore),
}));

import { StartupScreen } from "../StartupScreen";

const ANALYTICS_URL = "http://127.0.0.1:1420/api/v1/analytics/startup";

let mockFetch: ReturnType<typeof vi.fn>;

/** 在 mock fetch 调用记录中查找 analytics 上报调用 */
function findAnalyticsCall(): [string, RequestInit] | undefined {
  return (mockFetch.mock.calls as Array<[string, RequestInit]>).find(
    ([url]) => url === ANALYTICS_URL
  );
}

describe("StartupScreen", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // 每个测试前重置 store
    mockStore.backendReady = false;
    mockStore.backendDead = false;
    mockStore.port = 1420;
    mockStore.token = "test-token";
    mockFetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({}),
    });
    vi.stubGlobal("fetch", mockFetch);
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  // ── 渲染 ──────────────────────────────────────────────────

  it("渲染加载状态——显示品牌名与状态区（Phase 49-H 阶段归并为单行状态 + 进度条）", () => {
    render(<StartupScreen />);
    // 品牌名（t("chat.emptyTitle") 被 mock 返回 key 字符串）
    expect(screen.getByText("chat.emptyTitle")).toBeInTheDocument();
    expect(screen.getByText("app.subtitle")).toBeInTheDocument();
    // 阶段指示器已从逐个标签改为单行状态文案（startupMsg 为空时显示 stageStatus）
    expect(screen.getByText("startup.stageStatus")).toBeInTheDocument();
  });

  it("渲染副标题和耗时计数器", () => {
    render(<StartupScreen />);
    expect(screen.getByText("app.subtitle")).toBeInTheDocument();
    // 耗时计数器（t("startup.elapsed", { seconds: 0 })）
    expect(screen.getByText("startup.elapsed")).toBeInTheDocument();
  });

  it("backendDead=true 时显示错误状态和重试按钮", () => {
    mockStore.backendDead = true;
    render(<StartupScreen />);
    expect(screen.getByText("startup.failed")).toBeInTheDocument();
    expect(screen.getByText("startup.retry")).toBeInTheDocument();
    expect(screen.getByText("startup.troubleshooting")).toBeInTheDocument();
  });

  it("backendReady=true 时 return null——不渲染任何内容", () => {
    mockStore.backendReady = true;
    const { container } = render(<StartupScreen />);
    expect(container.firstChild).toBeNull();
  });

  // ── Phase 97: 启动耗时上报 ──────────────────────────────────

  it("backendReady 变 true 时 POST /api/v1/analytics/startup 上报前端启动耗时", async () => {
    const { rerender } = render(<StartupScreen />);

    // 初始 backendReady=false——不上报 analytics
    expect(findAnalyticsCall()).toBeUndefined();

    // backendReady 变 true
    mockStore.backendReady = true;
    rerender(<StartupScreen />);

    await waitFor(() => {
      expect(findAnalyticsCall()).toBeDefined();
    });

    const analyticsCall = findAnalyticsCall();
    expect(analyticsCall).toBeDefined();
    const opts = analyticsCall![1];
    expect(opts.method).toBe("POST");
    expect((opts.headers as Record<string, string>)["Content-Type"]).toBe("application/json");
    expect((opts.headers as Record<string, string>)["Authorization"]).toBe("Bearer test-token");

    const body = JSON.parse(opts.body as string);
    expect(body.frontend_ms).toBeGreaterThanOrEqual(0);
    expect(body.stages).toBeDefined();
    expect(body.stages.init).toBe(0); // 初始 stage 进入时间为 0
    expect(body.platform).toBeDefined();
  });

  it("上报失败时静默处理——不抛错不影响 UI", async () => {
    // 让 fetch reject——模拟网络错误
    mockFetch.mockImplementation((url: string) => {
      if (url.includes("/api/v1/analytics/startup")) {
        return Promise.reject(new Error("network error"));
      }
      return Promise.resolve({ ok: true, json: async () => ({}) });
    });

    const { rerender } = render(<StartupScreen />);
    mockStore.backendReady = true;
    rerender(<StartupScreen />);

    // 等待 fetch 被调用
    await waitFor(() => {
      expect(findAnalyticsCall()).toBeDefined();
    });
    // 组件正常 return null（无未捕获异常）
  });

  it("port 缺失时不上报 analytics", () => {
    mockStore.port = 0;
    mockStore.backendReady = true;
    render(<StartupScreen />);

    expect(findAnalyticsCall()).toBeUndefined();
  });

  it("token 缺失时不上报 analytics", () => {
    mockStore.token = "";
    mockStore.backendReady = true;
    render(<StartupScreen />);

    expect(findAnalyticsCall()).toBeUndefined();
  });

  it("stages 记录各阶段进入时间——init 为 0", async () => {
    const { rerender } = render(<StartupScreen />);
    mockStore.backendReady = true;
    rerender(<StartupScreen />);

    await waitFor(() => {
      expect(findAnalyticsCall()).toBeDefined();
    });

    const analyticsCall = findAnalyticsCall();
    const body = JSON.parse(analyticsCall![1].body as string);
    // init 阶段进入时间为 0（组件挂载时就是 init）
    expect(body.stages.init).toBe(0);
    // frontend_ms >= stages.init
    expect(body.frontend_ms).toBeGreaterThanOrEqual(body.stages.init);
  });
});
