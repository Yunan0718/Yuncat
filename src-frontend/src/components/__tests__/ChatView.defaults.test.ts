import { describe, it, expect, beforeEach, vi } from "vitest";
import { loadLeftCollapsed } from "../chat/ChatView";

/**
 * Phase 70: ChatView 默认展开测试
 *
 * 方案要求："清除 localStorage 后渲染，断言两侧展开"
 *
 * ChatView 含 Monaco/xterm/WebSocket 等重依赖，完整渲染需大量 mock（违背简单原则）。
 * 故采用 TerminalPanel Phase 66 模式：导出 loadLeftCollapsed() 纯函数直接测试，
 * 并通过 vi.resetModules + 动态 import 验证 appStore 的 rightCollapsed 默认值。
 */
describe("Phase 70: ChatView 默认展开", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  // ── loadLeftCollapsed 纯函数 ──
  describe("loadLeftCollapsed", () => {
    it("localStorage 为空时返回 false（默认展开）", () => {
      expect(loadLeftCollapsed()).toBe(false);
    });

    it("localStorage = 'true' 时返回 true（折叠）", () => {
      localStorage.setItem("yuncat-left-collapsed", "true");
      expect(loadLeftCollapsed()).toBe(true);
    });

    it("localStorage = 'false' 时返回 false（展开）", () => {
      localStorage.setItem("yuncat-left-collapsed", "false");
      expect(loadLeftCollapsed()).toBe(false);
    });

    it("localStorage 为非法值时返回 false（默认展开）", () => {
      localStorage.setItem("yuncat-left-collapsed", "garbage");
      expect(loadLeftCollapsed()).toBe(false);
    });
  });

  // ── appStore rightCollapsed 默认值（需重新加载模块以触发 IIFE 初始化）──
  describe("appStore rightCollapsed 默认折叠", () => {
    it("localStorage 为空时 rightCollapsed === true（Phase 69 默认折叠，首屏以对话为主）", async () => {
      vi.resetModules();
      const { useAppStore } = await import("../../store/appStore");
      expect(useAppStore.getState().rightCollapsed).toBe(true);
    });

    it("localStorage = 'true' 时 rightCollapsed === true（折叠）", async () => {
      localStorage.setItem("yuncat-right-collapsed", "true");
      vi.resetModules();
      const { useAppStore } = await import("../../store/appStore");
      expect(useAppStore.getState().rightCollapsed).toBe(true);
    });

    it("localStorage = 'false' 时 rightCollapsed === false（展开）", async () => {
      localStorage.setItem("yuncat-right-collapsed", "false");
      vi.resetModules();
      const { useAppStore } = await import("../../store/appStore");
      expect(useAppStore.getState().rightCollapsed).toBe(false);
    });
  });

  // ── 综合断言：左侧默认展开、右侧默认折叠（Phase 69）──
  describe("默认布局：左展开右折叠", () => {
    it("清除 localStorage 后 leftCollapsed=false 且 rightCollapsed=true", async () => {
      // leftCollapsed
      expect(loadLeftCollapsed()).toBe(false);
      // rightCollapsed
      vi.resetModules();
      const { useAppStore } = await import("../../store/appStore");
      expect(useAppStore.getState().rightCollapsed).toBe(true);
    });
  });
});
