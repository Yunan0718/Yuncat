import { describe, it, expect, beforeEach } from "vitest";
import { useTaskStore } from "../taskStore";

describe("taskStore.patchTaskLifecycle (Phase 86)", () => {
  beforeEach(() => {
    useTaskStore.setState({
      tasks: {},
      activeTaskId: null,
      confirmDialog: null,
      confirmQueue: [],
    });
  });

  it("phase_start patch 设置 phase/description/startedAt", () => {
    useTaskStore.getState().createTask("t1", "hermes", "", "");
    useTaskStore.getState().patchTaskLifecycle("t1", {
      phase: "clarifying",
      phaseDescription: "正在澄清任务目标",
      phaseStartedAt: 1000,
    });
    const t = useTaskStore.getState().tasks["t1"];
    expect(t.lifecycle).toBeDefined();
    expect(t.lifecycle!.phase).toBe("clarifying");
    expect(t.lifecycle!.phaseDescription).toBe("正在澄清任务目标");
    expect(t.lifecycle!.phaseStartedAt).toBe(1000);
  });

  it("step_done patch 保留已有 phase 字段（patch 合并）", () => {
    useTaskStore.getState().createTask("t1", "hermes", "", "");
    // 先推 phase_start
    useTaskStore.getState().patchTaskLifecycle("t1", {
      phase: "executing",
      phaseStartedAt: 2000,
    });
    // 再推 step_done
    useTaskStore.getState().patchTaskLifecycle("t1", {
      stepIndex: 0,
      totalSteps: 3,
      outputPreview: "done",
    });
    const t = useTaskStore.getState().tasks["t1"];
    // phase 应保留
    expect(t.lifecycle!.phase).toBe("executing");
    expect(t.lifecycle!.phaseStartedAt).toBe(2000);
    // step_done 字段应存在
    expect(t.lifecycle!.stepIndex).toBe(0);
    expect(t.lifecycle!.totalSteps).toBe(3);
    expect(t.lifecycle!.outputPreview).toBe("done");
  });

  it("stall_check patch 设置 stall 对象且保留 phase", () => {
    useTaskStore.getState().createTask("t1", "hermes", "", "");
    useTaskStore.getState().patchTaskLifecycle("t1", {
      phase: "executing",
      phaseStartedAt: 100,
    });
    useTaskStore.getState().patchTaskLifecycle("t1", {
      stall: {
        lastTool: "read_file",
        roundsWithoutProgress: 3,
        suggestion: "更换关键词",
        at: 999,
      },
    });
    const t = useTaskStore.getState().tasks["t1"];
    expect(t.lifecycle!.phase).toBe("executing"); // 保留
    expect(t.lifecycle!.stall).toBeDefined();
    expect(t.lifecycle!.stall!.lastTool).toBe("read_file");
    expect(t.lifecycle!.stall!.roundsWithoutProgress).toBe(3);
    expect(t.lifecycle!.stall!.suggestion).toBe("更换关键词");
  });

  it("estimate patch 设置 estimate 对象且保留 phase", () => {
    useTaskStore.getState().createTask("t1", "hermes", "", "");
    useTaskStore.getState().patchTaskLifecycle("t1", {
      phase: "executing",
      phaseStartedAt: 100,
    });
    useTaskStore.getState().patchTaskLifecycle("t1", {
      estimate: {
        elapsedSeconds: 50.5,
        estimatedRemainingSeconds: 100.2,
        confidence: 0.45,
        at: 200,
      },
    });
    const t = useTaskStore.getState().tasks["t1"];
    expect(t.lifecycle!.phase).toBe("executing"); // 保留
    expect(t.lifecycle!.estimate).toBeDefined();
    expect(t.lifecycle!.estimate!.elapsedSeconds).toBe(50.5);
    expect(t.lifecycle!.estimate!.estimatedRemainingSeconds).toBe(100.2);
    expect(t.lifecycle!.estimate!.confidence).toBe(0.45);
  });

  it("对不存在的 task 是 no-op", () => {
    useTaskStore.getState().patchTaskLifecycle("nonexistent", {
      phase: "clarifying",
    });
    expect(useTaskStore.getState().tasks["nonexistent"]).toBeUndefined();
  });

  it("连续 stall_check 覆盖更新（最新覆盖旧的）", () => {
    useTaskStore.getState().createTask("t1", "hermes", "", "");
    useTaskStore.getState().patchTaskLifecycle("t1", {
      phase: "executing",
      phaseStartedAt: 100,
    });
    // 第一次 stall
    useTaskStore.getState().patchTaskLifecycle("t1", {
      stall: { lastTool: "read_file", roundsWithoutProgress: 3, at: 1 },
    });
    // 第二次 stall（递增）
    useTaskStore.getState().patchTaskLifecycle("t1", {
      stall: { lastTool: "read_file", roundsWithoutProgress: 4, at: 2 },
    });
    const t = useTaskStore.getState().tasks["t1"];
    expect(t.lifecycle!.stall!.roundsWithoutProgress).toBe(4); // 最新值
    expect(t.lifecycle!.stall!.at).toBe(2);
  });
});
