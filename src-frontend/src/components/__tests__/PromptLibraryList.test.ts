/**
 * Phase 73: PromptLibraryList 纯函数单元测试
 *
 * 验证 filterByQuery——按 query 过滤条目（id/name/description 大小写不敏感）。
 */
import { describe, it, expect } from "vitest";
import { filterByQuery } from "../prompts/PromptLibraryList";

interface TestItem {
  id: string;
  name?: string;
  description?: string;
}

describe("Phase 73: filterByQuery", () => {
  it("空 query 返回全部条目", () => {
    const items: TestItem[] = [
      { id: "architect", name: "架构师" },
      { id: "developer", name: "开发者" },
    ];
    const result = filterByQuery(items, "");
    expect(result).toHaveLength(2);
  });

  it("空格 query 返回全部条目", () => {
    const items: TestItem[] = [{ id: "architect", name: "架构师" }];
    const result = filterByQuery(items, "   ");
    expect(result).toHaveLength(1);
  });

  it("按 id 匹配（大小写不敏感）", () => {
    const items: TestItem[] = [
      { id: "architect", name: "架构师" },
      { id: "developer", name: "开发者" },
    ];
    const result = filterByQuery(items, "ARCH");
    expect(result).toHaveLength(1);
    expect(result[0].id).toBe("architect");
  });

  it("按 name 匹配", () => {
    const items: TestItem[] = [
      { id: "a", name: "架构师" },
      { id: "b", name: "开发者" },
    ];
    const result = filterByQuery(items, "架构");
    expect(result).toHaveLength(1);
    expect(result[0].id).toBe("a");
  });

  it("按 description 匹配", () => {
    const items: TestItem[] = [
      { id: "a", description: "负责高层设计" },
      { id: "b", description: "负责实现" },
    ];
    const result = filterByQuery(items, "设计");
    expect(result).toHaveLength(1);
    expect(result[0].id).toBe("a");
  });

  it("无匹配时返回空数组", () => {
    const items: TestItem[] = [{ id: "a", name: "架构师" }];
    const result = filterByQuery(items, "nonexistent");
    expect(result).toHaveLength(0);
  });

  it("空数组输入返回空数组", () => {
    const result = filterByQuery([], "anything");
    expect(result).toHaveLength(0);
  });

  it("name/description 为 undefined 时不报错", () => {
    const items: TestItem[] = [{ id: "a" }];
    const result = filterByQuery(items, "a");
    expect(result).toHaveLength(1);
  });

  it("部分匹配——query 是 id 的子串", () => {
    const items: TestItem[] = [
      { id: "zh_tdd_loop", name: "TDD 循环" },
      { id: "zh_code_review", name: "代码审查" },
    ];
    const result = filterByQuery(items, "tdd");
    expect(result).toHaveLength(1);
    expect(result[0].id).toBe("zh_tdd_loop");
  });
});
