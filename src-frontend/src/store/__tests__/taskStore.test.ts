import { describe, it, expect, beforeEach } from "vitest";
import { useTaskStore } from "../taskStore";

describe("taskStore", () => {
  beforeEach(() => {
    useTaskStore.setState({
      tasks: {},
      activeTaskId: null,
      confirmDialog: null,
      confirmQueue: [],
    });
  });

  // ---- createTask ----
  it("createTask 创建新任务并设为 active", () => {
    useTaskStore.getState().createTask("task-1", "hermes", "Hello", "意图匹配");
    const t = useTaskStore.getState().tasks["task-1"];
    expect(t).toBeDefined();
    expect(t.agentId).toBe("hermes");
    expect(t.status).toBe("running");
    expect(t.chunks).toEqual([]);
    expect(t.routeReason).toBe("意图匹配");
    expect(useTaskStore.getState().activeTaskId).toBe("task-1");
  });

  // ---- appendChunk ----
  it("appendChunk 追加流式数据块", () => {
    useTaskStore.getState().createTask("task-1", "hermes", "", "");
    useTaskStore.getState().appendChunk("task-1", "Hello");
    useTaskStore.getState().appendChunk("task-1", " World");
    expect(useTaskStore.getState().tasks["task-1"].chunks).toEqual(["Hello", " World"]);
  });

  it("appendChunk 忽略不存在的任务", () => {
    useTaskStore.getState().appendChunk("nonexistent", "data");
    // 不应抛出异常，状态不变
    expect(useTaskStore.getState().tasks["nonexistent"]).toBeUndefined();
  });

  // ---- setTaskDone ----
  it("setTaskDone 将任务标记为 completed", () => {
    useTaskStore.getState().createTask("task-1", "hermes", "", "");
    useTaskStore.getState().setTaskDone("task-1");
    expect(useTaskStore.getState().tasks["task-1"].status).toBe("completed");
    // active 任务应被清除
    expect(useTaskStore.getState().activeTaskId).toBeNull();
  });

  it("setTaskDone 不覆盖已取消的任务", () => {
    useTaskStore.getState().createTask("task-1", "hermes", "", "");
    useTaskStore.getState().setTaskCancelled("task-1");
    useTaskStore.getState().setTaskDone("task-1");
    // Guard: cancelled 不应被 completed 覆盖
    expect(useTaskStore.getState().tasks["task-1"].status).toBe("cancelled");
  });

  // ---- setTaskError ----
  it("setTaskError 将任务标记为 failed 并设置错误消息", () => {
    useTaskStore.getState().createTask("task-1", "hermes", "", "");
    useTaskStore.getState().setTaskError("task-1", "Connection refused");
    expect(useTaskStore.getState().tasks["task-1"].status).toBe("failed");
    expect(useTaskStore.getState().tasks["task-1"].errorMessage).toBe("Connection refused");
  });

  it("setTaskError 不覆盖已取消的任务", () => {
    useTaskStore.getState().createTask("task-1", "hermes", "", "");
    useTaskStore.getState().setTaskCancelled("task-1");
    useTaskStore.getState().setTaskError("task-1", "error");
    expect(useTaskStore.getState().tasks["task-1"].status).toBe("cancelled");
  });

  // ---- setTaskCancelled ----
  it("setTaskCancelled 取消运行中的任务", () => {
    useTaskStore.getState().createTask("task-1", "hermes", "", "");
    useTaskStore.getState().setTaskCancelled("task-1");
    expect(useTaskStore.getState().tasks["task-1"].status).toBe("cancelled");
    expect(useTaskStore.getState().activeTaskId).toBeNull();
  });

  it("setTaskCancelled 不覆盖已完成的任务", () => {
    useTaskStore.getState().createTask("task-1", "hermes", "", "");
    useTaskStore.getState().setTaskDone("task-1");
    useTaskStore.getState().setTaskCancelled("task-1");
    // Guard: completed 不应被 cancelled 覆盖
    expect(useTaskStore.getState().tasks["task-1"].status).toBe("completed");
  });

  // ---- setTaskProgress ----
  it("setTaskProgress 更新任务进度", () => {
    useTaskStore.getState().createTask("task-1", "hermes", "", "");
    useTaskStore.getState().setTaskProgress("task-1", 3, 10);
    const t = useTaskStore.getState().tasks["task-1"];
    expect(t.progress).toEqual({ step: 3, total: 10 });
  });

  // ---- setTaskResultType ----
  it("setTaskResultType 设置结构化结果类型", () => {
    useTaskStore.getState().createTask("task-1", "hermes", "", "");
    useTaskStore.getState().setTaskResultType("task-1", "chart");
    expect(useTaskStore.getState().tasks["task-1"].resultType).toBe("chart");
  });

  // ---- confirmDialog ----
  it("showConfirmDialog 显示确认弹窗并返回 Promise", () => {
    const promise = useTaskStore.getState().showConfirmDialog("task-1", "write_file", { path: "test.py" });
    expect(promise).toBeInstanceOf(Promise);
    const dialog = useTaskStore.getState().confirmDialog;
    expect(dialog).not.toBeNull();
    expect(dialog?.taskId).toBe("task-1");
    expect(dialog?.toolName).toBe("write_file");
  });

  it("showConfirmDialog 队列化新请求——不自动拒绝旧弹窗（P0-1）", async () => {
    // P0-1 (2026-07-04): 旧实现自动 resolve(false) 旧弹窗导致 Agent 跳过工具。
    // 新实现：已有弹窗展示中时新请求入队，等当前弹窗 resolve 后再弹出下一个。
    const oldPromise = useTaskStore.getState().showConfirmDialog("task-1", "write_file", {});
    const newPromise = useTaskStore.getState().showConfirmDialog("task-2", "run_command", {});

    // 旧弹窗仍是当前展示的，新弹窗入队
    expect(useTaskStore.getState().confirmDialog?.taskId).toBe("task-1");
    expect(useTaskStore.getState().confirmQueue.length).toBe(1);
    expect(useTaskStore.getState().confirmQueue[0].taskId).toBe("task-2");

    // resolve 旧弹窗——旧 Promise 应得到 true（而非旧实现的 false 自动拒绝）
    useTaskStore.getState().resolveConfirmDialog(true);
    const oldResult = await oldPromise;
    expect(oldResult).toBe(true);

    // 队列中的新弹窗应自动弹出展示
    expect(useTaskStore.getState().confirmDialog?.taskId).toBe("task-2");
    expect(useTaskStore.getState().confirmQueue.length).toBe(0);

    // resolve 新弹窗
    useTaskStore.getState().resolveConfirmDialog(true);
    const newResult = await newPromise;
    expect(newResult).toBe(true);
    expect(useTaskStore.getState().confirmDialog).toBeNull();
  });

  it("resolveConfirmDialog 拒绝时队列中的下一个弹窗仍弹出（P0-1）", async () => {
    const p1 = useTaskStore.getState().showConfirmDialog("t1", "write_file", {});
    useTaskStore.getState().showConfirmDialog("t2", "run_command", {});
    useTaskStore.getState().showConfirmDialog("t3", "delete_file", {});

    // 拒绝第一个——队列中下一个仍应弹出（不被前一个拒绝影响）
    useTaskStore.getState().resolveConfirmDialog(false);
    const r1 = await p1;
    expect(r1).toBe(false);
    expect(useTaskStore.getState().confirmDialog?.taskId).toBe("t2");
    expect(useTaskStore.getState().confirmQueue.length).toBe(1);

    // 确认第二个
    useTaskStore.getState().resolveConfirmDialog(true);
    expect(useTaskStore.getState().confirmDialog?.taskId).toBe("t3");
    expect(useTaskStore.getState().confirmQueue.length).toBe(0);

    // 确认第三个
    useTaskStore.getState().resolveConfirmDialog(true);
    expect(useTaskStore.getState().confirmDialog).toBeNull();
  });

  it("resolveConfirmDialog 确认弹窗", async () => {
    const promise = useTaskStore.getState().showConfirmDialog("task-1", "run_command", {});
    useTaskStore.getState().resolveConfirmDialog(true);
    const result = await promise;
    expect(result).toBe(true);
    // 弹窗应被清除
    expect(useTaskStore.getState().confirmDialog).toBeNull();
  });

  it("resolveConfirmDialog 拒绝弹窗", async () => {
    const promise = useTaskStore.getState().showConfirmDialog("task-1", "write_file", {});
    useTaskStore.getState().resolveConfirmDialog(false);
    const result = await promise;
    expect(result).toBe(false);
  });

  // Phase D G1.3: 讨论共识注入
  it("injectAssistantMessage 将共识作为已完成 assistant 任务注入", () => {
    useTaskStore.getState().injectAssistantMessage("delib-1", "讨论主题", "共识内容", "session-1");
    const t = useTaskStore.getState().tasks["delib-1"];
    expect(t).toBeDefined();
    expect(t.agentId).toBe("deliberation");
    expect(t.status).toBe("completed");
    expect(t.chunks).toEqual(["共识内容"]);
    expect(t.convId).toBe("session-1");
    expect(t.routeReason).toBe("deliberation_consensus");
  });
});
