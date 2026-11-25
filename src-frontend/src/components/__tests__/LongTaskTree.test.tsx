/**
 * LongTaskTree.test.tsx —— Phase 82 长任务分解树测试
 *
 * 验证：
 * 1. 空数组返回 null
 * 2. 渲染单节点 + 父子节点
 * 3. currentStepId 高亮当前节点（ring-2 ring-primary）
 * 4. 6 种状态色标（running/completed/failed 等）
 * 5. 点击节点触发 onSelect
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { LongTaskTree } from "../LongTaskTree";
import type { PlanTreeNode } from "../../store/taskStore";

const makeNode = (overrides: Partial<PlanTreeNode> = {}): PlanTreeNode => ({
  id: "n1",
  title: "Step 1",
  status: "pending",
  level: 0,
  parent_id: null,
  ...overrides,
});

describe("LongTaskTree", () => {
  it("空数组返回 null", () => {
    const { container } = render(<LongTaskTree nodes={[]} currentStepId={null} />);
    expect(container.firstChild).toBeNull();
  });

  it("渲染单节点", () => {
    render(<LongTaskTree nodes={[makeNode()]} currentStepId={null} />);
    expect(screen.getByText("Step 1")).toBeInTheDocument();
  });

  it("渲染父子节点——两个节点都出现", () => {
    const nodes: PlanTreeNode[] = [
      makeNode({ id: "root", title: "Root", parent_id: null }),
      makeNode({ id: "child", title: "Child", parent_id: "root", level: 1 }),
    ];
    render(<LongTaskTree nodes={nodes} currentStepId={null} />);
    expect(screen.getByText("Root")).toBeInTheDocument();
    expect(screen.getByText("Child")).toBeInTheDocument();
  });

  it("currentStepId 高亮当前节点（ring-2 ring-primary）", () => {
    render(<LongTaskTree nodes={[makeNode({ id: "n1" })]} currentStepId="n1" />);
    const button = screen.getByText("Step 1").closest("button");
    expect(button?.className).toContain("ring-2");
    expect(button?.className).toContain("ring-primary");
  });

  it("running 状态有 animate-pulse", () => {
    render(<LongTaskTree nodes={[makeNode({ status: "running" })]} currentStepId={null} />);
    const button = screen.getByText("Step 1").closest("button");
    expect(button?.className).toContain("animate-pulse");
  });

  it("completed 状态有 success 色标", () => {
    render(<LongTaskTree nodes={[makeNode({ status: "completed" })]} currentStepId={null} />);
    const button = screen.getByText("Step 1").closest("button");
    expect(button?.className).toContain("bg-success/10");
  });

  it("failed 状态有 destructive 色标", () => {
    render(<LongTaskTree nodes={[makeNode({ status: "failed" })]} currentStepId={null} />);
    const button = screen.getByText("Step 1").closest("button");
    expect(button?.className).toContain("bg-destructive/10");
  });

  it("blocked 状态有 warning 色标", () => {
    render(<LongTaskTree nodes={[makeNode({ status: "blocked" })]} currentStepId={null} />);
    const button = screen.getByText("Step 1").closest("button");
    expect(button?.className).toContain("bg-warning/10");
  });

  it("skipped 状态有 line-through", () => {
    render(<LongTaskTree nodes={[makeNode({ status: "skipped" })]} currentStepId={null} />);
    const button = screen.getByText("Step 1").closest("button");
    expect(button?.className).toContain("line-through");
  });

  it("点击节点触发 onSelect 回调", () => {
    const onSelect = vi.fn();
    render(<LongTaskTree nodes={[makeNode()]} currentStepId={null} onSelect={onSelect} />);
    fireEvent.click(screen.getByText("Step 1"));
    expect(onSelect).toHaveBeenCalledWith("n1");
  });

  it("没有 onDecompose 时不显示重分解按钮", () => {
    render(<LongTaskTree nodes={[makeNode()]} currentStepId={null} />);
    expect(screen.queryByLabelText("supervisor.decompose")).not.toBeInTheDocument();
  });

  it("提供 onDecompose 时每个节点都能重分解", () => {
    const onDecompose = vi.fn();
    render(
      <LongTaskTree nodes={[makeNode()]} currentStepId={null} onDecompose={onDecompose} />,
    );
    fireEvent.click(screen.getByLabelText("supervisor.decompose"));
    expect(onDecompose).toHaveBeenCalledTimes(1);
    expect(onDecompose.mock.calls[0][0].id).toBe("n1");
  });
});
