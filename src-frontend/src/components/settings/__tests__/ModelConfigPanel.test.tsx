import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";

// Mock useToast
const mockToast = vi.fn();
vi.mock("../../../hooks/use-toast", () => ({
  useToast: () => ({ toast: mockToast }),
}));

// Mock useApi——根据 path 返回不同 mock 数据
const mockApi = vi.fn();
vi.mock("../../../hooks/useApi", () => ({
  useApi: () => ({ api: mockApi }),
  ApiError: class ApiError extends Error {
    constructor(
      public readonly status: number,
      public readonly statusText: string,
      public readonly body: string,
      public readonly url: string,
    ) {
      super(statusText);
      this.name = "ApiError";
    }
  },
}));

// Mock ProviderIcon 避免复杂依赖
vi.mock("../../ProviderIcon", () => ({
  ProviderIcon: ({ size }: { size?: number }) => (
    <span data-testid="provider-icon" style={{ width: size, height: size }}>icon</span>
  ),
}));

// Mock LoadingCurve / EmptyState 避免额外依赖
vi.mock("../../common/LoadingCurve", () => ({
  LoadingCurve: () => <div data-testid="loading">loading</div>,
}));
vi.mock("../../common/EmptyState", () => ({
  EmptyState: ({ title, description, action }: { title?: string; description?: string; action?: { label: string } }) => (
    <div data-testid="empty">
      {title && <span data-testid="empty-title">{title}</span>}
      {description && <span data-testid="empty-desc">{description}</span>}
      {action && <button data-testid="empty-action">{action.label}</button>}
    </div>
  ),
}));

import { ModelConfigPanel, type ProviderInfo } from "../ModelConfigPanel";

// H3 测试：API key 轮换年龄徽章 + 提醒横幅
// 注意: vitest setup mock 了 useTranslation，t(key) 直接返回 key 字符串
// 所以 t("settings.keyRotation.days", {days:90}) 返回 "settings.keyRotation.days"

function makeProvider(overrides: Partial<ProviderInfo> = {}): ProviderInfo {
  return {
    provider: "test",
    name: "Test Provider",
    base_url: "https://api.test.com",
    model: "test-model",
    models: [{ id: "test-model", name: "Test Model", context: 8192 }],
    api_key_configured: true,
    ...overrides,
  };
}

