/**
 * Batch 4.6: WS 重连后恢复流式上下文 — replayMessages handler 测试
 *
 * 验证:
 * - 后端推送 replay_messages 时，前端 diff 渲染未存在的 assistant 内容
 * - 已存在的内容不重复追加
 * - user 角色不渲染（避免与 prompt 重复）
 * - reasoning_content 追加到 thinkingChunks
 * - 实际追加了内容时触发 bumpSessionList
 *
 * 对标 Claude Code 重连后全量状态同步。
 */
import { describe, it, expect, beforeEach } from "vitest";
import { useTaskStore } from "../../../store/taskStore";
import { replayMessages } from "../taskHandlers";

describe("Batch 4.6: replayMessages handler", () => {
  beforeEach(() => {
    useTaskStore.getState().clearTasks();
  });

  function createTask(taskId: string, initialChunks: string[] = [], initialThinking: string[] = []) {
    useTaskStore.getState().createTask(taskId, "hermes", "test prompt", "reason", "sess-1");
    useTaskStore.setState((s) => ({
      tasks: {
        ...s.tasks,
        [taskId]: {
          ...s.tasks[taskId],
          chunks: initialChunks,
          thinkingChunks: initialThinking,
        },
      },
    }));
  }

  it("追加未渲染的 assistant 内容", () => {
    createTask("t1");
    const beforeVersion = useTaskStore.getState().sessionListVersion;

    replayMessages(
      {
        task_id: "t1",
        messages: [
          { id: "m1", role: "user", content: "Hello" },
          { id: "m2", role: "assistant", content: "Hi there!" },
        ],
      },
      useTaskStore.getState(),
    );

    const task = useTaskStore.getState().tasks.t1;
    expect(task.chunks).toEqual(["Hi there!"]);
    expect(useTaskStore.getState().sessionListVersion).toBe(beforeVersion + 1);
  });

  it("已存在的内容不重复追加", () => {
    createTask("t2", ["Hi there!"]);
    const beforeVersion = useTaskStore.getState().sessionListVersion;

    replayMessages(
      {
        task_id: "t2",
        messages: [
          { id: "m1", role: "assistant", content: "Hi there!" },
          { id: "m2", role: "assistant", content: "New content" },
        ],
      },
      useTaskStore.getState(),
    );

    const task = useTaskStore.getState().tasks.t2;
    expect(task.chunks).toEqual(["Hi there!", "New content"]);
    expect(useTaskStore.getState().sessionListVersion).toBe(beforeVersion + 1);
  });

  it("reasoning_content 追加到 thinkingChunks", () => {
    createTask("t3", [], ["existing thought"]);

    replayMessages(
      {
        task_id: "t3",
        messages: [
          { id: "m1", role: "assistant", content: "Answer", reasoning_content: "thinking..." },
        ],
      },
      useTaskStore.getState(),
    );

    const task = useTaskStore.getState().tasks.t3;
    expect(task.chunks).toEqual(["Answer"]);
    expect(task.thinkingChunks).toEqual(["existing thought", "thinking..."]);
  });

  it("不存在的 task 直接返回", () => {
    const beforeState = useTaskStore.getState();
    replayMessages(
      {
        task_id: "nonexistent",
        messages: [{ id: "m1", role: "assistant", content: "Hello" }],
      },
      beforeState,
    );

    expect(useTaskStore.getState().tasks.nonexistent).toBeUndefined();
  });

  it("空 messages 不追加也不触发 bumpSessionList", () => {
    createTask("t4");
    const beforeVersion = useTaskStore.getState().sessionListVersion;

    replayMessages(
      {
        task_id: "t4",
        messages: [],
      },
      useTaskStore.getState(),
    );

    expect(useTaskStore.getState().tasks.t4.chunks).toEqual([]);
    expect(useTaskStore.getState().sessionListVersion).toBe(beforeVersion);
  });
});
