/**
 * SuggestionMenu.test.tsx —— Phase 64 通用下拉建议菜单测试
 *
 * 验证：
 * 1. visible=false 时不渲染
 * 2. items 渲染所有项
 * 3. ArrowDown/ArrowUp 循环高亮
 * 4. Enter 选中并触发 onSelect
 * 5. Escape 关闭
 * 6. 鼠标点击触发 onSelect
 * 7. 空列表显示 emptyText
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { SuggestionMenu } from "../chat/SuggestionMenu";

interface Item {
  id: string;
  label: string;
  desc: string;
}

const items: Item[] = [
  { id: "clear", label: "/clear", desc: "清空会话" },
  { id: "compact", label: "/compact", desc: "压缩上下文" },
  { id: "model", label: "/model", desc: "切换模型" },
];

function renderMenu(overrides: Partial<React.ComponentProps<typeof SuggestionMenu<Item>>> = {}) {
  const props: React.ComponentProps<typeof SuggestionMenu<Item>> = {
    items,
    visible: true,
    onSelect: vi.fn(),
    onClose: vi.fn(),
    getKey: (i) => i.id,
    getLabel: (i) => i.label,
    getDescription: (i) => i.desc,
    header: "Commands",
    ...overrides,
  };
  return { ...render(<SuggestionMenu {...props} />), props };
}

describe("SuggestionMenu", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("visible=false 时不渲染", () => {
    renderMenu({ visible: false });
    expect(screen.queryByText("Commands")).toBeNull();
    expect(screen.queryByText("/clear")).toBeNull();
  });

  it("渲染所有 items", () => {
    renderMenu();
    expect(screen.getByText("/clear")).toBeInTheDocument();
    expect(screen.getByText("/compact")).toBeInTheDocument();
    expect(screen.getByText("/model")).toBeInTheDocument();
  });

  it("渲染 header", () => {
    renderMenu({ header: "Commands" });
    expect(screen.getByText("Commands")).toBeInTheDocument();
  });

  it("空列表显示 emptyText", () => {
    renderMenu({ items: [], emptyText: "暂无命令" });
    expect(screen.getByText("暂无命令")).toBeInTheDocument();
  });

  it("ArrowDown 循环高亮（最后一项 → 第一项）", () => {
    renderMenu();
    // 初始高亮第 0 项
    const first = screen.getByText("/clear").closest("button");
    expect(first).toHaveAttribute("aria-selected", "true");
    // ArrowDown → 第 1 项
    fireEvent.keyDown(document, { key: "ArrowDown" });
    expect(screen.getByText("/compact").closest("button")).toHaveAttribute("aria-selected", "true");
    // ArrowDown → 第 2 项
    fireEvent.keyDown(document, { key: "ArrowDown" });
    expect(screen.getByText("/model").closest("button")).toHaveAttribute("aria-selected", "true");
    // ArrowDown → 循环回第 0 项
    fireEvent.keyDown(document, { key: "ArrowDown" });
    expect(screen.getByText("/clear").closest("button")).toHaveAttribute("aria-selected", "true");
  });

  it("ArrowUp 循环高亮（第一项 → 最后一项）", () => {
    renderMenu();
    // 初始第 0 项，ArrowUp → 循环到最后一项
    fireEvent.keyDown(document, { key: "ArrowUp" });
    expect(screen.getByText("/model").closest("button")).toHaveAttribute("aria-selected", "true");
  });

  it("Enter 触发 onSelect（高亮项）", () => {
    const { props } = renderMenu();
    // ArrowDown 一次 → 高亮 /compact
    fireEvent.keyDown(document, { key: "ArrowDown" });
    fireEvent.keyDown(document, { key: "Enter" });
    expect(props.onSelect).toHaveBeenCalledWith(items[1]);
  });

  it("Escape 触发 onClose", () => {
    const { props } = renderMenu();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(props.onClose).toHaveBeenCalledTimes(1);
  });

  it("鼠标点击触发 onSelect", () => {
    const { props } = renderMenu();
    fireEvent.click(screen.getByText("/model"));
    expect(props.onSelect).toHaveBeenCalledWith(items[2]);
  });

  it("鼠标 hover 切换高亮", () => {
    renderMenu();
    fireEvent.mouseEnter(screen.getByText("/compact"));
    expect(screen.getByText("/compact").closest("button")).toHaveAttribute("aria-selected", "true");
    // 此时按 Enter 应选中 /compact
    fireEvent.keyDown(document, { key: "ArrowDown" });
    // hover 后 ArrowDown 应从 /compact 推进到 /model
    expect(screen.getByText("/model").closest("button")).toHaveAttribute("aria-selected", "true");
  });

  it("local badge 渲染（renderBadge）", () => {
    renderMenu({
      renderBadge: (i) =>
        i.id === "clear" ? <span data-testid="badge-local">local</span> : null,
    });
    expect(screen.getByTestId("badge-local")).toBeInTheDocument();
  });
});