describe("ModelConfigPanel H3: key 轮换年龄 UI", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // 默认 mock：/api/v1/models 返回空数组，/api/v1/models/active 返回 {provider:""}
    mockApi.mockImplementation(async (path: string) => {
      if (path === "/api/v1/models") return [];
      if (path === "/api/v1/models/active") return { provider: "" };
      return {};
    });
  });

  it("H3: rotation_status=ok 时渲染绿色年龄徽章", async () => {
    const provider = makeProvider({
      provider: "fresh",
      api_key_configured: true,
      key_age_days: 30,
      rotation_status: "ok",
    });
    mockApi.mockImplementation(async (path: string) => {
      if (path === "/api/v1/models") return [provider];
      if (path === "/api/v1/models/active") return { provider: "fresh" };
      return {};
    });

    render(<ModelConfigPanel />);
    // 等待表单渲染（settings.apiKey 是 API Key 标签，表单渲染后一定存在）
    await waitFor(() => {
      expect(screen.getByText("settings.apiKey")).toBeInTheDocument();
    });
    // H3: 年龄徽章渲染（t key 文本）
    expect(screen.getByText("settings.keyRotation.days")).toBeInTheDocument();
  });

  it("H3: rotation_status=warn 时渲染黄色徽章 + warn 横幅", async () => {
    const provider = makeProvider({
      provider: "aging",
      api_key_configured: true,
      key_age_days: 70,
      rotation_status: "warn",
    });
    mockApi.mockImplementation(async (path: string) => {
      if (path === "/api/v1/models") return [provider];
      if (path === "/api/v1/models/active") return { provider: "aging" };
      return {};
    });

    render(<ModelConfigPanel />);
    await waitFor(() => {
      expect(screen.getByText("settings.apiKey")).toBeInTheDocument();
    });
    // H3: 徽章
    expect(screen.getByText("settings.keyRotation.days")).toBeInTheDocument();
    // H3: warn 横幅（t key 文本）
    expect(screen.getByText("settings.keyRotation.warn")).toBeInTheDocument();
  });

  it("H3: rotation_status=expired 时渲染红色徽章 + expired 横幅", async () => {
    const provider = makeProvider({
      provider: "stale",
      api_key_configured: true,
      key_age_days: 100,
      rotation_status: "expired",
    });
    mockApi.mockImplementation(async (path: string) => {
      if (path === "/api/v1/models") return [provider];
      if (path === "/api/v1/models/active") return { provider: "stale" };
      return {};
    });

    render(<ModelConfigPanel />);
    await waitFor(() => {
      expect(screen.getByText("settings.apiKey")).toBeInTheDocument();
    });
    // H3: 徽章
    expect(screen.getByText("settings.keyRotation.days")).toBeInTheDocument();
    // H3: expired 横幅
    expect(screen.getByText("settings.keyRotation.expired")).toBeInTheDocument();
  });

  it("H3: rotation_status=disabled 时不渲染徽章也不渲染横幅", async () => {
    const provider = makeProvider({
      provider: "disabled",
      api_key_configured: true,
      key_age_days: 100,
      rotation_status: "disabled",
    });
    mockApi.mockImplementation(async (path: string) => {
      if (path === "/api/v1/models") return [provider];
      if (path === "/api/v1/models/active") return { provider: "disabled" };
      return {};
    });

    render(<ModelConfigPanel />);
    await waitFor(() => {
      expect(screen.getByText("settings.apiKey")).toBeInTheDocument();
    });
    // disabled 状态不渲染徽章（无 settings.keyRotation.days 文本）
    expect(screen.queryByText("settings.keyRotation.days")).not.toBeInTheDocument();
    // 也不渲染横幅
    expect(screen.queryByText("settings.keyRotation.warn")).not.toBeInTheDocument();
    expect(screen.queryByText("settings.keyRotation.expired")).not.toBeInTheDocument();
  });

  it("H3: rotation_status=null（旧库未迁移）时不渲染徽章", async () => {
    const provider = makeProvider({
      provider: "legacy",
      api_key_configured: true,
      key_age_days: null,
      rotation_status: null,
    });
    mockApi.mockImplementation(async (path: string) => {
      if (path === "/api/v1/models") return [provider];
      if (path === "/api/v1/models/active") return { provider: "legacy" };
      return {};
    });

    render(<ModelConfigPanel />);
    await waitFor(() => {
      expect(screen.getByText("settings.apiKey")).toBeInTheDocument();
    });
    // 无徽章
    expect(screen.queryByText("settings.keyRotation.days")).not.toBeInTheDocument();
    // 无横幅
    expect(screen.queryByText("settings.keyRotation.warn")).not.toBeInTheDocument();
    expect(screen.queryByText("settings.keyRotation.expired")).not.toBeInTheDocument();
  });

  it("H3: 未配置 key 的 provider 不渲染徽章", async () => {
    const provider = makeProvider({
      provider: "nokey",
      api_key_configured: false,
      // 未配置时无 key_age_days / rotation_status
    });
    delete (provider as Partial<ProviderInfo>).key_age_days;
    delete (provider as Partial<ProviderInfo>).rotation_status;
    mockApi.mockImplementation(async (path: string) => {
      if (path === "/api/v1/models") return [provider];
      if (path === "/api/v1/models/active") return { provider: "nokey" };
      return {};
    });

    render(<ModelConfigPanel />);
    await waitFor(() => {
      expect(screen.getByText("settings.apiKey")).toBeInTheDocument();
    });
    // 无徽章
    expect(screen.queryByText("settings.keyRotation.days")).not.toBeInTheDocument();
  });

  it("H3: 切换 provider 时徽章/横幅跟随更新", async () => {
    const providers: ProviderInfo[] = [
      makeProvider({
        provider: "ok-prov",
        name: "OK Provider",
        api_key_configured: true,
        key_age_days: 10,
        rotation_status: "ok",
      }),
      makeProvider({
        provider: "expired-prov",
        name: "Expired Provider",
        api_key_configured: true,
        key_age_days: 200,
        rotation_status: "expired",
      }),
    ];
    mockApi.mockImplementation(async (path: string) => {
      if (path === "/api/v1/models") return providers;
      if (path === "/api/v1/models/active") return { provider: "ok-prov" };
      return {};
    });

    render(<ModelConfigPanel />);
    // 默认选中 ok-prov，等待表单渲染
    await waitFor(() => {
      expect(screen.getByText("settings.apiKey")).toBeInTheDocument();
    });
    // 初始: ok 徽章在，无 expired 横幅
    expect(screen.getByText("settings.keyRotation.days")).toBeInTheDocument();
    expect(screen.queryByText("settings.keyRotation.expired")).not.toBeInTheDocument();

    // 点击左侧列表中的 Expired Provider（button 的 accessible name 含 provider 名）
    const expiredBtn = screen.getByRole("button", { name: /Expired Provider/ });
    fireEvent.click(expiredBtn);

    // 切换后: expired 横幅出现
    await waitFor(() => {
      expect(screen.getByText("settings.keyRotation.expired")).toBeInTheDocument();
    });
  });
});

