/**
 * v67: CJK 友好的 `**加粗**` 回归测试。
 *
 * 背景：CommonMark 要求强调的闭合边界为空白或标点，而中文没有词间空格，
 * 导致 `**注意！**后面是中文`、`**注：**说明` 这类高频写法在 marked 下
 * 退化为字面星号。修复方式是注册行内扩展 cjk-strong，仅在闭合 `**` 后
 * 紧跟 CJK 字符时接管。
 *
 * 验证：
 * - 原本失败的「闭合 ** 后紧跟 CJK」场景现在正确加粗
 * - 常规中英文加粗行为不变
 * - 代码 span 内的 ** 不被误伤
 * - 未闭合 ** 不产生加粗
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render } from "@testing-library/react";
import { ResultRenderer } from "../ResultRenderer";

vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (k: string) => k }),
}));

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

/** 渲染 content 并返回容器 HTML。 */
function renderHtml(content: string): string {
  const { container } = render(<ResultRenderer content={content} isStreaming={false} />);
  return container.innerHTML;
}

describe("v67: CJK 友好的 **加粗**", () => {
  beforeEach(() => {
    document.body.innerHTML = "";
  });

  // 修复目标：闭合 ** 的前一个字符是标点、后一个字符是 CJK
  it.each([
    ["**注意！**后面是中文", "注意！"],
    ["**结束。**继续说明", "结束。"],
    ["**TODO:**中文内容", "TODO:"],
    ["**注：**这里是中文", "注："],
    ["**第一项、**第二项", "第一项、"],
  ])("闭合 ** 后紧跟 CJK 时正确加粗：%s", (content, inner) => {
    const html = renderHtml(content);
    expect(html).toContain("<strong>");
    expect(html).toContain(inner);
    // 不应残留字面星号
    expect(html).not.toContain("**");
  });

  it("常规中文加粗行为不变", () => {
    const html = renderHtml("这是**加粗**的中文");
    expect(html).toContain("<strong>加粗</strong>");
  });

  it("英文加粗行为不变", () => {
    const html = renderHtml("**bold** then english");
    expect(html).toContain("<strong>bold</strong>");
  });

  it("代码 span 内的 ** 不被误伤", () => {
    const html = renderHtml("行内 `code **x**` 不该受影响");
    expect(html).toContain("<code>");
    expect(html).not.toContain("<strong>");
  });

  it("未闭合的 ** 不产生加粗", () => {
    const html = renderHtml("**未闭合的星号");
    expect(html).not.toContain("<strong>");
  });

  it("无加粗标记的普通中文不受影响", () => {
    const html = renderHtml("普通文本没有加粗");
    expect(html).toContain("普通文本没有加粗");
    expect(html).not.toContain("<strong>");
  });
});
