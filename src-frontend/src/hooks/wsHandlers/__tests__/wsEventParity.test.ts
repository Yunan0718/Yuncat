import { describe, expect, it, vi, beforeEach } from "vitest";

const { emit } = vi.hoisted(() => ({ emit: vi.fn() }));

vi.mock("../../../components/common/toast-context", () => ({
  emitGlobalToast: (message: string, type?: string) => emit(message, type),
}));

// t 走 defaultValue（生产里语言包缺 key 时的兜底路径），没有 defaultValue 才回 key
vi.mock("i18next", () => ({
  default: { t: (key: string, opts?: { defaultValue?: string }) => opts?.defaultValue ?? key },
}));

import { useTaskStore } from "../../../store/taskStore";
import { dispatchToStore } from "../index";

/**
 * 后端发了、前端没接的事件——消息被静默丢弃，界面上"少了一件事"。
 *
 * 这一批补上的三个：
 * - task_status：spec 循环审计改状态时推的，前端不接 → 任务状态不更新；
 * - steer_queued：追加指令入队的即时回执（原来只有 applied / error）；
 * - standards_lookup_result：标准查询结果（原来只进了下一轮的模型上下文）。
 */
describe("WS 事件面对账", () => {
  beforeEach(() => {
    emit.mockReset();
    useTaskStore.setState({ tasks: {}, activeTaskId: null });
    useTaskStore.getState().createTask("t1", "yuncat", "", "");
  });

  it("task_status 同步任务状态", () => {
    dispatchToStore({ type: "task_status", task_id: "t1", status: "completed" });
    expect(useTaskStore.getState().tasks.t1.status).toBe("completed");
  });

  it("task_status 遇到未知状态不改状态", () => {
    dispatchToStore({ type: "task_status", task_id: "t1", status: "completed" });
    dispatchToStore({ type: "task_status", task_id: "t1", status: "weird-value" });
    expect(useTaskStore.getState().tasks.t1.status).toBe("completed");
  });

  it("steer_queued 给出入队回执", () => {
    dispatchToStore({ type: "steer_queued", task_id: "t1", queued: 2, prompt: "x" });
    expect(emit).toHaveBeenCalledTimes(1);
    expect(emit.mock.calls[0][1]).toBe("info");
  });

  it("standards_lookup_result 展示查询摘要", () => {
    dispatchToStore({
      type: "standards_lookup_result",
      task_id: "t1",
      tool_name: "standards_lookup",
      summary: "命中 3 条规范",
      recommendations: ["a"],
    });
    expect(emit).toHaveBeenCalledTimes(1);
    expect(emit.mock.calls[0][0]).toContain("命中 3 条规范");
  });

  it("standards_lookup_result 没有内容时不打扰用户", () => {
    dispatchToStore({ type: "standards_lookup_result", task_id: "t1" });
    expect(emit).not.toHaveBeenCalled();
  });

  it("long_task_window_rollover 提示上下文翻滚", () => {
    dispatchToStore({
      type: "long_task_window_rollover",
      task_id: "t1",
      generation: 3,
      progress: 0.42,
    });
    expect(emit).toHaveBeenCalledTimes(1);
    expect(emit.mock.calls[0][0]).toContain("3");
  });

  it("long_task_supervisor_escalation 用错误级提示（需要用户注意）", () => {
    dispatchToStore({
      type: "long_task_supervisor_escalation",
      task_id: "t1",
      reason: "偏离原始意图",
    });
    expect(emit).toHaveBeenCalledTimes(1);
    expect(emit.mock.calls[0][1]).toBe("error");
  });

  it("long_task_arbitration_resolved 告知仲裁结论", () => {
    dispatchToStore({
      type: "long_task_arbitration_resolved",
      task_id: "t1",
      decision: "abort",
      directive: "方向错了",
    });
    expect(emit).toHaveBeenCalledTimes(1);
    expect(emit.mock.calls[0][0]).toContain("abort");
  });

  it("long_task_supervision 逐轮事件不弹提示（否则变成噪音）", () => {
    dispatchToStore({
      type: "long_task_supervision",
      task_id: "t1",
      action: "continue",
      score: 82,
    });
    expect(emit).not.toHaveBeenCalled();
  });
});
