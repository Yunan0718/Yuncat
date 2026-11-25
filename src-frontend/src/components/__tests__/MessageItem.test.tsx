import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { useTaskStore } from "../../store/taskStore";
import { MessageItemView, type TaskActions } from "../chat/MessageItem";

describe("MessageItem TaskControls", () => {
  const actions: TaskActions = {
    onContextMenu: vi.fn(),
    onEditPrompt: vi.fn(),
    onRegenerate: vi.fn(),
    onFork: vi.fn(),
    onSaveAsTemplate: vi.fn(),
    onUndo: vi.fn(),
    onRate: vi.fn(),
    onShare: vi.fn(),
    onPause: vi.fn(),
    onForcePause: vi.fn(),
    onResume: vi.fn(),
    onStop: vi.fn(),
    onRetry: vi.fn(),
    onOpenFile: vi.fn(),
    onExecutePlan: vi.fn(),
    onShowVersionTree: vi.fn(),
    onSwitchVersion: vi.fn(),
  };

  beforeEach(() => {
    useTaskStore.setState({
      tasks: {},
      activeTaskId: null,
      confirmDialog: null,
      confirmQueue: [],
      planSteps: [],
      planActiveId: null,
    });
  });

  function renderTask(status: "running" | "paused" | "completed" | "interrupted") {
    useTaskStore.getState().createTask("t1", "hermes", "hello", "", "conv-1");
    useTaskStore.getState().appendChunk("t1", "world");
    if (status === "paused") {
      useTaskStore.getState().setTaskPaused("t1");
    } else if (status === "completed") {
      useTaskStore.getState().setTaskDone("t1");
    } else if (status === "interrupted") {
      useTaskStore.getState().setTaskInterrupted("t1");
    }
    const task = useTaskStore.getState().tasks["t1"];
    return render(
      <MessageItemView
        item={{ type: "task", id: "t1", task }}
        currentSessionId="session-1"
        lang="zh"
        t={(key: string) => key}
        inputRef={{ current: null }}
        taskActions={actions}
        versionTrees={{}}
        planSteps={[]}
        planActiveId={null}
        convToTask={new Map()}
        onOpenFile={vi.fn()}
      />,
    );
  }

  it("运行中任务显示暂停和停止按钮", () => {
    renderTask("running");
    expect(screen.getByLabelText("chat.pause")).toBeInTheDocument();
    expect(screen.getByText("chat.stop")).toBeInTheDocument();
  });

  it("点击暂停选项调用 onPause", async () => {
    renderTask("running");
    fireEvent.click(screen.getByLabelText("chat.pause"));
    const pauseItem = await screen.findByText("chat.pause");
    fireEvent.click(pauseItem);
    expect(actions.onPause).toHaveBeenCalledWith("t1");
  });

  it("点击强制暂停选项调用 onForcePause", async () => {
    renderTask("running");
    fireEvent.click(screen.getByLabelText("chat.pause"));
    const forceItem = await screen.findByText("chat.forcePause");
    fireEvent.click(forceItem);
    expect(actions.onForcePause).toHaveBeenCalledWith("t1");
  });

  it("点击停止按钮调用 onStop", () => {
    renderTask("running");
    fireEvent.click(screen.getByText("chat.stop"));
    expect(actions.onStop).toHaveBeenCalledWith("t1");
  });

  it("暂停任务显示继续按钮并调用 onResume", () => {
    renderTask("paused");
    const resumeButton = screen.getByText(/chat\.resume/);
    expect(resumeButton).toBeInTheDocument();
    fireEvent.click(resumeButton);
    expect(actions.onResume).toHaveBeenCalledWith("t1");
  });

  it("G4: 中断任务显示继续按钮并调用 onResume（可恢复）", () => {
    renderTask("interrupted");
    const resumeButton = screen.getByText(/chat\.resume/);
    expect(resumeButton).toBeInTheDocument();
    fireEvent.click(resumeButton);
    expect(actions.onResume).toHaveBeenCalledWith("t1");
  });

  it("G4: 中断任务显示已中断状态标签", () => {
    renderTask("interrupted");
    expect(screen.getByText("chat.interrupted")).toBeInTheDocument();
  });
});

