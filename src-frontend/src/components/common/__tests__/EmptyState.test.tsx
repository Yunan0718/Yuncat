/**
 * Task 7.19: EmptyState 组件契约测试
 *
 * 验证 components/common/EmptyState 的 props 渲染行为——
 * 作为统一空状态组件，后续页面改造（SessionList/AgentManager/PromptManager 等）
 * 都依赖此契约，必须先固化。
 *
 * 对标 shadcn/ui / Radix Empty pattern 的组件测试惯例。
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { EmptyState } from "../EmptyState";

describe("Task 7.19: EmptyState 组件契约", () => {
  it("渲染 title（必填）", () => {
    render(<EmptyState title="暂无数据" />);
    expect(screen.getByText("暂无数据")).toBeInTheDocument();
  });

  it("未提供 description 时不渲染描述段落", () => {
    render(<EmptyState title="暂无数据" />);
    // EmptyState 容器内只有一个 h3 标题，没有 p 描述
    const container = screen.getByText("暂无数据").parentElement!;
    expect(container.querySelector("p")).toBeNull();
  });

  it("提供 description 时渲染描述", () => {
    render(<EmptyState title="暂无数据" description="请先创建新会话" />);
    expect(screen.getByText("请先创建新会话")).toBeInTheDocument();
  });

  it("未提供 icon 时不渲染图标容器", () => {
    render(<EmptyState title="暂无数据" />);
    // EmptyState 在 icon 存在时渲染 rounded-full 容器
    const container = screen.getByText("暂无数据").parentElement!;
    expect(container.querySelector(".rounded-full")).toBeNull();
  });

  it("提供 icon 时渲染图标", () => {
    render(<EmptyState title="暂无数据" icon={<span data-testid="test-icon">📦</span>} />);
    expect(screen.getByTestId("test-icon")).toBeInTheDocument();
  });

  it("未提供 action 时不渲染按钮", () => {
    render(<EmptyState title="暂无数据" />);
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("提供 action 时渲染按钮并触发 onClick", () => {
    const onClick = vi.fn();
    render(<EmptyState title="暂无数据" action={{ label: "刷新", onClick }} />);
    const btn = screen.getByRole("button", { name: "刷新" });
    fireEvent.click(btn);
    expect(onClick).toHaveBeenCalledOnce();
  });

  it("compact 模式应用更小的 padding 类", () => {
    const { container } = render(<EmptyState title="暂无数据" compact />);
    // compact 模式根 div 应包含 py-6（而非 py-10）
    const root = container.firstElementChild as HTMLElement;
    expect(root.className).toContain("py-6");
    expect(root.className).not.toContain("py-10");
  });

  it("非 compact 模式应用默认 padding 类", () => {
    const { container } = render(<EmptyState title="暂无数据" />);
    const root = container.firstElementChild as HTMLElement;
    expect(root.className).toContain("py-10");
  });
});
