/**
 * CostPanel.test.tsx —— 任务用量面板测试（2026-09-10 去价格化后口径）
 *
 * 验证：
 * 1. 有数据时渲染聚合卡片（调用次数/总 Token/缓存命中），**不渲染任何成本**
 * 2. 有数据时渲染明细表行（按 metrics 数量），列为 Token/缓存命中
 * 3. 无数据时显示空态（call_count=0）
 * 4. cached_tokens / reasoning_tokens 为可选字段——缺失时显示 0 而非 NaN/undefined
 *
 * 背景：厂商没有"逐次用量查询"API，token 统计走本地账本；价格统计因口径
 * 从未对上（恒为 $0）且厂商定价漂移而移除。
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { CostPanel } from "../CostPanel";

const mockApi = vi.fn();

vi.mock("../../hooks/useApi", () => ({
  useApi: () => ({ api: mockApi }),
}));

vi.mock("react-i18next", () => {
  const stableT = (key: string, options?: Record<string, unknown>) => {
    if (options && typeof options.count === "number") return `${key}:${options.count}`;
    return String(key);
  };
  return {
    useTranslation: () => ({ t: stableT, i18n: { language: "zh" } }),
    initReactI18next: { type: "3rdParty", init: vi.fn() },
  };
});

const SUMMARY_WITH_DATA = {
  call_count: 2,
  total_duration_ms: 2000,
  total_tokens: 980,
  total_prompt_tokens: 800,
  total_completion_tokens: 180,
  total_cached_tokens: 600,
  total_reasoning_tokens: 90,
};

const METRICS_WITH_DATA = {
  metrics: [
    {
      round: 1,
      model: "deepseek-v4-flash",
      agent_id: "yuncat",
      duration_ms: 1200,
      prompt_tokens: 500,
      completion_tokens: 100,
      total_tokens: 600,
      cached_tokens: 400,
      reasoning_tokens: 60,
    },
    {
      round: 2,
      model: "deepseek-v4-flash",
      agent_id: "yuncat",
      duration_ms: 800,
      prompt_tokens: 300,
      completion_tokens: 80,
      total_tokens: 380,
      cached_tokens: 200,
      reasoning_tokens: 30,
    },
  ],
};

const SUMMARY_EMPTY = {
  call_count: 0,
  total_duration_ms: 0,
  total_tokens: 0,
  total_prompt_tokens: 0,
  total_completion_tokens: 0,
};

describe("CostPanel（token 用量口径）", () => {
  beforeEach(() => {
    mockApi.mockReset();
  });

  it("渲染聚合卡片（调用次数/总 Token/缓存命中），不渲染任何成本", async () => {
    mockApi.mockImplementation((path: string) => {
      if (path.includes("llm-summary")) return Promise.resolve(SUMMARY_WITH_DATA);
      if (path.includes("llm-metrics")) return Promise.resolve(METRICS_WITH_DATA);
      return Promise.resolve({});
    });

    render(<CostPanel taskId="t1" />);

    await waitFor(() => {
      expect(screen.getByText("cost.callCount")).toBeInTheDocument();
    });
    // token 聚合卡片（toLocaleString 千分位；980 只出现在卡片）
    expect(screen.getByText("980")).toBeInTheDocument();
    // 缓存命中 600 同时出现在聚合卡片与明细行 → 至少存在即可
    expect(screen.getAllByText("600").length).toBeGreaterThanOrEqual(1);
    // 去价格化：任何 $ 金额都不应出现
    expect(screen.queryByText(/^\$/)).not.toBeInTheDocument();
    expect(screen.queryByText("cost.totalCost")).not.toBeInTheDocument();
  });

  it("渲染 Token 细分（输入/输出/推理）", async () => {
    mockApi.mockImplementation((path: string) => {
      if (path.includes("llm-summary")) return Promise.resolve(SUMMARY_WITH_DATA);
      if (path.includes("llm-metrics")) return Promise.resolve(METRICS_WITH_DATA);
      return Promise.resolve({});
    });

    render(<CostPanel taskId="t1" />);
    // 标签 span 的文本是 "cost.reasoningTokens: "（带冒号空格）→ 用正则匹配
    await waitFor(() => {
      expect(screen.getByText(/cost\.reasoningTokens/)).toBeInTheDocument();
    });
    expect(screen.getByText("90")).toBeInTheDocument(); // 推理 token 合计
  });

  it("明细表含 Token/缓存命中列，无成本列", async () => {
    mockApi.mockImplementation((path: string) => {
      if (path.includes("llm-summary")) return Promise.resolve(SUMMARY_WITH_DATA);
      if (path.includes("llm-metrics")) return Promise.resolve(METRICS_WITH_DATA);
      return Promise.resolve({});
    });

    render(<CostPanel taskId="t1" />);
    await waitFor(() => {
      expect(screen.getAllByText("deepseek-v4-flash")).toHaveLength(2);
    });
    expect(screen.getByText("cost.colTokens")).toBeInTheDocument();
    expect(screen.getByText("cost.colCached")).toBeInTheDocument();
    expect(screen.queryByText("cost.colCost")).not.toBeInTheDocument();
  });

  it("cached/reasoning 缺失时显示 0 而非 NaN/undefined", async () => {
    mockApi.mockImplementation((path: string) => {
      if (path.includes("llm-summary")) {
        // 模拟旧版后端 / 旧 token_cost 事件：没有细分字段
        return Promise.resolve({ ...SUMMARY_WITH_DATA, total_cached_tokens: undefined, total_reasoning_tokens: undefined });
      }
      if (path.includes("llm-metrics")) {
        return Promise.resolve({
          metrics: METRICS_WITH_DATA.metrics.map(({ cached_tokens: _c, reasoning_tokens: _r, ...m }) => m),
        });
      }
      return Promise.resolve({});
    });

    render(<CostPanel taskId="t1" />);
    await waitFor(() => {
      expect(screen.getByText("cost.cachedTokens")).toBeInTheDocument();
    });
    // 缺失字段补 0（多处为 0 → 至少存在一个）
    expect(screen.getAllByText("0").length).toBeGreaterThanOrEqual(1);
    expect(screen.queryByText(/NaN|undefined/)).not.toBeInTheDocument();
  });

  it("无数据时显示空态（call_count=0）", async () => {
    mockApi.mockImplementation((path: string) => {
      if (path.includes("llm-summary")) return Promise.resolve(SUMMARY_EMPTY);
      if (path.includes("llm-metrics")) return Promise.resolve({ metrics: [] });
      return Promise.resolve({});
    });

    render(<CostPanel taskId="t1" />);
    await waitFor(() => {
      expect(screen.getByText("cost.empty")).toBeInTheDocument();
    });
  });
});
