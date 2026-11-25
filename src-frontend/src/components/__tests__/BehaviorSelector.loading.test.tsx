/**
 * Task 7.19: BehaviorSelector 加载态 bug 修复测试
 *
 * Bug: BehaviorSelector L125 硬编码 "Loading..." 字面量，未走 i18n。
 * 修复后：加载态应使用 LoadingCurve + i18n key（common.loading），
 * 不应再出现裸 "Loading..." 字符串。
 *
 * 测试策略：mock useApi 让 loading=true，渲染组件，断言：
 * 1. 不包含字面量 "Loading..."
 * 2. 包含 i18n key "common.loading"（setup.ts mock 直接返回 key 字符串）
 * 3. 包含 LoadingCurve 组件（SVG 元素）
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";

// Mock useApi——返回永不 resolve 的 api 让 loading=true 持续
const mockApi = vi.fn().mockReturnValue(new Promise(() => {}));
vi.mock("../../hooks/useApi", () => ({
  useApi: () => ({ api: mockApi }),
}));

import { BehaviorSelector } from "../prompts/BehaviorSelector";

describe("Task 7.19: BehaviorSelector 加载态统一", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // 重新设为永不 resolve
    mockApi.mockReturnValue(new Promise(() => {}));
  });

  it("加载态不出现硬编码 'Loading...' 字面量", async () => {
    render(<BehaviorSelector selectedIds={[]} onChange={() => {}} />);
    await waitFor(() => {
      // setup.ts mock 的 t() 直接返回 key 字符串——所以 "common.loading" 会渲染
      // 但不应出现裸 "Loading..." 字面量（未走 i18n 的 bug）
      expect(screen.queryByText("Loading...")).toBeNull();
    });
  });

  it("加载态走 i18n（渲染 common.loading key）", async () => {
    render(<BehaviorSelector selectedIds={[]} onChange={() => {}} />);
    await waitFor(() => {
      // setup.ts 的 useTranslation mock：t(key) => String(key)
      expect(screen.getByText("common.loading")).toBeInTheDocument();
    });
  });

  it("加载态渲染 LoadingCurve（SVG 动画）", async () => {
    const { container } = render(<BehaviorSelector selectedIds={[]} onChange={() => {}} />);
    await waitFor(() => {
      // LoadingCurve 渲染 <svg> 元素
      const svg = container.querySelector("svg");
      expect(svg).not.toBeNull();
    });
  });
});
