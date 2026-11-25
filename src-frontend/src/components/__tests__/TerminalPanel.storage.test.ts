/**
 * TerminalPanel.storage.test.ts —— Phase 66 终端 localStorage 持久化测试
 *
 * 验证：
 * 1. loadCmdHistory: 空/正常/超长截断/非法 JSON
 * 2. loadTabs: 空/正常/空数组→默认/非法 JSON
 * 3. loadActiveTab: 空/正常/非法 JSON
 */
import { describe, it, expect, beforeEach } from "vitest";
import { loadCmdHistory, loadTabs, loadActiveTab } from "../workspace/TerminalPanel";

describe("TerminalPanel localStorage 持久化", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  describe("loadCmdHistory", () => {
    it("空 localStorage 返回空数组", () => {
      expect(loadCmdHistory()).toEqual([]);
    });

    it("正常 JSON 数组返回", () => {
      localStorage.setItem("yuncat:terminal:cmdHistory", JSON.stringify(["ls", "pwd", "git status"]));
      expect(loadCmdHistory()).toEqual(["ls", "pwd", "git status"]);
    });

    it("超过 MAX_HISTORY 截断到 200 条", () => {
      const arr = Array.from({ length: 250 }, (_, i) => `cmd${i}`);
      localStorage.setItem("yuncat:terminal:cmdHistory", JSON.stringify(arr));
      const result = loadCmdHistory();
      expect(result).toHaveLength(200);
      expect(result[0]).toBe("cmd0");
      expect(result[199]).toBe("cmd199");
    });

    it("非数组 JSON 返回空数组", () => {
      localStorage.setItem("yuncat:terminal:cmdHistory", JSON.stringify({ not: "array" }));
      expect(loadCmdHistory()).toEqual([]);
    });

    it("非法 JSON 返回空数组", () => {
      localStorage.setItem("yuncat:terminal:cmdHistory", "not-json{");
      expect(loadCmdHistory()).toEqual([]);
    });
  });

  describe("loadTabs", () => {
    it("空 localStorage 返回默认 main tab", () => {
      const result = loadTabs();
      expect(result).toHaveLength(1);
      expect(result[0].id).toBe("main");
    });

    it("正常 JSON 数组返回", () => {
      const tabs = [
        { id: "main", label: "终端", sessionId: "sess1" },
        { id: "tab-2", label: "终端 2", sessionId: "sess2" },
      ];
      localStorage.setItem("yuncat:terminal:tabs", JSON.stringify(tabs));
      expect(loadTabs()).toEqual(tabs);
    });

    it("空数组返回默认 main tab", () => {
      localStorage.setItem("yuncat:terminal:tabs", "[]");
      const result = loadTabs();
      expect(result).toHaveLength(1);
      expect(result[0].id).toBe("main");
    });

    it("非数组 JSON 返回默认 main tab", () => {
      localStorage.setItem("yuncat:terminal:tabs", JSON.stringify({ not: "array" }));
      const result = loadTabs();
      expect(result).toHaveLength(1);
      expect(result[0].id).toBe("main");
    });

    it("非法 JSON 返回默认 main tab", () => {
      localStorage.setItem("yuncat:terminal:tabs", "not-json{");
      const result = loadTabs();
      expect(result).toHaveLength(1);
      expect(result[0].id).toBe("main");
    });
  });

  describe("loadActiveTab", () => {
    it("空 localStorage 返回 main", () => {
      expect(loadActiveTab()).toBe("main");
    });

    it("正常值返回", () => {
      localStorage.setItem("yuncat:terminal:activeTab", "tab-2");
      expect(loadActiveTab()).toBe("tab-2");
    });

    it("非法访问返回 main（容错）", () => {
      // 模拟 localStorage 抛错——通过覆盖 getItem
      const original = localStorage.getItem;
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      (localStorage as any).getItem = () => {
        throw new Error("access denied");
      };
      try {
        expect(loadActiveTab()).toBe("main");
      } finally {
        localStorage.getItem = original;
      }
    });
  });
});