describe("ModelConfigPanel P0: 保存即激活", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockApi.mockImplementation(async (path: string) => {
      if (path === "/api/v1/models") return [];
      if (path === "/api/v1/models/active") return { provider: "" };
      return {};
    });
  });

  it("保存后自动调用 activate 接口", async () => {
    const provider = makeProvider({ provider: "deepseek", api_key_configured: false });
    mockApi.mockImplementation(async (path: string) => {
      if (path === "/api/v1/models") return [provider];
      if (path === "/api/v1/models/active") return { provider: "" };
      return {};
    });

    render(<ModelConfigPanel />);
    await waitFor(() => {
      expect(screen.getByText("settings.apiKey")).toBeInTheDocument();
    });

    // 输入 API Key 使保存按钮可用
    const keyInput = screen.getByPlaceholderText("settings.apiKeyPlaceholder");
    fireEvent.change(keyInput, { target: { value: "sk-test" } });

    fireEvent.click(screen.getByText("common.save"));

    await waitFor(() => {
      expect(mockApi).toHaveBeenCalledWith("/api/v1/models/deepseek", {
        method: "PUT",
        body: JSON.stringify({ api_key: "sk-test" }),
      });
      expect(mockApi).toHaveBeenCalledWith("/api/v1/models/deepseek/activate", { method: "POST" });
    });
    expect(mockToast).toHaveBeenCalledWith("settings.savedAndActivated", "success");
  });

  it("保存失败时显示 API 失败提示", async () => {
    const provider = makeProvider({ provider: "deepseek", api_key_configured: false });
    mockApi.mockImplementation(async (path: string) => {
      if (path === "/api/v1/models") return [provider];
      if (path === "/api/v1/models/active") return { provider: "" };
      if (path === "/api/v1/models/deepseek") throw new Error("bad request");
      return {};
    });

    render(<ModelConfigPanel />);
    await waitFor(() => {
      expect(screen.getByText("settings.apiKey")).toBeInTheDocument();
    });

    const keyInput = screen.getByPlaceholderText("settings.apiKeyPlaceholder");
    fireEvent.change(keyInput, { target: { value: "sk-test" } });
    fireEvent.click(screen.getByText("common.save"));

    await waitFor(() => {
      expect(mockToast).toHaveBeenCalledWith(expect.stringContaining("settings.saveFailed"), "error");
    });
  });
});

