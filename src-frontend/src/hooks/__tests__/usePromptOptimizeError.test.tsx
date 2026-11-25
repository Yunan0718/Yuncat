/**
 * Task 6.17 / Batch 5.2: usePromptOptimize 测试
 *
 * 验证：
 * - 后端返回 {error, optimized, changes} 时 error state 被设置，result 为 null
 * - 后端返回无 error 时 result 被设置
 * - 后端返回 candidates 时 hook 正确保存
 * - 后端未返回 candidates 时自动补全单候选
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, act } from "@testing-library/react";

// Mock useApi——捕获 api 调用，避免 useAppStore 依赖
const mockApi = vi.fn();
vi.mock("../useApi", () => ({
  useApi: () => ({ api: mockApi }),
}));

import { usePromptOptimize } from "../usePromptOptimize";

describe("usePromptOptimize", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("后端返回 error 字段 → 设置 error state，不设置 result，返回 null", async () => {
    mockApi.mockResolvedValue({
      error: "LLM 调用失败",
      optimized: "test prompt",
      changes: [],
    });

    const { result } = renderHook(() => usePromptOptimize());

    let ret: unknown = "initial";
    await act(async () => {
      ret = await result.current.optimize("test prompt");
    });

    expect(ret).toBeNull();
    expect(result.current.error).toBe("LLM 调用失败");
    expect(result.current.result).toBeNull();
  });

  it("后端返回无 error → 正常设置 result，error 为 null", async () => {
    mockApi.mockResolvedValue({
      optimized: "优化后 prompt",
      changes: ["补充上下文"],
    });

    const { result } = renderHook(() => usePromptOptimize());

    let ret: unknown = null;
    await act(async () => {
      ret = await result.current.optimize("test prompt");
    });

    expect(ret).not.toBeNull();
    expect(result.current.error).toBeNull();
    expect(result.current.result?.optimized).toBe("优化后 prompt");
  });

  it("后端返回 candidates → result 包含完整候选列表", async () => {
    mockApi.mockResolvedValue({
      optimized: "best",
      changes: ["best change"],
      candidates: [
        { optimized: "best", changes: ["best change"], score: 0.9 },
        { optimized: "ok", changes: ["ok change"], score: 0.7 },
      ],
    });

    const { result } = renderHook(() => usePromptOptimize());

    await act(async () => {
      await result.current.optimize("test prompt");
    });

    expect(result.current.result?.candidates).toHaveLength(2);
    expect(result.current.result?.candidates[0].score).toBe(0.9);
  });

  it("后端未返回 candidates → 自动补全为单候选", async () => {
    mockApi.mockResolvedValue({
      optimized: "single",
      changes: ["single change"],
    });

    const { result } = renderHook(() => usePromptOptimize());

    await act(async () => {
      await result.current.optimize("test prompt");
    });

    expect(result.current.result?.candidates).toHaveLength(1);
    expect(result.current.result?.candidates[0]).toEqual({
      optimized: "single",
      changes: ["single change"],
      score: 1,
    });
  });
});
