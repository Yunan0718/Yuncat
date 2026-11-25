import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, act, cleanup } from "@testing-library/react";
import { ToolConfirmDialog } from "../ToolConfirmDialog";

/**
 * Batch 4b P3: 确认弹窗来源标识 + 剩余有效期倒计时
 *
 * - 头部显示来源任务摘要（taskPrompt，title 属性承载原文）
 * - expiresAt 提供时显示倒计时（data-remaining 秒数，1s interval 仅弹窗打开期间运行）
 * - 倒计时归零 → 自动 onConfirm(false) 关闭（不弹 404）
 * - 无 expiresAt → 不显示倒计时
 */
describe("Batch 4b P3: ToolConfirmDialog 来源 + 倒计时", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-01-01T00:00:00Z"));
  });

  afterEach(() => {
    cleanup();
    vi.useRealTimers();
  });

  const baseProps = {
    open: true,
    toolName: "run_command",
    preview: { command: "echo hi" },
  };

  it("显示来源任务摘要（sourceTask 行，title 承载原文）", () => {
    const prompt = "帮我重构登录模块并补充测试用例覆盖边界情况";
    render(
      <ToolConfirmDialog
        {...baseProps}
        taskPrompt={prompt}
        expiresAt={Date.now() / 1000 + 600}
        onConfirm={vi.fn()}
      />,
    );
    const source = screen.getByTestId("confirm-source-task");
    expect(source).toBeInTheDocument();
    // 全局 i18n mock 不做插值——title 属性承载 taskPrompt 原文
    expect(source.getAttribute("title")).toBe(prompt);
  });

  it("显示倒计时并每秒刷新（data-remaining 秒数）", () => {
    render(
      <ToolConfirmDialog
        {...baseProps}
        expiresAt={Date.now() / 1000 + 90}
        onConfirm={vi.fn()}
      />,
    );
    expect(screen.getByTestId("confirm-remaining").getAttribute("data-remaining")).toBe("90");

    act(() => {
      vi.advanceTimersByTime(60_000);
    });
    expect(screen.getByTestId("confirm-remaining").getAttribute("data-remaining")).toBe("30");
  });

  it("无 expiresAt 时不显示倒计时与来源行", () => {
    render(<ToolConfirmDialog {...baseProps} onConfirm={vi.fn()} />);
    expect(screen.queryByTestId("confirm-remaining")).toBeNull();
    expect(screen.queryByTestId("confirm-source-task")).toBeNull();
  });

  it("expiresAt 已过期 → 初始化即 0 并自动 onConfirm(false)", () => {
    const onConfirm = vi.fn();
    render(
      <ToolConfirmDialog
        {...baseProps}
        expiresAt={Date.now() / 1000 - 10}
        onConfirm={onConfirm}
      />,
    );
    expect(screen.getByTestId("confirm-remaining").getAttribute("data-remaining")).toBe("0");
    expect(onConfirm).toHaveBeenCalledWith(false);
  });

  it("倒计时归零 → 自动 onConfirm(false)（避免对过期 token 点确认收到 404）", () => {
    const onConfirm = vi.fn();
    render(
      <ToolConfirmDialog
        {...baseProps}
        expiresAt={Date.now() / 1000 + 3}
        onConfirm={onConfirm}
      />,
    );
    expect(onConfirm).not.toHaveBeenCalled();

    act(() => {
      vi.advanceTimersByTime(3_000);
    });
    expect(onConfirm).toHaveBeenCalledWith(false);
  });

  it("open=false 不渲染倒计时 interval（remaining 为 null）", () => {
    render(
      <ToolConfirmDialog
        {...baseProps}
        open={false}
        expiresAt={Date.now() / 1000 + 60}
        onConfirm={vi.fn()}
      />,
    );
    expect(screen.queryByTestId("confirm-remaining")).toBeNull();
  });
});
