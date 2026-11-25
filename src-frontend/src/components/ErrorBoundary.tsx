import { Component, type ReactNode } from "react";
import i18next from "i18next";

interface Props {
  children: ReactNode;
  /**
   * Task 6.3: resetKeys——数组任意项变化时重置 error state。
   * 主流做法（对标 react-error-boundary）：传 [location.pathname] 让路由切换自动重置。
   */
  resetKeys?: unknown[];
}
interface State {
  hasError: boolean;
  error: Error | null;
  stack: string;
}

/** 浅比较两个数组——同长度且逐项 Object.is 相等 */
function areArraysEqual(a: unknown[] | undefined, b: unknown[] | undefined): boolean {
  if (a === b) return true;
  if (!a || !b) return false;
  if (a.length !== b.length) return false;
  return a.every((item, i) => Object.is(item, b[i]));
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { hasError: false, error: null, stack: "" };

  static getDerivedStateFromError(error: Error): Partial<State> {
    return { hasError: true, error };
  }
  componentDidCatch(error: Error, info: { componentStack: string }) {
    try {
      console.error("[ErrorBoundary]", error.message, info.componentStack);
    } catch {
      // no-op
    }
    this.setState({ stack: info.componentStack || "" });
  }

  componentDidUpdate(prevProps: Props) {
    // Task 6.3: resetKeys 变化时重置 error state——对标 react-error-boundary 标准行为
    if (this.state.hasError && !areArraysEqual(prevProps.resetKeys, this.props.resetKeys)) {
      this.setState({ hasError: false, error: null, stack: "" });
    }
  }

  render() {
    if (this.state.hasError) {
      return (
        <div
          className="flex flex-col items-center justify-start p-8 space-y-3 min-h-screen bg-background text-left"
          style={{ fontFamily: "monospace", fontSize: 12 }}
        >
          <h2 className="text-destructive text-base font-bold">{i18next.t("error.renderError")}</h2>
          <div className="bg-destructive/10 border border-destructive/20 rounded-lg p-4 w-full max-w-3xl">
            <div className="font-bold text-destructive mb-1">{this.state.error?.message}</div>
            <pre className="text-[10px] text-destructive/80 whitespace-pre-wrap max-h-96 overflow-auto">
              {this.state.error?.stack?.slice(0, 3000) || i18next.t("error.noStack")}
            </pre>
          </div>
          {this.state.stack && (
            <div className="bg-warning/10 border border-warning/20 rounded-lg p-4 w-full max-w-3xl">
              <div className="font-bold text-warning mb-1">{i18next.t("error.componentStack")}</div>
              <pre className="text-[10px] text-warning/80 whitespace-pre-wrap max-h-96 overflow-auto">
                {this.state.stack}
              </pre>
            </div>
          )}
          <div className="flex items-center gap-2">
            {/* Task 6.3: 返回首页——用原生 <a> 而非 router Link，因为 ErrorBoundary 兜底场景下
                router 上下文可能已损坏，整页导航到 / 更可靠（对标 react-error-boundary） */}
            <a
              href="/"
              className="px-4 py-2 border border-border text-foreground rounded text-sm hover:bg-secondary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
            >
              {i18next.t("error.backHome")}
            </a>
            <button
              onClick={() => {
                this.setState({ hasError: false, error: null, stack: "" });
                window.location.reload();
              }}
              className="px-4 py-2 bg-primary text-primary-foreground rounded text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
            >
              {i18next.t("error.refreshPage")}
            </button>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}
