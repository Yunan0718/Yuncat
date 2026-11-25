import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen } from "@testing-library/react";
import type { ReactNode } from "react";

// Task 6.3: ErrorBoundary 测试——mock i18next 让 t(key) 返回 key 本身，断言可预测
vi.mock("i18next", () => ({
  default: { t: (k: string) => k },
}));

import { ErrorBoundary } from "../ErrorBoundary";

// 故意抛错的子组件——shouldThrow 控制是否抛错
function ThrowOnProp({ shouldThrow, label = "ok" }: { shouldThrow: boolean; label?: string }) {
  if (shouldThrow) throw new Error("boom");
  return <div>{label}</div>;
}

// 不依赖 props、永远抛错的组件（用于验证 resetKeys 不变时不重置）
// 显式标注 ReactNode 返回类型——throw 路径为 never，可赋值给 ReactNode，满足 JSX 组件类型约束
function AlwaysThrow(): ReactNode {
  throw new Error("always");
}

describe("ErrorBoundary", () => {
  let errorSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    // React error boundary 触发时会 console.error 打印堆栈，测试中静音
    errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
  });

  afterEach(() => {
    errorSpy.mockRestore();
  });

  it("正常渲染 children", () => {
    render(
      <ErrorBoundary>
        <div>hello</div>
      </ErrorBoundary>,
    );
    expect(screen.getByText("hello")).toBeInTheDocument();
  });

  it("子组件抛错时显示错误 UI（renderError + error message）", () => {
    render(
      <ErrorBoundary>
        <ThrowOnProp shouldThrow={true} />
      </ErrorBoundary>,
    );
    expect(screen.getByText("error.renderError")).toBeInTheDocument();
    expect(screen.getByText("boom")).toBeInTheDocument();
  });

  it("显示'返回首页'链接 + '刷新页面'按钮", () => {
    render(
      <ErrorBoundary>
        <ThrowOnProp shouldThrow={true} />
      </ErrorBoundary>,
    );
    // 返回首页链接——href 指向根路径
    const backHomeLink = screen.getByText("error.backHome");
    expect(backHomeLink.tagName).toBe("A");
    expect(backHomeLink).toHaveAttribute("href", "/");
    // 刷新页面按钮
    expect(screen.getByText("error.refreshPage")).toBeInTheDocument();
  });

  it("resetKeys 变化时重置 error state，重新渲染 children", () => {
    const { rerender } = render(
      <ErrorBoundary resetKeys={["/chat"]}>
        <ThrowOnProp shouldThrow={true} />
      </ErrorBoundary>,
    );
    // 抛错 → 显示错误 UI
    expect(screen.getByText("error.renderError")).toBeInTheDocument();

    // resetKeys 变化 + 子组件不再抛错 → componentDidUpdate 重置 → 重新渲染 children
    rerender(
      <ErrorBoundary resetKeys={["/settings"]}>
        <ThrowOnProp shouldThrow={false} />
      </ErrorBoundary>,
    );
    expect(screen.getByText("ok")).toBeInTheDocument();
    expect(screen.queryByText("error.renderError")).not.toBeInTheDocument();
  });

  it("resetKeys 未变化时不重置（仍显示错误 UI）", () => {
    const { rerender } = render(
      <ErrorBoundary resetKeys={["/chat"]}>
        <AlwaysThrow />
      </ErrorBoundary>,
    );
    expect(screen.getByText("error.renderError")).toBeInTheDocument();

    // resetKeys 相同 → 不重置
    rerender(
      <ErrorBoundary resetKeys={["/chat"]}>
        <AlwaysThrow />
      </ErrorBoundary>,
    );
    expect(screen.getByText("error.renderError")).toBeInTheDocument();
  });

  it("未传 resetKeys 时不重置（向后兼容）", () => {
    const { rerender } = render(
      <ErrorBoundary>
        <ThrowOnProp shouldThrow={true} />
      </ErrorBoundary>,
    );
    expect(screen.getByText("error.renderError")).toBeInTheDocument();

    // 未传 resetKeys → componentDidUpdate 中 prevProps.resetKeys 与 this.props.resetKeys 均为 undefined，相等不重置
    rerender(
      <ErrorBoundary>
        <ThrowOnProp shouldThrow={false} />
      </ErrorBoundary>,
    );
    expect(screen.getByText("error.renderError")).toBeInTheDocument();
  });

  it("componentStack 显示（componentDidCatch 写入）", () => {
    render(
      <ErrorBoundary>
        <ThrowOnProp shouldThrow={true} />
      </ErrorBoundary>,
    );
    expect(screen.getByText("error.componentStack")).toBeInTheDocument();
  });
});
