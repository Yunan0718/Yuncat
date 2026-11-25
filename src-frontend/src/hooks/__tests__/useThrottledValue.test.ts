/**
 * useThrottledValue 测试。
 *
 * 本 hook 的全部意义是：**产出频率上限 ≈ 1/minIntervalMs，与上游更新频率无关**。
 * 因此核心测试必须是「计数型」——高频输入下统计实际出帧次数，而不是只断言
 * 「某个值最终会出现」（后者对未节流的实现同样成立，是空测试）。
 *
 * 教训：第一版写了「持续更新期间必须周期性出帧，不退化成防抖」的测试，
 * 拿退化实现（clearTimeout 放进 effect cleanup）跑照样 7/7 通过——
 * 因为 `elapsed` 按真实时间累积，那种写法到点也会走 leading 分支。
 * 那条断言压根不区分两种实现，已删除。
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { act, renderHook } from "@testing-library/react";
import { useThrottledValue } from "../useThrottledValue";

const advance = async (ms: number) => {
  await act(async () => {
    vi.advanceTimersByTime(ms);
  });
};

/** 统计一段高频输入内，输出值实际发生了多少次变化（= 出帧次数） */
async function countEmits(opts: { windowMs: number; stepMs: number; steps: number }) {
  const { windowMs, stepMs, steps } = opts;
  const { result, rerender } = renderHook(({ v }) => useThrottledValue(v, windowMs), {
    initialProps: { v: "v0" },
  });

  let emits = 0;
  let prev = result.current;
  for (let i = 1; i <= steps; i++) {
    await advance(stepMs);
    rerender({ v: `v${i}` });
    if (result.current !== prev) {
      emits++;
      prev = result.current;
    }
  }
  return emits;
}

describe("useThrottledValue", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("首次渲染立即返回初始值（leading，不被节流延迟）", () => {
    const { result } = renderHook(() => useThrottledValue("a", 100));
    expect(result.current).toBe("a");
  });

  it("窗口内的更新不立即生效", () => {
    const { result, rerender } = renderHook(({ v }) => useThrottledValue(v, 100), {
      initialProps: { v: "a" },
    });
    rerender({ v: "b" });
    expect(result.current).toBe("a");
  });

  it("窗口过后补齐最新值（trailing）", async () => {
    const { result, rerender } = renderHook(({ v }) => useThrottledValue(v, 100), {
      initialProps: { v: "a" },
    });
    rerender({ v: "b" });
    await advance(100);
    expect(result.current).toBe("b");
  });

  it("核心契约：出帧次数由时长决定，而非输入次数", async () => {
    // 1000ms 内每 10ms 来一帧（共 100 次输入），节流窗口 100ms
    // → 期望出帧 ≈ 1000/100 = 10 次；未节流实现会出 ~100 次
    const emits = await countEmits({ windowMs: 100, stepMs: 10, steps: 100 });
    expect(emits).toBeGreaterThanOrEqual(8);
    expect(emits).toBeLessThanOrEqual(14);
  });

  it("输入频率翻倍，出帧次数基本不变（与输入频率解耦）", async () => {
    const slow = await countEmits({ windowMs: 100, stepMs: 20, steps: 50 }); // 1000ms / 50 次输入
    const fast = await countEmits({ windowMs: 100, stepMs: 5, steps: 200 }); // 1000ms / 200 次输入
    // 输入次数差 4 倍，出帧次数应当相近
    expect(Math.abs(fast - slow)).toBeLessThanOrEqual(4);
    expect(fast).toBeLessThanOrEqual(14);
  });

  it("停止更新后最终值一定会被补齐", async () => {
    const { result, rerender } = renderHook(({ v }) => useThrottledValue(v, 100), {
      initialProps: { v: "a" },
    });
    for (let i = 1; i <= 6; i++) {
      await advance(20);
      rerender({ v: `t${i}` });
    }
    rerender({ v: "final" });
    await advance(200);
    expect(result.current).toBe("final");
  });

  it("卸载后清理定时器——推进时间不报错", async () => {
    const { rerender, unmount } = renderHook(({ v }) => useThrottledValue(v, 100), {
      initialProps: { v: "a" },
    });
    rerender({ v: "b" });
    unmount();
    await expect(advance(500)).resolves.toBeUndefined();
  });

  it("节流窗口为 0 时等同直通", async () => {
    const emits = await countEmits({ windowMs: 0, stepMs: 10, steps: 20 });
    expect(emits).toBe(20);
  });
});
