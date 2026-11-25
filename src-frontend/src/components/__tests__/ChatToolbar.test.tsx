import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { ChatToolbar } from "../chat/ChatToolbar";
import type { ChatMode } from "../chat/types";

vi.mock("../ModelSelector", () => ({
  default: () => <div data-testid="model-selector">ModelSelector</div>,
}));

describe("ChatToolbar", () => {
  const defaultProps = {
    leftCollapsed: true,
    onToggleLeft: vi.fn(),
    rightCollapsed: true,
    onToggleRight: vi.fn(),
    selectedAgent: "",
    setSelectedAgent: vi.fn(),
    agents: [
      { id: "hermes", name: "Hermes" },
      { id: "openclaw", name: "OpenClaw" },
    ],
    mode: "agent" as ChatMode,
    setMode: vi.fn(),
    input: "",
    showSearch: false,
    setShowSearch: vi.fn(),
    currentModelName: "",
    termCollapsed: true,
    setTermCollapsed: vi.fn(),
    autoApprove: false,
    backendDead: false,
    onNewChat: vi.fn(),
    t: (key: string) => key,
  };

  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("渲染工具栏核心按钮", () => {
    render(<ChatToolbar {...defaultProps} />);
    expect(screen.getByLabelText("chat.fileBrowser")).toBeInTheDocument();
    expect(screen.getByLabelText("chat.newChat")).toBeInTheDocument();
    expect(screen.getByText("chat.terminal")).toBeInTheDocument();
    expect(screen.getByText("chat.mode_agent")).toBeInTheDocument();
  });

  it("点击文件夹按钮触发 onToggleLeft", () => {
    render(<ChatToolbar {...defaultProps} />);
    fireEvent.click(screen.getByLabelText("chat.fileBrowser"));
    expect(defaultProps.onToggleLeft).toHaveBeenCalledTimes(1);
  });

  it("点击 New chat 按钮触发 onNewChat", () => {
    render(<ChatToolbar {...defaultProps} />);
    fireEvent.click(screen.getByLabelText("chat.newChat"));
    expect(defaultProps.onNewChat).toHaveBeenCalledTimes(1);
  });

  it("点击终端按钮触发 setTermCollapsed", () => {
    render(<ChatToolbar {...defaultProps} />);
    fireEvent.click(screen.getByText("chat.terminal"));
    expect(defaultProps.setTermCollapsed).toHaveBeenCalledTimes(1);
  });

  it("点击模式按钮触发 setMode", () => {
    render(<ChatToolbar {...defaultProps} />);
    fireEvent.click(screen.getByText("chat.mode_ask"));
    expect(defaultProps.setMode).toHaveBeenCalledWith("ask");
  });

  it("模式按钮显示 agent/ask/long_task 三个模式，不显示 plan/spec/deliberation（plan 隐藏 AI 判断，deliberation 由 AI 自动触发）", () => {
    render(<ChatToolbar {...defaultProps} />);
    expect(screen.getByText("chat.mode_agent")).toBeInTheDocument();
    expect(screen.getByText("chat.mode_ask")).toBeInTheDocument();
    expect(screen.getByText("chat.mode_long_task")).toBeInTheDocument();
    expect(screen.queryByText("chat.mode_plan")).not.toBeInTheDocument();
    expect(screen.queryByText("chat.mode_spec")).not.toBeInTheDocument();
    expect(screen.queryByText("chat.mode_deliberation")).not.toBeInTheDocument();
  });
});
