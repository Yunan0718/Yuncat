/**
 * Phase 73: BehaviorSelector 纯函数单元测试
 *
 * 验证 groupBehaviorsByLanguage——按 language 字段分组 behaviors。
 * 组件渲染测试在 contract test（Python 静态分析）中覆盖。
 */
import { describe, it, expect } from "vitest";
import { groupBehaviorsByLanguage, type Behavior } from "../prompts/BehaviorSelector";

function makeBehavior(id: string, language: string): Behavior {
  return {
    id,
    type: "behavior",
    category: "behaviors",
    name: id,
    description: "",
    content: "",
    language,
    source: "builtin",
    is_builtin: true,
  };
}

describe("Phase 73: groupBehaviorsByLanguage", () => {
  it("空数组返回空 Map", () => {
    const result = groupBehaviorsByLanguage([]);
    expect(result.size).toBe(0);
  });

  it("单个 behavior 归入对应 language 分组", () => {
    const result = groupBehaviorsByLanguage([makeBehavior("git-workflow", "general")]);
    expect(result.size).toBe(1);
    expect(result.get("general")).toHaveLength(1);
    expect(result.get("general")![0].id).toBe("git-workflow");
  });

  it("不同 language 分到不同组", () => {
    const result = groupBehaviorsByLanguage([
      makeBehavior("a", "general"),
      makeBehavior("b", "python"),
      makeBehavior("c", "golang"),
    ]);
    expect(result.size).toBe(3);
    expect(result.get("general")![0].id).toBe("a");
    expect(result.get("python")![0].id).toBe("b");
    expect(result.get("golang")![0].id).toBe("c");
  });

  it("同 language 的多个 behavior 归入同一组", () => {
    const result = groupBehaviorsByLanguage([
      makeBehavior("a", "general"),
      makeBehavior("b", "general"),
      makeBehavior("c", "general"),
    ]);
    expect(result.size).toBe(1);
    expect(result.get("general")).toHaveLength(3);
  });

  it("language 为空字符串时归入 general 分组（兜底）", () => {
    const result = groupBehaviorsByLanguage([makeBehavior("a", "")]);
    expect(result.size).toBe(1);
    expect(result.get("general")).toHaveLength(1);
  });

  it("混合 language 正确分组", () => {
    const result = groupBehaviorsByLanguage([
      makeBehavior("a", "general"),
      makeBehavior("b", "python"),
      makeBehavior("c", "general"),
      makeBehavior("d", "typescript"),
      makeBehavior("e", "python"),
    ]);
    expect(result.size).toBe(3);
    expect(result.get("general")).toHaveLength(2);
    expect(result.get("python")).toHaveLength(2);
    expect(result.get("typescript")).toHaveLength(1);
  });
});
