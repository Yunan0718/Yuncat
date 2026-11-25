import { describe, it, expect, beforeEach } from "vitest";
import { useTaskStore } from "../taskStore";
import { dispatchToStore } from "../../hooks/wsHandlers";

/**
 * 工具实时输出（后端 tool_output_chunk → 前端）。
 *
 * 后端从 Phase 61 起就流式回传命令输出，但前端一直没接这个事件——
 * 事件在 useWebSocket 里被直接丢弃，"命令跑起来能看到输出"只存在于后端。
 */
describe("工具实时输出", () => {
  beforeEach(() => {
    useTaskStore.setState({
      tasks: {},
      activeTaskId: null,
      confirmDialog: null,
      confirmQueue: [],
    });
    useTaskStore.getState().createTask("task-1", "yuncat", "", "");
  });

  it("累积同一工具的输出", () => {
    const store = useTaskStore.getState();
    store.appendToolLiveOutput("task-1", "run_command", "line1\n");
    store.appendToolLiveOutput("task-1", "run_command", "line2\n");
    expect(useTaskStore.getState().tasks["task-1"].toolLiveOutput?.run_command).toBe("line1\nline2\n");
  });

  it("超过上限时保留最新的一段（不能无限吃内存）", () => {
    const store = useTaskStore.getState();
    store.appendToolLiveOutput("task-1", "run_command", "x".repeat(9000));
    const out = useTaskStore.getState().tasks["task-1"].toolLiveOutput?.run_command ?? "";
    expect(out.length).toBe(8000);
    expect(out.endsWith("x")).toBe(true);
  });

  it("工具结束后清掉实时输出", () => {
    const store = useTaskStore.getState();
    store.appendToolLiveOutput("task-1", "run_command", "done");
    store.clearToolLiveOutput("task-1", "run_command");
    expect(useTaskStore.getState().tasks["task-1"].toolLiveOutput?.run_command).toBeUndefined();
  });

  it("WS 事件 tool_output_chunk 能落到 store 里", () => {
    dispatchToStore({
      type: "tool_output_chunk",
      task_id: "task-1",
      tool_name: "run_command",
      data: "hello\n",
      stream: "stdout",
    });
    expect(useTaskStore.getState().tasks["task-1"].toolLiveOutput?.run_command).toBe("hello\n");
  });

  it("tool_done 之后实时输出被清掉（最终内容以结果为准）", () => {
    dispatchToStore({
      type: "tool_output_chunk",
      task_id: "task-1",
      tool_name: "run_command",
      data: "partial",
    });
    dispatchToStore({ type: "tool_done", task_id: "task-1", tool_name: "run_command" });
    expect(useTaskStore.getState().tasks["task-1"].toolLiveOutput?.run_command).toBeUndefined();
  });
});
