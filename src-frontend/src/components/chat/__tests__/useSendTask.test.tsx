/**
 * Batch 4a F5: useSendTask 15s 安全超时测试。
 *
 * 修复背景：原超时 effect 依赖数组含 p.store.tasks（每 chunk 变引用），
 * timer 被无限重置——任务 B 的 task_created 丢失时 sending 永久卡死。
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { act, renderHook } from "@testing-library/react";
import { useSendTask, type UseSendTaskParams } from "../useSendTask";
import { useTaskStore } from "../../../store/taskStore";

// mock context-pills：跳过 @引用解析（doSend 主流程依赖）
vi.mock("../../context-pills", () => ({
  parseReferences: () => [],
  stripReferences: (t: string) => t,
  resolveMemoryRefs: async () => "",
  resolveFileRefs: async () => "",
  resolveSessionRefs: async () => "",
  resolveFolderRefs: async () => "",
  resolveWebRefs: async () => "",
  resolveGitRefs: async () => "",
}));

function makeParams(overrides: Partial<UseSendTaskParams> = {}): UseSendTaskParams {
  return {
    input: "task B input",
    setInput: vi.fn(),
    commands: [],
    handleLocalSlashCommand: vi.fn(async () => true),
    submitTask: vi.fn(),
    selectedAgent: "",
    api: vi.fn(async () => ({ session_id: "sess-1" })) as unknown as UseSendTaskParams["api"],
    mode: "agent",
    currentSessionId: null,
    setCurrentSession: vi.fn(),
    editingTaskId: null,
    setEditingTaskId: vi.fn(),
    setTaskSuperseded: vi.fn(),
    // 对齐 ChatView 的 stableStore：tasks 通过 getter 实时读取
    store: {
      get tasks() {
        return useTaskStore.getState().tasks;
      },
      setTaskParent: vi.fn(),
      addTaskVersion: vi.fn(),
    },
    t: (k: string) => k,
    toast: vi.fn(),
    wsPort: 9000,
    wsToken: "tk",
    setSlashMenu: vi.fn(),
    attachmentPreviews: [],
    setAttachmentPreviews: vi.fn(),
    setStatusMsg: vi.fn(),
    ...overrides,
  };
}

describe("Batch 4a F5: 15s 超时不受流式 chunk 重置", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    useTaskStore.setState({ tasks: {}, currentSessionId: null, activeTaskId: null });
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("任务 A 流式期间任务 B 超时仍触发——回填输入 + 重置 sending", async () => {
    // 任务 A 先创建（running）——其 chunk 到达只变 tasks 引用，不增 taskCount
    act(() => {
      useTaskStore.getState().createTask("task-a", "hermes", "task A prompt", "", "conv-a");
    });

    const params = makeParams();
    const { result } = renderHook(() => useSendTask(params));

    await act(async () => {
      await result.current.doSend();
    });
    expect(result.current.sending).toBe(true);

    // 15s 窗口内任务 A 持续流式（原实现依赖含 p.store.tasks，timer 被无限重置）
    for (let i = 1; i <= 3; i++) {
      await act(async () => {
        useTaskStore.getState().appendChunk("task-a", `chunk-${i}`);
        vi.advanceTimersByTime(6000);
      });
    }
    // 累计 18s > 15s——超时必须已触发
    act(() => {
      vi.advanceTimersByTime(1000);
    });

    expect(result.current.sending).toBe(false);
    expect(params.setInput).toHaveBeenCalledWith("task B input");
    expect(params.toast).toHaveBeenCalledWith("chat.sendTimeout", "error");
  });

  it("taskCount 已增加（task_created 在途/到达）时不回填输入", async () => {
    act(() => {
      useTaskStore.getState().createTask("task-a", "hermes", "task A prompt", "", "conv-a");
    });

    const params = makeParams();
    const { result } = renderHook(() => useSendTask(params));

    await act(async () => {
      await result.current.doSend();
    });
    expect(result.current.sending).toBe(true);

    // 模拟任务 B 的 task_created 到达——taskCount 增加，Send reset 生效
    act(() => {
      useTaskStore.getState().createTask("task-b", "hermes", "task B input", "", "conv-b");
    });
    expect(result.current.sending).toBe(false);

    act(() => {
      vi.advanceTimersByTime(20000);
    });
    expect(params.setInput).not.toHaveBeenCalledWith("task B input");
    expect(params.toast).not.toHaveBeenCalledWith("chat.sendTimeout", "error");
  });

  it("当前 active task 运行中时按任务投递 steer", async () => {
    act(() => {
      useTaskStore.getState().createTask("task-a", "yuncat", "original", "", "conv-a");
    });
    const steerTask = vi.fn();
    const params = makeParams({ steerTask });
    const { result } = renderHook(() => useSendTask(params));

    await act(async () => {
      await result.current.doSend();
    });

    expect(steerTask).toHaveBeenCalledWith("task-a", "task B input");
    expect(params.submitTask).not.toHaveBeenCalled();
    expect(result.current.sending).toBe(false);
  });

  it("图片先走 HTTP 上传，WS 只提交附件引用", async () => {
    const uploaded = {
      name: "shot.png", type: "image/png", path: "/data/attachments/shot.png",
      bytes: 5, sha256: "abc",
    };
    const api = vi.fn(async (path: string) => {
      if (path.startsWith("/api/v1/attachments")) return uploaded;
      return { session_id: "sess-1" };
    });
    const params = makeParams({
      api: api as unknown as UseSendTaskParams["api"],
      currentSessionId: "sess-1",
      attachmentPreviews: [{ name: "shot.png", type: "image/png", data: "aGVsbG8=" }],
    });
    const { result } = renderHook(() => useSendTask(params));

    await act(async () => {
      await result.current.doSend();
    });

    expect(api).toHaveBeenCalledWith(
      expect.stringContaining("/api/v1/attachments?session_id=sess-1"),
      expect.objectContaining({ method: "POST", headers: { "Content-Type": "image/png" } }),
    );
    expect(params.submitTask).toHaveBeenCalledWith(
      "task B input", undefined, "sess-1", "agent",
      [uploaded], 1, "medium",
    );
  });

  it("长任务带图片时明确拒绝而不是静默丢附件", async () => {
    const params = makeParams({
      mode: "long_task",
      attachmentPreviews: [{ name: "shot.png", type: "image/png", data: "aGVsbG8=" }],
    });
    const { result } = renderHook(() => useSendTask(params));

    await act(async () => {
      await result.current.doSend();
    });

    expect(params.submitTask).not.toHaveBeenCalled();
    expect(params.toast).toHaveBeenCalledWith("chat.longTaskAttachmentsUnsupported", "error");
    expect(params.setAttachmentPreviews).toHaveBeenCalledWith(params.attachmentPreviews);
  });
});
