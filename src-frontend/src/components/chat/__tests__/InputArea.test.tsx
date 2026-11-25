import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { InputArea } from "../InputArea";
import type { ChatAttachment } from "../types";

const mockOnSend = vi.fn();
const mockOnCancelRunning = vi.fn();

vi.mock("../../../store/taskStore", () => ({
  useTaskStore: (selector?: (s: unknown) => unknown) => {
    const state = {
      contextUsage: 0.1,
      tasks: { t1: { status: "running" } },
    };
    return selector ? selector(state) : state;
  },
}));

vi.mock("../../../hooks/usePromptOptimize", () => ({
  usePromptOptimize: () => ({
    optimize: vi.fn(),
    loading: false,
    result: null,
    error: null,
    reset: vi.fn(),
  }),
}));

vi.mock("../../../hooks/use-toast", () => ({
  useToast: () => ({ toast: vi.fn() }),
}));

let shortcutOverrides: Record<string, string> = {};

vi.mock("../../../lib/shortcuts", () => ({
  matchShortcut: (e: KeyboardEvent, id: string) => {
    if (id !== "send_message") return false;
    const combo = shortcutOverrides[id] || "Enter";
    const needsCtrl = combo.includes("Ctrl");
    const needsShift = combo.includes("Shift");
    const needsEnter = combo.includes("Enter");
    const ctrlOk = needsCtrl === (e.ctrlKey || e.metaKey);
    const shiftOk = needsShift === e.shiftKey;
    const enterOk = !needsEnter || e.key === "Enter";
    return ctrlOk && shiftOk && enterOk;
  },
}));

function renderInputArea(props: Partial<React.ComponentProps<typeof InputArea>> = {}) {
  const defaultProps: React.ComponentProps<typeof InputArea> = {
    input: "",
    setInput: vi.fn(),
    sending: false,
    attachmentPreviews: [] as ChatAttachment[],
    setAttachmentPreviews: vi.fn(),
    atMenu: { visible: false, query: "", cursorPos: 0 },
    setAtMenu: vi.fn(),
    sessionsForAt: [],
    agents: [],
    inputRef: { current: null },
    fileInputRef: { current: null },
    onSend: mockOnSend,
    onCompact: vi.fn(),
    onCancelRunning: mockOnCancelRunning,
    handleAtAutocomplete: vi.fn(),
    onAddAttachments: vi.fn(),
    t: (k: string) => k,
    slashMenu: { visible: false, query: "", cursorPos: 0 },
    setSlashMenu: vi.fn(),
    commands: [],
    securityPreset: "workspace_write",
    onSecurityPresetChange: vi.fn(),
  };
  return render(<InputArea {...defaultProps} {...props} />);
}

describe("InputArea", () => {
  beforeEach(() => {
    mockOnSend.mockClear();
    mockOnCancelRunning.mockClear();
    shortcutOverrides = {};
  });

  it("Batch 3.8: 默认 Enter 发送", () => {
    renderInputArea();
    const textarea = screen.getByPlaceholderText("chat.placeholder") as HTMLTextAreaElement;
    fireEvent.keyDown(textarea, { key: "Enter" });
    expect(mockOnSend).toHaveBeenCalledTimes(1);
  });

  it("Batch 3.8: Shift+Enter 换行，不发送", () => {
    renderInputArea();
    const textarea = screen.getByPlaceholderText("chat.placeholder") as HTMLTextAreaElement;
    fireEvent.keyDown(textarea, { key: "Enter", shiftKey: true });
    expect(mockOnSend).not.toHaveBeenCalled();
  });

  it("Batch 3.8: 用户覆盖为 Ctrl+Enter 后，Ctrl+Enter 发送而 Enter 不发送", () => {
    shortcutOverrides = { send_message: "Ctrl+Enter" };
    renderInputArea();
    const textarea = screen.getByPlaceholderText("chat.placeholder") as HTMLTextAreaElement;
    fireEvent.keyDown(textarea, { key: "Enter" });
    expect(mockOnSend).not.toHaveBeenCalled();
    fireEvent.keyDown(textarea, { key: "Enter", ctrlKey: true });
    expect(mockOnSend).toHaveBeenCalledTimes(1);
  });

  it("Batch 3.8: / 命令菜单可见时 Enter 不发送", () => {
    renderInputArea({ slashMenu: { visible: true, query: "", cursorPos: 0 } });
    const textarea = screen.getByPlaceholderText("chat.placeholder") as HTMLTextAreaElement;
    fireEvent.keyDown(textarea, { key: "Enter", ctrlKey: true });
    expect(mockOnSend).not.toHaveBeenCalled();
  });

  it("Batch 3.8: Escape 在有运行任务时触发取消", () => {
    renderInputArea();
    const textarea = screen.getByPlaceholderText("chat.placeholder") as HTMLTextAreaElement;
    fireEvent.keyDown(textarea, { key: "Escape" });
    expect(mockOnCancelRunning).toHaveBeenCalledTimes(1);
  });

  it("Batch 4.3: @ 菜单显示 folder/web/git 提示项", () => {
    const setInput = vi.fn();
    const setAtMenu = vi.fn();
    renderInputArea({
      input: "@",
      atMenu: { visible: true, query: "", cursorPos: 0 },
      setAtMenu,
      setInput,
    });
    expect(screen.getByText(/phase49\.inputArea\.folderHint/)).toBeInTheDocument();
    expect(screen.getByText(/phase49\.inputArea\.webHint/)).toBeInTheDocument();
    expect(screen.getByText(/phase49\.inputArea\.gitHint/)).toBeInTheDocument();
  });

  it("Batch 4.3: 点击 @folder 提示插入引用文本", () => {
    const setInput = vi.fn();
    const setAtMenu = vi.fn();
    renderInputArea({
      input: "",
      atMenu: { visible: true, query: "", cursorPos: 0 },
      setAtMenu,
      setInput,
    });
    fireEvent.click(screen.getByText(/phase49\.inputArea\.folderHint/));
    expect(setInput).toHaveBeenCalledWith("@folder:phase49.inputArea.folderHint ");
    expect(setAtMenu).toHaveBeenCalledWith({ visible: false, query: "", cursorPos: 0 });
  });

  it("Batch 4.3: @ 菜单按 query 过滤提示项", () => {
    const setAtMenu = vi.fn();
    renderInputArea({
      input: "@fol",
      atMenu: { visible: true, query: "fol", cursorPos: 0 },
      setAtMenu,
    });
    expect(screen.getByText(/phase49\.inputArea\.folderHint/)).toBeInTheDocument();
    expect(screen.queryByText(/phase49\.inputArea\.webHint/)).not.toBeInTheDocument();
  });
});
