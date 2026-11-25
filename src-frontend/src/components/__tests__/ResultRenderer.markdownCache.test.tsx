/**
 * Batch 4a F2: renderMarkdown 缓存测试。
 *
 * 验证：
 * - 相同 content 跨组件实例二次渲染（组件级 useMemo 失效）命中模块级 LRU，
 *   marked.parse 不重复调用
 * - 同组件 rerender 相同 content 不重算（组件级 useMemo）
 * - 不同 content 正常重算
 */
import { describe, it, expect, vi } from "vitest";
import { render, fireEvent, waitFor } from "@testing-library/react";
import { marked } from "marked";
import { ResultRenderer } from "../ResultRenderer";

// Mock i18n
vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (k: string) => k }),
}));

// Mock recharts（对齐 ResultRenderer.virtualization.test.tsx）
vi.mock("recharts", () => ({
  ResponsiveContainer: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  BarChart: () => null,
  LineChart: () => null,
  PieChart: () => null,
  AreaChart: () => null,
  Bar: () => null,
  Line: () => null,
  Pie: () => null,
  Area: () => null,
  XAxis: () => null,
  YAxis: () => null,
  CartesianGrid: () => null,
  Tooltip: () => null,
  Cell: () => null,
  Legend: () => null,
}));

vi.mock("../workspace/InlineDiff", () => ({
  InlineDiff: () => <div data-testid="inline-diff" />,
}));

describe("Batch 4a F2: renderMarkdown LRU 缓存", () => {
  it("相同 content 跨组件实例二次渲染命中缓存——marked.parse 不重复调用", () => {
    const content = "# Cache Title\n\nHello **world** with some text.";
    const spy = vi.spyOn(marked, "parse");
    const { unmount } = render(<ResultRenderer content={content} isStreaming={false} />);
    expect(spy).toHaveBeenCalledTimes(1);

    unmount();
    // 新组件实例（组件级 useMemo 已失效）——模块级 LRU 命中
    render(<ResultRenderer content={content} isStreaming={false} />);
    expect(spy).toHaveBeenCalledTimes(1);
  });

  it("同组件 rerender 相同 content 不重算（组件级 useMemo）", () => {
    const spy = vi.spyOn(marked, "parse");
    const { rerender } = render(<ResultRenderer content="# Same" isStreaming={false} />);
    rerender(<ResultRenderer content="# Same" isStreaming={false} />);
    rerender(<ResultRenderer content="# Same" isStreaming={false} />);
    expect(spy).toHaveBeenCalledTimes(1);
  });

  it("不同 content 正常重算", () => {
    const spy = vi.spyOn(marked, "parse");
    render(<ResultRenderer content="# Alpha content" isStreaming={false} />);
    render(<ResultRenderer content="# Beta content" isStreaming={false} />);
    expect(spy).toHaveBeenCalledTimes(2);
  });
});

describe("FE2: document click 单一委托", () => {
  it("两个实例同时挂载时点击一次 copy-btn → clipboard.writeText 仅调用一次", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText },
      configurable: true,
    });
    // 缩进代码块走 marked code renderer（产出 .copy-btn）；``` 围栏会被
    // extractCodeBlocks 拦截走 plain code 分支（无 .copy-btn）
    const content = "说明:\n\n    print('hello')\n\n结束";
    render(
      <div>
        <ResultRenderer content={content} />
        <ResultRenderer content={content} />
      </div>,
    );
    const btn = document.querySelector(".copy-btn") as HTMLElement;
    expect(btn).toBeTruthy();
    fireEvent.click(btn);
    await waitFor(() => {
      expect(writeText).toHaveBeenCalledTimes(1);
      expect(writeText).toHaveBeenCalledWith("print('hello')");
    });
  });
});
