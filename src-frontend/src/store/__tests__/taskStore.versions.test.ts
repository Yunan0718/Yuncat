import { describe, it, expect, beforeEach } from "vitest";
import { useTaskStore } from "../taskStore";

describe("taskStore versions (Phase 49-C)", () => {
  beforeEach(() => {
    useTaskStore.setState({
      tasks: {},
      activeTaskId: null,
      confirmDialog: null,
      confirmQueue: [],
    });
  });

  it("setTaskParent 设置任务父节点", () => {
    useTaskStore.getState().createTask("parent", "hermes", "parent prompt", "");
    useTaskStore.getState().createTask("child", "hermes", "child prompt", "");
    useTaskStore.getState().setTaskParent("child", "parent");
    expect(useTaskStore.getState().tasks["child"].parentTaskId).toBe("parent");
  });

  it("setTaskParent 支持清空父节点", () => {
    useTaskStore.getState().createTask("child", "hermes", "child prompt", "");
    useTaskStore.getState().setTaskParent("child", "parent");
    useTaskStore.getState().setTaskParent("child", null);
    expect(useTaskStore.getState().tasks["child"].parentTaskId).toBeNull();
  });

  it("addTaskVersion 向父任务追加历史版本", () => {
    useTaskStore.getState().createTask("parent", "hermes", "parent prompt", "");
    const version = { taskId: "v1", prompt: "v1 prompt", chunks: ["a"], createdAt: 1 };
    useTaskStore.getState().addTaskVersion("parent", version);
    const versions = useTaskStore.getState().tasks["parent"].versions;
    expect(versions).toHaveLength(1);
    expect(versions?.[0].taskId).toBe("v1");
  });

  it("addTaskVersion 忽略不存在的父任务", () => {
    const version = { taskId: "v1", prompt: "v1 prompt", chunks: ["a"], createdAt: 1 };
    useTaskStore.getState().addTaskVersion("missing", version);
    expect(useTaskStore.getState().tasks["missing"]).toBeUndefined();
  });

  it("switchVersion 切换到指定历史版本内容", () => {
    useTaskStore.getState().createTask("parent", "hermes", "parent prompt", "");
    useTaskStore.getState().appendChunk("parent", "original");
    const version = { taskId: "v1", prompt: "v1 prompt", chunks: ["v1 chunk"], createdAt: 1 };
    useTaskStore.getState().addTaskVersion("parent", version);
    useTaskStore.getState().switchVersion("parent", "v1");
    const task = useTaskStore.getState().tasks["parent"];
    expect(task.prompt).toBe("v1 prompt");
    expect(task.chunks).toEqual(["v1 chunk"]);
    expect(task.activeVersionTaskId).toBe("v1");
  });

  it("switchVersion 对不存在版本保持原状态", () => {
    useTaskStore.getState().createTask("parent", "hermes", "parent prompt", "");
    useTaskStore.getState().appendChunk("parent", "original");
    useTaskStore.getState().switchVersion("parent", "missing");
    const task = useTaskStore.getState().tasks["parent"];
    expect(task.prompt).toBe("parent prompt");
    expect(task.chunks).toEqual(["original"]);
  });
});