describe("MessageItem Phase 65 移动端适配", () => {
  const actions: TaskActions = {
    onContextMenu: vi.fn(),
    onEditPrompt: vi.fn(),
    onRegenerate: vi.fn(),
    onFork: vi.fn(),
    onSaveAsTemplate: vi.fn(),
    onUndo: vi.fn(),
    onRate: vi.fn(),
    onShare: vi.fn(),
    onPause: vi.fn(),
    onForcePause: vi.fn(),
    onResume: vi.fn(),
    onStop: vi.fn(),
    onRetry: vi.fn(),
    onOpenFile: vi.fn(),
    onExecutePlan: vi.fn(),
    onShowVersionTree: vi.fn(),
    onSwitchVersion: vi.fn(),
  };

  beforeEach(() => {
    useTaskStore.setState({
      tasks: {},
      activeTaskId: null,
      confirmDialog: null,
      confirmQueue: [],
      planSteps: [],
      planActiveId: null,
    });
  });

  function renderCompletedTask() {
    useTaskStore.getState().createTask("t1", "hermes", "hello", "", "conv-1");
    useTaskStore.getState().appendChunk("t1", "world");
    useTaskStore.getState().setTaskDone("t1");
    const task = useTaskStore.getState().tasks["t1"];
    return render(
      <MessageItemView
        item={{ type: "task", id: "t1", task }}
        currentSessionId="session-1"
        lang="zh"
        t={(key: string) => key}
        inputRef={{ current: null }}
        taskActions={actions}
        versionTrees={{}}
        planSteps={[]}
        planActiveId={null}
        convToTask={new Map()}
        onOpenFile={vi.fn()}
      />,
    );
  }

  it("completed 任务的核心操作按钮（regenerate）始终在 DOM 中——不依赖 hover", () => {
    // Phase 65: 触屏无 hover，按钮必须默认可见
    renderCompletedTask();
    const regen = screen.getByLabelText("chat.regenerate");
    expect(regen).toBeInTheDocument();
    // 按钮可点击（不依赖 CSS hover 状态）
    fireEvent.click(regen);
    expect(actions.onRegenerate).toHaveBeenCalled();
  });

  it(`completed 任务的"更多操作"下拉触发器存在（小屏入口）`, () => {
    renderCompletedTask();
    // Phase 65: 次要按钮收入"更多"下拉
    const moreBtn = screen.getByLabelText("phase65.moreActions");
    expect(moreBtn).toBeInTheDocument();
  });

  it(`PromptBubble 的"编辑重发"按钮有 aria-label`, () => {
    renderCompletedTask();
    // Phase 65: 无障碍——按钮必须有 aria-label
    const editBtn = screen.getByLabelText("chat.editResend");
    expect(editBtn).toBeInTheDocument();
  });

  it("所有 iconOnly 按钮有 aria-label（like/dislike/share）", () => {
    renderCompletedTask();
    // Phase 65: 无障碍——图标按钮必须有 aria-label
    expect(screen.getByLabelText("chat.like")).toBeInTheDocument();
    expect(screen.getByLabelText("chat.dislike")).toBeInTheDocument();
    expect(screen.getByLabelText("chat.share")).toBeInTheDocument();
  });

  it("点击 regenerate 调用 onRegenerate", () => {
    renderCompletedTask();
    fireEvent.click(screen.getByLabelText("chat.regenerate"));
    expect(actions.onRegenerate).toHaveBeenCalledWith(
      expect.objectContaining({ taskId: "t1" }),
    );
  });

  it("点击 fork 调用 onFork（核心按钮始终可见）", () => {
    renderCompletedTask();
    fireEvent.click(screen.getByLabelText("chat.fork"));
    expect(actions.onFork).toHaveBeenCalledWith("t1");
  });

  it("Phase 105: 长任务按 lifecycle.phase 显示角色徽章", () => {
    useTaskStore.getState().createTask("t1", "hermes", "hello", "", "conv-1");
    useTaskStore.getState().setTaskLongTask("t1");
    useTaskStore.getState().patchTaskLifecycle("t1", { phase: "auditor" });
    const task = useTaskStore.getState().tasks["t1"];
    render(
      <MessageItemView
        item={{ type: "task", id: "t1", task }}
        currentSessionId="session-1"
        lang="zh"
        t={(key: string) => key}
        inputRef={{ current: null }}
        taskActions={actions}
        versionTrees={{}}
        planSteps={[]}
        planActiveId={null}
        convToTask={new Map()}
        onOpenFile={vi.fn()}
      />,
    );
    expect(screen.getByText("task.role.reviewer")).toBeInTheDocument();
  });
});
