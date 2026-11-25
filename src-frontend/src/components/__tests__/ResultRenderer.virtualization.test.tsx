/**
 * Task 6.11: ResultRenderer 虚拟化测试。
 *
 * 验证：
 * - 小内容（< threshold）→ 正常 Markdown 渲染（无 <details> 折叠）
 * - 大内容（> threshold）→ 默认折叠（<details>），显示预览 + 行数提示
 * - 大内容展开后 → 完整 Markdown 渲染（marked + DOMPurify）
 * - 大代码块 → 虚拟滚动容器（@tanstack/react-virtual）
 * - 分页：大内容展开后分页渲染，避免一次性 O(n²)
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { ResultRenderer } from "../ResultRenderer";

// Mock i18n
vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (k: string) => k }),
}));

// Mock recharts
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

// Mock InlineDiff
vi.mock("../workspace/InlineDiff", () => ({
  InlineDiff: () => <div data-testid="inline-diff" />,
}));

// 生成大内容
function generateLargeContent(lines: number): string {
  return Array.from({ length: lines }, (_, i) => `Line ${i + 1}: some content here`).join("\n");
}

describe("Task 6.11: ResultRenderer 虚拟化", () => {
  it("小内容（< threshold）正常渲染 Markdown，无 <details> 折叠", () => {
    const smallContent = "# Small\n\nThis is a short paragraph.";
    const { container } = render(
      <ResultRenderer content={smallContent} isStreaming={false} />,
    );
    expect(container.querySelector("h1")).not.toBeNull();
    expect(container.querySelector("details")).toBeNull();
  });

  it("大内容（> threshold）默认折叠，显示 <details>", () => {
    const largeContent = generateLargeContent(500); // ~10KB
    const { container } = render(
      <ResultRenderer content={largeContent} isStreaming={false} />,
    );
    const details = container.querySelector("details");
    expect(details).not.toBeNull();
    // 默认折叠
    expect(details?.hasAttribute("open")).toBe(false);
  });

  it("大内容折叠时显示行数提示", () => {
    const largeContent = generateLargeContent(300);
    render(
      <ResultRenderer content={largeContent} isStreaming={false} />,
    );
    // 应显示行数信息
    expect(screen.getByText(/300/i)).toBeTruthy();
  });

  it("大内容展开后渲染完整 Markdown", () => {
    const largeContent = `# Large Title\n\n${generateLargeContent(300)}`;
    const { container } = render(
      <ResultRenderer content={largeContent} isStreaming={false} />,
    );
    const details = container.querySelector("details");
    expect(details).not.toBeNull();

    // 展开前不应有 h1（折叠状态不渲染 markdown）
    expect(container.querySelector("h1")).toBeNull();

    // 点击展开
    const summary = details?.querySelector("summary");
    expect(summary).not.toBeNull();
    fireEvent.click(summary!);

    // 展开后应有 h1（markdown 渲染）
    expect(container.querySelector("h1")).not.toBeNull();
  });

  it("大代码块使用虚拟滚动容器", () => {
    const largeCode = "```python\n" + generateLargeContent(500) + "\n```";
    const { container } = render(
      <ResultRenderer content={largeCode} isStreaming={false} />,
    );
    // 大代码块应有虚拟滚动容器（data-testid="virtual-code-container"）
    const virtualContainer = container.querySelector('[data-testid="virtual-code-container"]');
    expect(virtualContainer).not.toBeNull();
  });

  it("小代码块不使用虚拟滚动", () => {
    const smallCode = "```python\nprint('hello')\n```";
    const { container } = render(
      <ResultRenderer content={smallCode} isStreaming={false} />,
    );
    const virtualContainer = container.querySelector('[data-testid="virtual-code-container"]');
    expect(virtualContainer).toBeNull();
  });

  it("流式期间大内容不折叠（节流富文本）", () => {
    const largeContent = generateLargeContent(500);
    const { container } = render(
      <ResultRenderer content={largeContent} isStreaming={true} />,
    );
    // 流式期间不折叠：CollapsibleMarkdown 是为已定稿内容设计的（默认收起 + 展开后
    // lazy 渲染），流式期间内容还在增长，收起会让用户盯着一个折叠框看不到任何输出。
    expect(container.querySelector("details")).toBeNull();
    // 但也不再退化成纯文本——10KB 远未到 STREAM_RICH_MAX_CHARS(50k)，走节流富文本
    expect(container.querySelector(".whitespace-pre-wrap")).toBeNull();
    expect(container.querySelector("p")).not.toBeNull();
  });
});
