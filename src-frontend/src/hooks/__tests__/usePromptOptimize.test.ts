/**
 * Phase 73-A: usePromptOptimize 纯函数测试
 *
 * 测试 buildOptimizeRequest——不渲染组件（组件渲染由 Python 合约测试静态分析覆盖）。
 */
import { describe, it, expect } from "vitest";
import { buildOptimizeRequest } from "../usePromptOptimize";

describe("buildOptimizeRequest", () => {
  it("默认 agent_id 为空字符串（后端 DEFAULT_AGENT_ID 兜底）", () => {
    const req = buildOptimizeRequest("实现登录 API");
    expect(req).toEqual({ raw_prompt: "实现登录 API", agent_id: "" });
  });

  it("自定义 agent_id", () => {
    const req = buildOptimizeRequest("实现登录 API", "openclaw");
    expect(req).toEqual({ raw_prompt: "实现登录 API", agent_id: "openclaw" });
  });

  it("空 raw_prompt 仍返回（校验由 hook 内部处理）", () => {
    const req = buildOptimizeRequest("");
    expect(req).toEqual({ raw_prompt: "", agent_id: "" });
  });

  it("含特殊字符的 raw_prompt", () => {
    const req = buildOptimizeRequest("用 @agent:hermes 实现 {json} API\n带换行");
    expect(req.raw_prompt).toBe("用 @agent:hermes 实现 {json} API\n带换行");
    expect(req.agent_id).toBe("");
  });

  it("返回值是新对象（不引用外部状态）", () => {
    const req1 = buildOptimizeRequest("test");
    const req2 = buildOptimizeRequest("test");
    expect(req1).toEqual(req2);
    expect(req1).not.toBe(req2);  // 不同引用
  });
});
