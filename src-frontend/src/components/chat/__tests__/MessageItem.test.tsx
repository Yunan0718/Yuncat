import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { MessageItemView, type TaskActions, type MessageItem } from "../MessageItem";
import type { TaskState } from "../../../store/taskStore";

const t = (key: string, opts?: Record<string, unknown>): string => {
  if (key === "chat.progressStep") return `step ${opts?.step}/${opts?.total}`;
  if (key === "chat.elapsed") return `elapsed ${opts?.time}`;
  if (key === "chat.progressUnknown") return "progress unknown";
  if (key === "chat.executing") return "executing";
  return key;
};

const baseTask: TaskState = {
  taskId: "t1",
  agentId: "hermes",
  prompt: "Build",
  status: "running",
  chunks: [],
  thinkingChunks: [],
  toolCalls: [],
  toolResults: [],
  routeReason: "",
  errorMessage: null,
};

const taskActions: TaskActions = {
  onContextMenu: vi.fn(),
  onEditPrompt: vi.fn(),
  onRegenerate: vi.fn(),
  onFork: vi.fn().mockResolvedValue(undefined),
  onSaveAsTemplate: vi.fn(),
  onUndo: vi.fn().mockResolvedValue(undefined),
  onRate: vi.fn().mockResolvedValue(undefined),
  onShare: vi.fn().mockResolvedValue(undefined),
  onPause: vi.fn().mockResolvedValue(undefined),
  onForcePause: vi.fn().mockResolvedValue(undefined),
  onResume: vi.fn().mockResolvedValue(undefined),
  onStop: vi.fn(),
  onRetry: vi.fn(),
  onOpenFile: vi.fn(),
  onExecutePlan: vi.fn().mockResolvedValue(undefined),
  onShowVersionTree: vi.fn(),
  onSwitchVersion: vi.fn(),
};

const shared = {
  currentSessionId: "s1",
  lang: "zh",
  t,
  inputRef: { current: null as HTMLTextAreaElement | null },
  taskActions,
  versionTrees: {},
  planSteps: [],
  planActiveId: null,
  convToTask: new Map<string, string>(),
  onOpenFile: vi.fn(),
};

function renderTask(task: Partial<TaskState>) {
  const item: MessageItem = { type: "task", id: "t1", task: { ...baseTask, ...task } as TaskState };
  return render(<MessageItemView item={item} {...shared} />);
}

// Task 6.7: 运行中的消息气泡展示进度条与已用时长
describe("MessageItem progress / 已用时长", () => {
  it("长任务运行中显示进度条和 step 文本", () => {
    renderTask({
      isLongTask: true,
      lifecycle: { phase: "executing", stepIndex: 2, totalSteps: 5 },
    });
    expect(screen.getByText("executing")).toBeInTheDocument();
    expect(screen.getByText("step 3/5")).toBeInTheDocument();
    expect(document.querySelector('[role="progressbar"]')).toBeInTheDocument();
  });

  it("有 estimate 时显示已用时长（实测值）", () => {
    renderTask({
      isLongTask: true,
      lifecycle: {
        phase: "executing",
        stepIndex: 1,
        totalSteps: 4,
        estimate: { elapsedSeconds: 125, confidence: 0.8, at: Date.now() },
      },
    });
    expect(screen.getByText("elapsed 2m 5s")).toBeInTheDocument();
  });

  it("诚实耗时：即使后端推了 estimatedRemainingSeconds 也不展示（不外推预测值）", () => {
    renderTask({
      isLongTask: true,
      lifecycle: {
        phase: "executing",
        stepIndex: 1,
        totalSteps: 4,
        estimate: {
          elapsedSeconds: 10,
          estimatedRemainingSeconds: 999,
          confidence: 0.8,
          at: Date.now(),
        },
      },
    });
    expect(screen.getByText("elapsed 10s")).toBeInTheDocument();
    // 999s 的「预计剩余」不应出现在任何位置
    expect(screen.queryByText(/16m 39s|999/)).toBeNull();
  });

  it("任务完成后不显示进度条", () => {
    renderTask({
      status: "completed",
      isLongTask: true,
      lifecycle: { phase: "executing", stepIndex: 5, totalSteps: 5 },
    });
    expect(document.querySelector('[role="progressbar"]')).toBeNull();
  });

  it("普通任务 progress 显示进度条", () => {
    renderTask({
      progress: { step: 2, total: 4 },
    });
    expect(screen.getByText("step 2/4")).toBeInTheDocument();
    expect(document.querySelector('[role="progressbar"]')).toBeInTheDocument();
  });

  it("长任务无 total 时显示 progressUnknown", () => {
    renderTask({
      isLongTask: true,
      lifecycle: { phase: "clarifying" },
    });
    expect(screen.getByText("progress unknown")).toBeInTheDocument();
  });
});
