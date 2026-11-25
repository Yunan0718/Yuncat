import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { useTaskStore } from "../../store/taskStore";
import { MessageList } from "../chat/MessageList";
import type { TaskActions } from "../chat/MessageItem";

function createTask(id: string, prompt: string, chunks: string[] = [], convId?: string) {
  useTaskStore.getState().createTask(id, "hermes", prompt, "", convId);
  for (const chunk of chunks) {
    useTaskStore.getState().appendChunk(id, chunk);
  }
}

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

function renderMessageList(props?: { sessionLoading?: boolean; pendingPrompt?: string }) {
  return render(
    <MessageList
      sessionLoading={props?.sessionLoading ?? false}
      pendingPrompt={props?.pendingPrompt ?? ""}
      currentSessionId="session-1"
      lang="zh"
      t={(key: string) => key}
      messagesEndRef={{ current: document.createElement("div") }}
      inputRef={{ current: null }}
      taskActions={actions}
      versionTrees={{}}
    />,
  );
}

describe("MessageList", () => {
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

  it("渲染 3 条消息并显示消息文本", () => {
    createTask("t1", "第一条消息", ["回答 1"]);
    createTask("t2", "第二条消息", ["回答 2"]);
    createTask("t3", "第三条消息", ["回答 3"]);
    renderMessageList();
    expect(screen.getByText("第一条消息")).toBeInTheDocument();
    expect(screen.getByText("第二条消息")).toBeInTheDocument();
    expect(screen.getByText("第三条消息")).toBeInTheDocument();
    expect(screen.getByText("回答 1")).toBeInTheDocument();
  });

  it("pending prompt 渲染为乐观消息", () => {
    renderMessageList({ pendingPrompt: "正在发送…" });
    expect(screen.getByText("正在发送…")).toBeInTheDocument();
  });

  it("消息数超过 60 时使用虚拟滚动", () => {
    for (let i = 0; i < 61; i++) {
      createTask(`task-${i}`, `msg-${i}`);
    }
    renderMessageList();
    expect(screen.getByTestId("virtualized-message-list")).toBeInTheDocument();
  });

  it("FE1 迟滞：55 条（30-60 中间带）不切换虚拟化，避免阈值附近反复切换", () => {
    for (let i = 0; i < 55; i++) {
      createTask(`task-${i}`, `msg-${i}`);
    }
    renderMessageList();
    expect(screen.queryByTestId("virtualized-message-list")).not.toBeInTheDocument();
  });

  it("FE1 虚拟化模式下滚动容器唯一——外层不再 overflow-y-auto，由内层虚拟列表滚动", () => {
    for (let i = 0; i < 61; i++) {
      createTask(`task-${i}`, `msg-${i}`);
    }
    const { container } = renderMessageList();
    const root = container.firstChild as HTMLElement;
    expect(root.className).toContain("overflow-hidden");
    expect(root.className).not.toContain("overflow-y-auto");
    const virt = screen.getByTestId("virtualized-message-list");
    expect(virt.className).toContain("overflow-y-auto");
  });

  it("FE1 非虚拟化模式保持外层容器滚动", () => {
    createTask("t1", "hello");
    const { container } = renderMessageList();
    expect((container.firstChild as HTMLElement).className).toContain("overflow-y-auto");
  });

  it("右键菜单不导致崩溃", () => {
    createTask("t1", "hello", ["world"]);
    renderMessageList();
    const bubble = screen.getByText("hello");
    expect(() => fireEvent.contextMenu(bubble)).not.toThrow();
  });

  it("版本切换按钮不导致崩溃", () => {
    createTask("t1", "hello", ["world"], "conv-1");
    useTaskStore.getState().addTaskVersion("t1", {
      taskId: "v1",
      prompt: "v1 prompt",
      chunks: ["v1 chunk"],
      createdAt: Date.now(),
    });
    renderMessageList();
    const button = screen.getByRole("button", { name: "V1" });
    expect(() => fireEvent.click(button)).not.toThrow();
  });
});
