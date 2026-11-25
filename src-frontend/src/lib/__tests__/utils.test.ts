/**
 * v67: 头尾截断工具测试。
 *
 * 背景：长文本只保留头部会丢掉往往最关键的结尾（报错、结果摘要），
 * 因此改为保留「头 + 尾」、中间折叠。移植自 DeepSeek harness 的
 * `headTailCap`（packages/client/ui-primitives/src/head-tail-cap.ts）。
 */
import { describe, it, expect } from "vitest";
import { headTailCap, truncateMiddle } from "../utils";

describe("headTailCap", () => {
  it("未超上限时不截断", () => {
    const r = headTailCap(10, 20, false);
    expect(r.hidden).toBe(-10);
    expect(r.capped).toBe(false);
  });

  it("恰好等于上限时不截断", () => {
    const r = headTailCap(20, 20, false);
    expect(r.hidden).toBe(0);
    expect(r.capped).toBe(false);
  });

  it("超出上限时折叠，头为 ceil(max/2)、尾为余数", () => {
    const r = headTailCap(100, 20, false);
    expect(r.hidden).toBe(80);
    expect(r.capped).toBe(true);
    expect(r.headCount).toBe(10);
    expect(r.tailCount).toBe(10);
  });

  it("奇数上限时头部多分一个", () => {
    const r = headTailCap(100, 15, false);
    expect(r.headCount).toBe(8); // ceil(15/2)
    expect(r.tailCount).toBe(7);
    expect(r.headCount + r.tailCount).toBe(15);
  });

  it("展开态即使超出上限也不折叠", () => {
    const r = headTailCap(100, 20, true);
    expect(r.hidden).toBe(80);
    expect(r.capped).toBe(false);
  });
});

describe("truncateMiddle", () => {
  it("短于上限时原样返回", () => {
    expect(truncateMiddle("hello", 10)).toBe("hello");
  });

  it("等于上限时原样返回", () => {
    expect(truncateMiddle("hello", 5)).toBe("hello");
  });

  it("超长时保留头尾、中间以 … 连接", () => {
    // 10 个数字，保留头 3 + 尾 3
    const out = truncateMiddle("0123456789", 6);
    expect(out).toBe("012…789");
    expect(out).toContain("…");
  });

  it("结果长度恒为 max + 1（含省略号）", () => {
    const text = "x".repeat(1000);
    const out = truncateMiddle(text, 200);
    expect(out.length).toBe(201);
  });

  it("保留的是开头与结尾，而非仅开头", () => {
    const text = "HEAD" + "y".repeat(100) + "TAIL";
    const out = truncateMiddle(text, 20);
    expect(out.startsWith("HEAD")).toBe(true);
    expect(out.endsWith("TAIL")).toBe(true);
  });

  it("上限 ≤ 0 时返回空串", () => {
    expect(truncateMiddle("hello", 0)).toBe("");
    expect(truncateMiddle("hello", -5)).toBe("");
  });
});
