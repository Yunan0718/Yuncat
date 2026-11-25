/**
 * E4: 流式输出改进——isStreaming 状态生命周期测试。
 *
 * 验证：
 * - createTask 设 isStreaming=true
 * - setTaskDone/Error/Cancelled/Paused/Interrupted 设 isStreaming=false
 * - setTaskResumed 设 isStreaming=true
 * - setStreaming 动作直接控制
 */
import { describe, it, expect, beforeEach } from "vitest";
import { useTaskStore } from "../taskStore";

describe("E4: isStreaming 生命周期", () => {
  beforeEach(() => {
    useTaskStore.setState({ tasks: {}, activeTaskId: null });
  });

  it("createTask 设 isStreaming=true", () => {
    useTaskStore.getState().createTask("t1", "hermes", "hi", "test");
    expect(useTaskStore.getState().tasks["t1"].isStreaming).toBe(true);
  });

  it("setTaskDone 设 isStreaming=false", () => {
    useTaskStore.getState().createTask("t1", "hermes", "hi", "test");
    useTaskStore.getState().setTaskDone("t1");
    expect(useTaskStore.getState().tasks["t1"].isStreaming).toBe(false);
  });

  it("setTaskError 设 isStreaming=false", () => {
    useTaskStore.getState().createTask("t1", "hermes", "hi", "test");
    useTaskStore.getState().setTaskError("t1", "boom");
    expect(useTaskStore.getState().tasks["t1"].isStreaming).toBe(false);
  });

  it("setTaskCancelled 设 isStreaming=false", () => {
    useTaskStore.getState().createTask("t1", "hermes", "hi", "test");
    useTaskStore.getState().setTaskCancelled("t1");
    expect(useTaskStore.getState().tasks["t1"].isStreaming).toBe(false);
  });

  it("setTaskPaused 设 isStreaming=false", () => {
    useTaskStore.getState().createTask("t1", "hermes", "hi", "test");
    useTaskStore.getState().setTaskPaused("t1");
    expect(useTaskStore.getState().tasks["t1"].isStreaming).toBe(false);
  });

  it("setTaskInterrupted 设 isStreaming=false", () => {
    useTaskStore.getState().createTask("t1", "hermes", "hi", "test");
    useTaskStore.getState().setTaskInterrupted("t1");
    expect(useTaskStore.getState().tasks["t1"].isStreaming).toBe(false);
  });

  it("setTaskResumed 恢复 isStreaming=true", () => {
    useTaskStore.getState().createTask("t1", "hermes", "hi", "test");
    useTaskStore.getState().setTaskPaused("t1");
    expect(useTaskStore.getState().tasks["t1"].isStreaming).toBe(false);
    useTaskStore.getState().setTaskResumed("t1");
    expect(useTaskStore.getState().tasks["t1"].isStreaming).toBe(true);
  });

  it("setStreaming 动作直接控制", () => {
    useTaskStore.getState().createTask("t1", "hermes", "hi", "test");
    expect(useTaskStore.getState().tasks["t1"].isStreaming).toBe(true);

    useTaskStore.getState().setStreaming("t1", false);
    expect(useTaskStore.getState().tasks["t1"].isStreaming).toBe(false);

    useTaskStore.getState().setStreaming("t1", true);
    expect(useTaskStore.getState().tasks["t1"].isStreaming).toBe(true);
  });

  it("setStreaming 幂等——相同值不触发更新", () => {
    useTaskStore.getState().createTask("t1", "hermes", "hi", "test");
    const before = useTaskStore.getState().tasks["t1"];
    useTaskStore.getState().setStreaming("t1", true); // 已经是 true
    const after = useTaskStore.getState().tasks["t1"];
    // 引用应不变（Zustand set 返回 s 时跳过更新）
    expect(after).toBe(before);
  });

  it("setStreaming 忽略不存在的任务", () => {
    // 不应抛异常
    useTaskStore.getState().setStreaming("nonexistent", false);
    expect(useTaskStore.getState().tasks["nonexistent"]).toBeUndefined();
  });

  it("迟到的 stream_done 不覆盖 interrupted 状态的 isStreaming", () => {
    // 模拟：任务中断后，迟到的 stream_done 到达
    useTaskStore.getState().createTask("t1", "hermes", "hi", "test");
    useTaskStore.getState().setTaskInterrupted("t1");
    expect(useTaskStore.getState().tasks["t1"].isStreaming).toBe(false);

    // 迟到的 stream_done → setTaskDone 应被 interrupted guard 拦截
    useTaskStore.getState().setTaskDone("t1");
    expect(useTaskStore.getState().tasks["t1"].status).toBe("interrupted");
    expect(useTaskStore.getState().tasks["t1"].isStreaming).toBe(false);
  });
});
