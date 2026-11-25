import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { EmptyMessages } from "../MessageItem";

// Phase 9-X: 空会话首屏引导 chips（DSH 式）。
// 仅在传入 onSuggestion 时渲染一行轻量建议药丸，点击即发送对应 prompt。

// t 返回 key 本身，便于断言按钮文案与回调入参
const t = (key: string): string => key;

describe("EmptyMessages suggestions", () => {
  it("未传入 onSuggestion 时：保持极简空状态，无任何可点击元素", () => {
    const { container } = render(<EmptyMessages sessionLoading={false} t={t} />);
    expect(container.querySelector("button")).toBeNull();
    expect(container.querySelector("a")).toBeNull();
  });

  it("传入 onSuggestion 时：渲染 4 个建议 chip", () => {
    const { container } = render(
      <EmptyMessages sessionLoading={false} t={t} onSuggestion={() => {}} />,
    );
    expect(container.querySelectorAll('[data-testid="suggestion-chip"]')).toHaveLength(4);
  });

  it("点击 chip 调用 onSuggestion 并带上对应文案", () => {
    const onSuggestion = vi.fn();
    render(<EmptyMessages sessionLoading={false} t={t} onSuggestion={onSuggestion} />);
    const chips = screen.getAllByTestId("suggestion-chip");
    chips[0].click();
    expect(onSuggestion).toHaveBeenCalledWith("chat.chipCode");
    chips[1].click();
    expect(onSuggestion).toHaveBeenCalledWith("chat.chipExplain");
  });

  it("加载中：渲染骨架屏且不渲染建议 chip", () => {
    const { container } = render(
      <EmptyMessages sessionLoading={true} t={t} onSuggestion={() => {}} />,
    );
    expect(container.querySelectorAll('[data-testid="suggestion-chip"]')).toHaveLength(0);
  });

  it("渲染欢迎文案（welcomeHeading / emptyDesc）", () => {
    render(<EmptyMessages sessionLoading={false} t={t} />);
    expect(screen.getByText("chat.welcomeHeading")).toBeInTheDocument();
    expect(screen.getByText("chat.emptyDesc")).toBeInTheDocument();
  });
});
