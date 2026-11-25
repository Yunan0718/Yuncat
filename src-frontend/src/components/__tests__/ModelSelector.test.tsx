/**
 * 模型选择器——只列已配置的模型（2026-10-07 修复的回归守卫）
 *
 * 起因：16 家供应商全量铺开，未配置的灰名单把唯一能用的那个淹没了；
 * 用户明确要求「只显示已配置的模型」。折叠入口的计数还必须按
 * "折叠时会藏起多少"算——用可见列表反推会导致展开后按钮消失、无法收起。
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

const providers = [
  { provider: "deepseek", name: "DeepSeek", model: "deepseek-v4-flash", api_key_configured: true,
    health_status: "active", models: [{ id: "deepseek-v4-flash", name: "V4", context: 1000000 }] },
  { provider: "openai", name: "OpenAI", model: "gpt-5", api_key_configured: false,
    health_status: "unconfigured", models: [] },
  { provider: "anthropic", name: "Anthropic", model: "claude", api_key_configured: false,
    health_status: "unconfigured", models: [] },
];

vi.mock("../../hooks/useApi", () => ({
  useApi: () => ({
    api: vi.fn((path: string) => {
      if (path === "/api/v1/models") return Promise.resolve(providers);
      if (path === "/api/v1/models/active") return Promise.resolve(activeConfigRef.current);
      return Promise.resolve({});
    }),
  }),
}));

const { activeConfigRef } = vi.hoisted(() => ({
  activeConfigRef: { current: { provider: "deepseek" } as Record<string, unknown> },
}));

vi.mock("../../hooks/use-toast", () => ({ useToast: () => ({ toast: vi.fn() }) }));
vi.mock("../../hooks/useModelReasoning", () => ({
  useModelReasoning: () => ({ info: { effortSupported: false, efforts: [] } }),
}));

import ModelSelector from "../ModelSelector";

describe("ModelSelector：只显示已配置的模型", () => {
  beforeEach(() => {
    localStorage.clear();
    activeConfigRef.current = { provider: "deepseek" };
  });

  it("默认不渲染未配置的供应商", async () => {
    render(<ModelSelector />);
    await waitFor(() => expect(screen.getByText("DeepSeek")).toBeInTheDocument());
    fireEvent.click(screen.getByText("DeepSeek"));
    await waitFor(() => expect(screen.getByTestId("toggle-unconfigured")).toBeInTheDocument());
    // 已配置的可见；未配置的两家被折叠
    expect(screen.getAllByText("DeepSeek").length).toBeGreaterThan(0);
    expect(screen.queryByText("OpenAI")).not.toBeInTheDocument();
    expect(screen.queryByText("Anthropic")).not.toBeInTheDocument();
  });

  it("展开后才显示未配置项，且还能收回去", async () => {
    render(<ModelSelector />);
    await waitFor(() => expect(screen.getByText("DeepSeek")).toBeInTheDocument());
    fireEvent.click(screen.getByText("DeepSeek"));

    const toggle = await screen.findByTestId("toggle-unconfigured");
    fireEvent.click(toggle);
    await waitFor(() => expect(screen.getByText("OpenAI")).toBeInTheDocument());
    expect(screen.getByText("Anthropic")).toBeInTheDocument();

    // 关键回归点：展开后按钮不能消失（否则用户无法收起）
    const toggleAgain = screen.getByTestId("toggle-unconfigured");
    fireEvent.click(toggleAgain);
    await waitFor(() => expect(screen.queryByText("OpenAI")).not.toBeInTheDocument());
  });

  it("本机密钥失效时给出可执行提示", async () => {
    activeConfigRef.current = {
      provider: "deepseek", api_key_configured: false,
      credential_state: "missing", has_stored_ciphertext: true,
    };
    render(<ModelSelector />);
    await waitFor(() => expect(screen.getByTestId("credential-warning")).toBeInTheDocument());
  });

  it("只是没配 Key 时不弹本机密钥警告", async () => {
    activeConfigRef.current = {
      provider: "deepseek", api_key_configured: false,
      credential_state: "missing", has_stored_ciphertext: false,
    };
    render(<ModelSelector />);
    await waitFor(() => expect(screen.getByText("DeepSeek")).toBeInTheDocument());
    expect(screen.queryByTestId("credential-warning")).toBeNull();
  });
});
