/**
 * ResultRenderer 流式渲染测试。
 *
 * 契约变更（2026-08-30，原 E4 优化被重新设计）：
 *
 * - **旧 E4**：流式期间一律纯文本（`isStreaming` → 跳过 marked+hljs+DOMPurify）。
 *   代价是整个响应期间用户看到的都是**裸 markdown**——代码围栏、表格管道符、
 *   强调星号全部原样可见，只有 stream_done 后才突然变成富文本。
 * - **现在**：流式期间渲染富文本，但按 120ms 节流重解析。
 *   解析次数与**时长**成正比，而非与 token 数成正比，避免 O(tokens × n) 开销；
 *   仅当内容超过 `STREAM_RICH_MAX_CHARS`(50k) 时才回退纯文本快路径。
 *
 * 验证：
 * - isStreaming=true → **Markdown 渲染**（不再是纯文本）
 * - isStreaming=false/undefined → Markdown 渲染（行为不变）
 * - 流式期间代码块触发 hljs 高亮（不再是裸文本）
 * - 流式期间大内容**不折叠**（CollapsibleMarkdown 只用于已定稿内容）
 * - 流式 + 超过 50k 字符 → 回退纯文本快路径
 */
import { describe, it, expect, vi } from "vitest";
import { render } from "@testing-library/react";
import { ResultRenderer } from "../ResultRenderer";

// Mock i18n
vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (k: string) => k }),
}));

// Mock recharts（避免重型依赖）
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

// Mock InlineDiff（避免 Monaco 加载）
vi.mock("../workspace/InlineDiff", () => ({
  InlineDiff: () => <div data-testid="inline-diff" />,
}));

describe("ResultRenderer 流式渲染", () => {
  it("isStreaming=true 渲染 Markdown（首帧直通，不等节流窗口）", () => {
    const markdown = "# Hello\n\n**bold** and `code`";
    const { container } = render(<ResultRenderer content={markdown} isStreaming={true} />);
    // 富文本：应产生 h1 / strong / code，而不是原样显示 **bold**
    expect(container.querySelector("h1")).not.toBeNull();
    expect(container.querySelector("strong")).not.toBeNull();
    expect(container.querySelector("code")).not.toBeNull();
  });

  it("isStreaming=false 渲染 Markdown HTML", () => {
    const { container } = render(
      <ResultRenderer content={"# Hello\n\n**bold** text"} isStreaming={false} />,
    );
    expect(container.querySelector("h1")).not.toBeNull();
    expect(container.querySelector("strong")).not.toBeNull();
  });

  it("isStreaming=undefined 默认 Markdown 渲染（向后兼容）", () => {
    const { container } = render(<ResultRenderer content={"# Title"} />);
    expect(container.querySelector("h1")).not.toBeNull();
  });

  it("流式期间代码块触发 Markdown + hljs 渲染", () => {
    const { container } = render(
      <ResultRenderer content={"Here is code:\n```python\nprint('hello')\n```\n"} isStreaming={true} />,
    );
    expect(container.querySelector("pre")).not.toBeNull();
    expect(container.querySelector("code")).not.toBeNull();
  });

  it("流式完成后代码块渲染（行为不变）", () => {
    const { container } = render(
      <ResultRenderer content={"```python\nprint('hello')\n```"} isStreaming={false} />,
    );
    expect(container.querySelector("pre")).not.toBeNull();
    expect(container.querySelector("code")).not.toBeNull();
  });

  it("流式期间 chunks 拼接后 Markdown 渲染", () => {
    const { container } = render(
      <ResultRenderer chunks={["# Heading\n\n", "Some **bold** text"]} isStreaming={true} />,
    );
    expect(container.querySelector("h1")).not.toBeNull();
    expect(container.querySelector("strong")).not.toBeNull();
  });

  it("流式完成后 chunks 拼接后 Markdown 渲染", () => {
    const { container } = render(
      <ResultRenderer chunks={["# Heading\n\n", "Some **bold** text"]} isStreaming={false} />,
    );
    expect(container.querySelector("h1")).not.toBeNull();
    expect(container.querySelector("strong")).not.toBeNull();
  });

  it("流式期间换行由 Markdown 承载（不再是 whitespace-pre-wrap 纯文本）", () => {
    const { container } = render(
      <ResultRenderer content={"line1\nline2\nline3"} isStreaming={true} />,
    );
    // 单个换行在 CommonMark 下是软换行，不产生 <br>；这里确认不再走纯文本分支
    expect(container.querySelector(".whitespace-pre-wrap")).toBeNull();
    expect(container.textContent).toContain("line1");
    expect(container.textContent).toContain("line3");
  });

  it("流式 + 超长内容（>50k）回退纯文本快路径", () => {
    const huge = "x".repeat(50_001);
    const { container } = render(<ResultRenderer content={huge} isStreaming={true} />);
    // 纯文本快路径：不折叠、不解析
    expect(container.querySelector("details")).toBeNull();
    expect(container.querySelector(".whitespace-pre-wrap")).not.toBeNull();
  });

  it("流式期间大内容（>5k）不折叠——折叠只用于已定稿内容", () => {
    const large = "para\n\n".repeat(1_200); // 约 7200 字符 > LARGE_CONTENT_CHARS
    expect(large.length).toBeGreaterThan(5_000);
    const { container } = render(<ResultRenderer content={large} isStreaming={true} />);
    expect(container.querySelector("details")).toBeNull();
  });

  it("定稿后大内容（>5k）仍然折叠", () => {
    const large = "para\n\n".repeat(1_200);
    const { container } = render(<ResultRenderer content={large} isStreaming={false} />);
    expect(container.querySelector("details")).not.toBeNull();
  });
});
