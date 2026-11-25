import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, cleanup } from "@testing-library/react";

const mockApi = vi.fn();

vi.mock("../../hooks/useApi", () => ({
  useApi: () => ({ api: mockApi }),
  ApiError: class ApiError extends Error {},
}));

import { OnboardingWizard } from "../OnboardingWizard";

/**
 * Batch 4b P2: Onboarding 模型配置强制校验
 *
 * 1. Step2 输入未保存的 API Key 点 Next → 自动触发保存（PUT + activate），
 *    成功才切步——Key 不再静默丢失
 * 2. 保存失败 → 阻止切步并显示错误（复用 testResult 错误展示）
 * 3. 完成步骤：无任何已配置可用模型 → 显示 noModelWarning 警告区块
 */
const PROVIDERS = [
  {
    provider: "deepseek",
    name: "DeepSeek",
    base_url: "https://api.deepseek.com",
    model: "deepseek-chat",
    api_key_configured: false,
  },
];

function ok(val: unknown) {
  return Promise.resolve(val);
}

describe("Batch 4b P2: OnboardingWizard 模型配置校验", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    mockApi.mockImplementation((url: string) => {
      if (url === "/api/v1/models") return ok([...PROVIDERS]);
      return ok({});
    });
    const root = document.getElementById("overlay-root") || document.createElement("div");
    root.id = "overlay-root";
    document.body.appendChild(root);
  });

  afterEach(() => {
    cleanup();
    document.getElementById("overlay-root")?.remove();
  });

  function renderAtStep(step: number) {
    localStorage.setItem("yuncat-onboarding-step", String(step));
    return render(<OnboardingWizard />);
  }

  it("Step2 输入未保存 Key 点 Next → 自动 PUT+activate 成功后切步", async () => {
    renderAtStep(2);

    const keyInput = await screen.findByPlaceholderText("onboard.apiKeyPlaceholder");
    fireEvent.change(keyInput, { target: { value: "sk-test-123" } });
    fireEvent.click(screen.getByRole("button", { name: "onboard.next" }));

    // 切步动画 360ms 后进入 Step3（完成步骤——2026-09-10 移除 Agent 选择步骤，默认原生）
    await waitFor(
      () => expect(screen.getByText("onboard.ready")).toBeInTheDocument(),
      { timeout: 3000 },
    );

    const put = mockApi.mock.calls.find((c) => c[0] === "/api/v1/models/deepseek");
    expect(put).toBeDefined();
    expect(put![1].method).toBe("PUT");
    expect(JSON.parse(put![1].body).api_key).toBe("sk-test-123");

    const activate = mockApi.mock.calls.find(
      (c) => c[0] === "/api/v1/models/deepseek/activate",
    );
    expect(activate).toBeDefined();
    expect(activate![1].method).toBe("POST");
  });

  it("保存失败 → 阻止切步并显示错误", async () => {
    mockApi.mockImplementation((url: string) => {
      if (url === "/api/v1/models") return ok([...PROVIDERS]);
      if (url.startsWith("/api/v1/models/deepseek")) {
        return Promise.reject(new Error("boom"));
      }
      return ok({});
    });

    renderAtStep(2);

    const keyInput = await screen.findByPlaceholderText("onboard.apiKeyPlaceholder");
    fireEvent.change(keyInput, { target: { value: "sk-bad" } });
    fireEvent.click(screen.getByRole("button", { name: "onboard.next" }));

    await waitFor(() =>
      expect(screen.getByText("settings.saveFailed: boom")).toBeInTheDocument(),
    );
    // 仍停留在 Step2——未切步
    expect(screen.getByText("onboard.configKey")).toBeInTheDocument();
    expect(screen.queryByText("onboard.ready")).toBeNull();
  });

  it("Step2 已保存的 Key（再次点击 Next）不重复保存直接切步", async () => {
    renderAtStep(2);

    const keyInput = await screen.findByPlaceholderText("onboard.apiKeyPlaceholder");
    fireEvent.change(keyInput, { target: { value: "sk-once" } });
    fireEvent.click(screen.getByRole("button", { name: "onboard.next" }));

    await waitFor(
      () => expect(screen.getByText("onboard.ready")).toBeInTheDocument(),
      { timeout: 3000 },
    );
    const putCalls = mockApi.mock.calls.filter(
      (c) => c[0] === "/api/v1/models/deepseek" && c[1]?.method === "PUT",
    );
    expect(putCalls.length).toBe(1);
  });

  it("完成步骤：无任何已配置模型 → 显示 noModelWarning 警告", async () => {
    renderAtStep(3);

    await waitFor(() =>
      expect(screen.getByText("onboard.noModelWarning")).toBeInTheDocument(),
    );
    expect(screen.getByText("onboard.ready")).toBeInTheDocument();
  });

  it("完成步骤：已有配置模型 → 不显示警告", async () => {
    mockApi.mockImplementation((url: string) => {
      if (url === "/api/v1/models") {
        return ok([{ ...PROVIDERS[0], api_key_configured: true }]);
      }
      return ok({});
    });

    renderAtStep(3);

    await waitFor(() => expect(screen.getByText("onboard.ready")).toBeInTheDocument());
    expect(screen.queryByText("onboard.noModelWarning")).toBeNull();
  });
});
