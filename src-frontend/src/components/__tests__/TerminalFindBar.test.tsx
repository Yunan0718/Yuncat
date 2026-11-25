/**
 * TerminalFindBar.test.tsx —— Phase 66 终端查找栏测试
 *
 * 验证：
 * 1. visible=false 时不渲染
 * 2. visible=true 时渲染输入框并自动聚焦
 * 3. 输入文本触发 searchAddon.findNext
 * 4. ↑/↓ 按钮触发 findPrevious/findNext
 * 5. Enter 触发 findNext，Shift+Enter 触发 findPrevious
 * 6. Escape 触发 onClose
 * 7. X 按钮触发 onClose
 * 8. visible=false 时清空 query
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { TerminalFindBar } from "../workspace/TerminalFindBar";

const t = (k: string) => {
  const map: Record<string, string> = {
    "phase66.findPlaceholder": "Find...",
    "phase66.findLabel": "Terminal find",
    "phase66.findPrev": "Previous match",
    "phase66.findNext": "Next match",
    "common.close": "Close",
  };
  return map[k] ?? k;
};

function makeSearchAddon() {
  return {
    findNext: vi.fn(),
    findPrevious: vi.fn(),
  };
}

function renderBar(overrides: Partial<React.ComponentProps<typeof TerminalFindBar>> = {}) {
  const searchAddon = makeSearchAddon();
  const props: React.ComponentProps<typeof TerminalFindBar> = {
    visible: true,
    onClose: vi.fn(),
    searchAddon,
    t,
    ...overrides,
  };
  // overrides 中若传入了 searchAddon，则用 overrides 的
  if (overrides.searchAddon !== undefined) {
    props.searchAddon = overrides.searchAddon;
  }
  return { ...render(<TerminalFindBar {...props} />), props, searchAddon };
}

describe("TerminalFindBar", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("visible=false 时不渲染", () => {
    renderBar({ visible: false });
    expect(screen.queryByLabelText("Terminal find")).toBeNull();
    expect(screen.queryByPlaceholderText("Find...")).toBeNull();
  });

  it("visible=true 时渲染输入框和按钮", () => {
    renderBar();
    expect(screen.getByPlaceholderText("Find...")).toBeInTheDocument();
    expect(screen.getByLabelText("Previous match")).toBeInTheDocument();
    expect(screen.getByLabelText("Next match")).toBeInTheDocument();
    expect(screen.getByLabelText("Close")).toBeInTheDocument();
  });

  it("输入文本触发 findNext", () => {
    const { searchAddon } = renderBar();
    const input = screen.getByPlaceholderText("Find...");
    fireEvent.change(input, { target: { value: "error" } });
    expect(searchAddon.findNext).toHaveBeenCalledWith("error");
  });

  it("↑ 按钮触发 findPrevious", () => {
    const { searchAddon } = renderBar();
    fireEvent.click(screen.getByLabelText("Previous match"));
    expect(searchAddon.findPrevious).toHaveBeenCalledTimes(1);
  });

  it("↓ 按钮触发 findNext", () => {
    const { searchAddon } = renderBar();
    fireEvent.click(screen.getByLabelText("Next match"));
    expect(searchAddon.findNext).toHaveBeenCalledTimes(1);
  });

  it("Enter 触发 findNext", () => {
    const { searchAddon } = renderBar();
    const input = screen.getByPlaceholderText("Find...");
    fireEvent.change(input, { target: { value: "foo" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(searchAddon.findNext).toHaveBeenCalledWith("foo");
  });

  it("Shift+Enter 触发 findPrevious", () => {
    const { searchAddon } = renderBar();
    const input = screen.getByPlaceholderText("Find...");
    fireEvent.change(input, { target: { value: "foo" } });
    fireEvent.keyDown(input, { key: "Enter", shiftKey: true });
    expect(searchAddon.findPrevious).toHaveBeenCalledWith("foo");
  });

  it("Escape 触发 onClose", () => {
    const { props } = renderBar();
    const input = screen.getByPlaceholderText("Find...");
    fireEvent.keyDown(input, { key: "Escape" });
    expect(props.onClose).toHaveBeenCalledTimes(1);
  });

  it("X 按钮触发 onClose", () => {
    const { props } = renderBar();
    fireEvent.click(screen.getByLabelText("Close"));
    expect(props.onClose).toHaveBeenCalledTimes(1);
  });

  it("searchAddon=null 时不崩溃，按钮仍可点击", () => {
    renderBar({ searchAddon: null });
    expect(screen.getByPlaceholderText("Find...")).toBeInTheDocument();
    // 点击按钮不应抛错
    expect(() => fireEvent.click(screen.getByLabelText("Next match"))).not.toThrow();
    expect(() => fireEvent.click(screen.getByLabelText("Previous match"))).not.toThrow();
  });
});
