import { describe, it, expect, beforeEach } from "vitest";
import { useAppStore } from "../appStore";

describe("appStore", () => {
  beforeEach(() => {
    // 重置 store 到初始状态
    useAppStore.setState({
      backendReady: false,
      backendDead: false,
      port: null,
      token: null,
      theme: "system",
      commandPaletteOpen: false,
      selectedTaskId: null,
    });
  });

  it("初始状态 backendsReady 为 false", () => {
    expect(useAppStore.getState().backendReady).toBe(false);
  });

  it("setBackendReady 设置为 true", () => {
    useAppStore.getState().setBackendReady(true);
    expect(useAppStore.getState().backendReady).toBe(true);
  });

  it("setBackendDead 设置死连接状态", () => {
    useAppStore.getState().setBackendDead(true);
    expect(useAppStore.getState().backendDead).toBe(true);
  });

  it("setPort 和 setToken 设置连接参数", () => {
    useAppStore.getState().setPort(12345);
    useAppStore.getState().setToken("test-token");
    expect(useAppStore.getState().port).toBe(12345);
    expect(useAppStore.getState().token).toBe("test-token");
  });

  it("setTheme 切换主题", () => {
    useAppStore.getState().setTheme("dark");
    expect(useAppStore.getState().theme).toBe("dark");
    useAppStore.getState().setTheme("light");
    expect(useAppStore.getState().theme).toBe("light");
  });

  it("toggleCommandPalette 切换命令面板", () => {
    expect(useAppStore.getState().commandPaletteOpen).toBe(false);
    useAppStore.getState().toggleCommandPalette();
    expect(useAppStore.getState().commandPaletteOpen).toBe(true);
    useAppStore.getState().toggleCommandPalette();
    expect(useAppStore.getState().commandPaletteOpen).toBe(false);
  });

  it("setCommandPaletteOpen 直接设置命令面板", () => {
    useAppStore.getState().setCommandPaletteOpen(true);
    expect(useAppStore.getState().commandPaletteOpen).toBe(true);
  });

  it("setSelectedTaskId 设置选中任务", () => {
    useAppStore.getState().setSelectedTaskId("task-1");
    expect(useAppStore.getState().selectedTaskId).toBe("task-1");
    useAppStore.getState().setSelectedTaskId(null);
    expect(useAppStore.getState().selectedTaskId).toBeNull();
  });

  // ── Phase 69: 右侧面板全局状态 ──
  describe("Phase 69 rightCollapsed", () => {
    beforeEach(() => {
      // 重置到默认折叠状态
      useAppStore.getState().setRightCollapsed(true);
      try { localStorage.removeItem("yuncat-right-collapsed"); } catch { /* ignore */ }
    });

    it("toggleRightPanel 切换折叠状态", () => {
      useAppStore.getState().setRightCollapsed(true);
      expect(useAppStore.getState().rightCollapsed).toBe(true);
      useAppStore.getState().toggleRightPanel();
      expect(useAppStore.getState().rightCollapsed).toBe(false);
      useAppStore.getState().toggleRightPanel();
      expect(useAppStore.getState().rightCollapsed).toBe(true);
    });

    it("setRightCollapsed 直接设置状态", () => {
      useAppStore.getState().setRightCollapsed(false);
      expect(useAppStore.getState().rightCollapsed).toBe(false);
      useAppStore.getState().setRightCollapsed(true);
      expect(useAppStore.getState().rightCollapsed).toBe(true);
    });

    it("toggleRightPanel 持久化到 localStorage", () => {
      useAppStore.getState().setRightCollapsed(true);
      useAppStore.getState().toggleRightPanel();
      expect(localStorage.getItem("yuncat-right-collapsed")).toBe("false");
      useAppStore.getState().toggleRightPanel();
      expect(localStorage.getItem("yuncat-right-collapsed")).toBe("true");
    });

    it("setRightCollapsed 持久化到 localStorage", () => {
      useAppStore.getState().setRightCollapsed(false);
      expect(localStorage.getItem("yuncat-right-collapsed")).toBe("false");
    });
  });

  // ── Phase 98: 会话恢复持久化字段 ──
  describe("Phase 98 会话恢复", () => {
    beforeEach(() => {
      // 清理 Phase 98 相关 localStorage key + 重置 store 字段
      const keys = [
        "yuncat-last-route",
        "yuncat-chat-bottom-tab",
        "yuncat-workspace-bottom-panel",
        "yuncat-session-tabs",
      ];
      for (const k of keys) {
        try { localStorage.removeItem(k); } catch { /* ignore */ }
      }
      useAppStore.setState({
        lastRoute: "/",
        chatBottomTab: "output",
        workspaceBottomPanel: null,
        openSessionTabs: [],
        activeSessionTab: null,
      });
    });

    // ── lastRoute ──
    it("setLastRoute 设置并持久化路由", () => {
      useAppStore.getState().setLastRoute("/workflows");
      expect(useAppStore.getState().lastRoute).toBe("/workflows");
      expect(localStorage.getItem("yuncat-last-route")).toBe("/workflows");
    });

    it("setLastRoute 多次切换覆盖旧值", () => {
      useAppStore.getState().setLastRoute("/settings");
      useAppStore.getState().setLastRoute("/workspace");
      expect(localStorage.getItem("yuncat-last-route")).toBe("/workspace");
    });

    // ── chatBottomTab ──
    it("setChatBottomTab 设置并持久化 tab", () => {
      useAppStore.getState().setChatBottomTab("output");
      expect(useAppStore.getState().chatBottomTab).toBe("output");
      expect(localStorage.getItem("yuncat-chat-bottom-tab")).toBe("output");
    });

    it("setChatBottomTab 两种 tab 值均持久化", () => {
      for (const tab of ["output", "audit"] as const) {
        useAppStore.getState().setChatBottomTab(tab);
        expect(localStorage.getItem("yuncat-chat-bottom-tab")).toBe(tab);
      }
    });

    // ── workspaceBottomPanel ──
    it("setWorkspaceBottomPanel 设置 terminal 并持久化", () => {
      useAppStore.getState().setWorkspaceBottomPanel("terminal");
      expect(useAppStore.getState().workspaceBottomPanel).toBe("terminal");
      expect(localStorage.getItem("yuncat-workspace-bottom-panel")).toBe("terminal");
    });

    it("setWorkspaceBottomPanel 设置 git 并持久化", () => {
      useAppStore.getState().setWorkspaceBottomPanel("git");
      expect(useAppStore.getState().workspaceBottomPanel).toBe("git");
      expect(localStorage.getItem("yuncat-workspace-bottom-panel")).toBe("git");
    });

    it("setWorkspaceBottomPanel 设置 null 持久化为空字符串", () => {
      useAppStore.getState().setWorkspaceBottomPanel("terminal");
      useAppStore.getState().setWorkspaceBottomPanel(null);
      expect(useAppStore.getState().workspaceBottomPanel).toBeNull();
      expect(localStorage.getItem("yuncat-workspace-bottom-panel")).toBe("");
    });

    // ── openSessionTabs / activeSessionTab ──
    it("addSessionTab 添加 tab 并持久化", () => {
      useAppStore.getState().addSessionTab("sess-1", "会话1");
      expect(useAppStore.getState().openSessionTabs).toHaveLength(1);
      expect(useAppStore.getState().activeSessionTab).toBe("sess-1");
      const stored = JSON.parse(localStorage.getItem("yuncat-session-tabs") || "{}");
      expect(stored.tabs).toHaveLength(1);
      expect(stored.active).toBe("sess-1");
    });

    it("addSessionTab 重复 sessionId 不新增但更新 active", () => {
      useAppStore.getState().addSessionTab("sess-1", "会话1");
      useAppStore.getState().addSessionTab("sess-2", "会话2");
      useAppStore.getState().addSessionTab("sess-1", "会话1改");
      expect(useAppStore.getState().openSessionTabs).toHaveLength(2);
      expect(useAppStore.getState().activeSessionTab).toBe("sess-1");
    });

    it("removeSessionTab 移除 tab 并更新 active", () => {
      useAppStore.getState().addSessionTab("sess-1", "会话1");
      useAppStore.getState().addSessionTab("sess-2", "会话2");
      useAppStore.getState().removeSessionTab("sess-2");
      expect(useAppStore.getState().openSessionTabs).toHaveLength(1);
      // 移除 active 后回退到最后一个
      expect(useAppStore.getState().activeSessionTab).toBe("sess-1");
      const stored = JSON.parse(localStorage.getItem("yuncat-session-tabs") || "{}");
      expect(stored.tabs).toHaveLength(1);
      expect(stored.active).toBe("sess-1");
    });

    it("setActiveSessionTab 更新 active 并持久化", () => {
      useAppStore.getState().addSessionTab("sess-1", "会话1");
      useAppStore.getState().addSessionTab("sess-2", "会话2");
      useAppStore.getState().setActiveSessionTab("sess-1");
      expect(useAppStore.getState().activeSessionTab).toBe("sess-1");
      const stored = JSON.parse(localStorage.getItem("yuncat-session-tabs") || "{}");
      expect(stored.active).toBe("sess-1");
    });

    it("updateSessionTabTitle 更新标题并持久化", () => {
      useAppStore.getState().addSessionTab("sess-1", "会话1");
      useAppStore.getState().updateSessionTabTitle("sess-1", "新标题");
      expect(useAppStore.getState().openSessionTabs[0].title).toBe("新标题");
      const stored = JSON.parse(localStorage.getItem("yuncat-session-tabs") || "{}");
      expect(stored.tabs[0].title).toBe("新标题");
    });
  });
});