describe("ModelConfigPanel P0: 测试后自动保存", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockApi.mockImplementation(async (path: string) => {
      if (path === "/api/v1/models") return [];
      if (path === "/api/v1/models/active") return { provider: "" };
      return {};
    });
  });

  it("连接测试通过后自动保存并激活", async () => {
    const provider = makeProvider({ provider: "deepseek", api_key_configured: false });
    mockApi.mockImplementation(async (path: string, options?: RequestInit) => {
      if (path === "/api/v1/models") return [provider];
      if (path === "/api/v1/models/active") return { provider: "" };
      if (path === "/api/v1/models/test") return { ok: true, status: 200 };
      if (path === "/api/v1/models/deepseek" && options?.method === "PUT") return { status: "ok" };
      if (path === "/api/v1/models/deepseek/activate" && options?.method === "POST") return { status: "ok" };
      return {};
    });

    render(<ModelConfigPanel />);
    await waitFor(() => {
      expect(screen.getByText("settings.apiKey")).toBeInTheDocument();
    });

    const keyInput = screen.getByPlaceholderText("settings.apiKeyPlaceholder");
    fireEvent.change(keyInput, { target: { value: "sk-test" } });
    fireEvent.click(screen.getByText("settings.test"));

    await waitFor(() => {
      expect(mockApi).toHaveBeenCalledWith("/api/v1/models/test", expect.anything());
      expect(mockApi).toHaveBeenCalledWith("/api/v1/models/deepseek", {
        method: "PUT",
        body: JSON.stringify({ api_key: "sk-test" }),
      });
      expect(mockApi).toHaveBeenCalledWith("/api/v1/models/deepseek/activate", { method: "POST" });
    });
    expect(mockToast).toHaveBeenCalledWith("settings.testedAndSaved", "success");
  });

  it("连接测试失败后提示错误", async () => {
    const provider = makeProvider({ provider: "deepseek", api_key_configured: false });
    mockApi.mockImplementation(async (path: string) => {
      if (path === "/api/v1/models") return [provider];
      if (path === "/api/v1/models/active") return { provider: "" };
      if (path === "/api/v1/models/test") return { ok: false, status: 401, error: "invalid key" };
      return {};
    });

    render(<ModelConfigPanel />);
    await waitFor(() => {
      expect(screen.getByText("settings.apiKey")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByText("settings.test"));

    await waitFor(() => {
      expect(mockToast).toHaveBeenCalledWith(expect.stringContaining("settings.connectionFailed"), "error");
    });
  });
});

describe("ModelConfigPanel P0: backend 未就绪文案", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("后端未就绪时显示重试空状态", async () => {
    mockApi.mockImplementation(async (path: string) => {
      if (path === "/api/v1/models") {
        throw new (await import("../../../hooks/useApi")).ApiError(0, "Backend not ready", "", path);
      }
      return {};
    });

    render(<ModelConfigPanel />);
    await waitFor(() => {
      expect(screen.getByTestId("empty-title")).toHaveTextContent("settings.backendNotReady");
    });
    expect(screen.getByTestId("empty-desc")).toHaveTextContent("settings.backendNotReadyDesc");
    expect(screen.getByTestId("empty-action")).toHaveTextContent("common.retry");
  });
});

// ── 定价编辑器：模型列表 ∪ 已有定价 ──────────────────────────────────────
// 回归背景：定价区块原先只渲染「已存在定价条目」的模型，于是零定价的厂商
// （如 hunyuan 的 5 个模型）整个区块被隐藏，用户**没有任何入口**补价——
// 而保存逻辑本身支持新增 model id，卡住的只是渲染。
describe("ModelConfigPanel 定价编辑器覆盖范围", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockApi.mockImplementation(async (path: string) => {
      if (path === "/api/v1/models") return [];
      if (path === "/api/v1/models/active") return { provider: "" };
      return {};
    });
  });

  it("零定价的厂商仍渲染出每个模型的定价输入框", async () => {
    const provider = makeProvider({
      provider: "hunyuan",
      models: [
        { id: "hy3", name: "Hy3", context: 196608 },
        { id: "hunyuan-t1", name: "Hunyuan T1", context: 262144 },
      ],
      pricing: {},
    });
    mockApi.mockImplementation(async (path: string) => {
      if (path === "/api/v1/models") return [provider];
      if (path === "/api/v1/models/active") return { provider: "hunyuan" };
      return {};
    });

    render(<ModelConfigPanel />);
    await waitFor(() => {
      expect(screen.getByText("settings.pricingSummary")).toBeInTheDocument();
    });
    expect(screen.getByLabelText("hy3 input price")).toBeInTheDocument();
    expect(screen.getByLabelText("hunyuan-t1 input price")).toBeInTheDocument();
  });

  it("已下线模型的历史定价不会因不在 models 列表而消失", async () => {
    const provider = makeProvider({
      provider: "legacy",
      models: [{ id: "new-one", name: "New", context: 8192 }],
      pricing: { "old-one": { input: 1, output: 2, currency: "USD" } },
    });
    mockApi.mockImplementation(async (path: string) => {
      if (path === "/api/v1/models") return [provider];
      if (path === "/api/v1/models/active") return { provider: "legacy" };
      return {};
    });

    render(<ModelConfigPanel />);
    await waitFor(() => {
      expect(screen.getByText("settings.pricingSummary")).toBeInTheDocument();
    });
    expect(screen.getByLabelText("new-one input price")).toBeInTheDocument();
    expect(screen.getByLabelText("old-one input price")).toBeInTheDocument();
  });
});
