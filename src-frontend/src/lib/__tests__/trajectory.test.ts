import { describe, expect, it } from "vitest";

import { projectTrajectory } from "../trajectory";

/**
 * 轨迹投影（大改动的第一步）：先把"零散状态 → 有序条目"这件事用纯函数钉死，
 * 再谈渲染。这里断言的是顺序、种类、状态与折叠性——它们是渲染层唯一依赖的契约。
 */
describe("projectTrajectory", () => {
  it("空任务投影为空列表（不凭空造条目）", () => {
    expect(projectTrajectory({})).toEqual([]);
    expect(projectTrajectory({ chunks: ["   "], thinkingChunks: [] })).toEqual([]);
  });

  it("顺序：思考 → 计划 → 工具 → 失败 → 正文", () => {
    const items = projectTrajectory({
      thinkingChunks: ["先看目录"],
      planSteps: [{ step: 0, task: "读配置", status: "done" }],
      toolCalls: [{ name: "read_file", args: { path: "a.py" } }],
      toolResults: [{ tool_name: "read_file", success: true, preview: "print(1)" }],
      errorMessage: "Authentication Fails",
      chunks: ["看完了"],
      status: "completed",
    });
    expect(items.map((item) => item.kind)).toEqual(["thinking", "plan", "tool", "error", "message"]);
  });

  it("工具条目带摘要与状态：成功 done、失败 failed、没结果 running", () => {
    const items = projectTrajectory({
      toolCalls: [
        { name: "read_file", args: { path: "a.py" } },
        { name: "run_command", args: { command: "pytest" } },
        { name: "git", args: { args: "status" } },
      ],
      toolResults: [
        { tool_name: "read_file", success: true, preview: "ok" },
        { tool_name: "run_command", success: false, preview: "boom" },
      ],
      chunks: ["x"],
    });
    const tools = items.filter((item) => item.kind === "tool");
    expect(tools.map((item) => item.status)).toEqual(["done", "failed", "running"]);
    expect(tools[0].title).toContain("a.py");
  });

  it("工具结果到达前展示实时输出（命令 stdout 流式回传）", () => {
    const running = projectTrajectory({
      toolCalls: [{ name: "run_command", args: { command: "pytest -q" } }],
      toolLiveOutput: { run_command: "collecting...\n" },
    });
    const tool = running.find((item) => item.kind === "tool");
    expect(tool?.status).toBe("running");
    expect(tool?.detail).toContain("collecting...");

    // 结果到达后以结果为准，不与实时输出并存
    const done = projectTrajectory({
      toolCalls: [{ name: "run_command", args: { command: "pytest -q" } }],
      toolResults: [{ tool_name: "run_command", success: true, fullResult: "12 passed" }],
      toolLiveOutput: { run_command: "collecting...\n" },
    });
    const doneTool = done.find((item) => item.kind === "tool");
    expect(doneTool?.detail).toContain("12 passed");
    expect(doneTool?.detail).not.toContain("collecting...");
  });

  it("计划条目统计进度并在失败时标 failed", () => {
    const ok = projectTrajectory({
      planSteps: [{ step: 0, task: "a", status: "done" }, { step: 1, task: "b", status: "done" }],
    });
    expect(ok[0].title).toBe("执行计划 2/2");
    expect(ok[0].status).toBe("done");

    const bad = projectTrajectory({
      planSteps: [{ step: 0, task: "a", status: "failed", error: "测试没过" }],
    });
    expect(bad[0].status).toBe("failed");
    expect(bad[0].detail).toContain("测试没过");
  });

  it("正文与失败不可折叠，过程类可折叠", () => {
    const items = projectTrajectory({
      thinkingChunks: ["想一想"],
      toolCalls: [{ name: "read_file", args: { path: "a.py" } }],
      chunks: ["答案"],
      errorMessage: "boom",
    });
    const byKind = Object.fromEntries(items.map((item) => [item.kind, item.collapsible]));
    expect(byKind).toEqual({ thinking: true, tool: true, error: false, message: false });
  });

  it("只有运行中的工具时也能显示一行", () => {
    const items = projectTrajectory({
      currentTool: { name: "run_command", status: "running", preview: "pytest -q" },
      status: "running",
    });
    expect(items).toHaveLength(1);
    expect(items[0]).toMatchObject({ kind: "tool", status: "running", title: "pytest -q" });
  });

  it("参数里有循环引用也不抛异常（降级成字符串）", () => {
    const cyclic: Record<string, unknown> = {};
    cyclic.self = cyclic;
    const items = projectTrajectory({ toolCalls: [{ name: "x", args: cyclic }], chunks: ["y"] });
    expect(items[0].kind).toBe("tool");
  });
});
