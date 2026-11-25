/**
 * 状态圆点无障碍测试。
 *
 * 背景（借鉴 deepseek-harness 的 StateDot）：Yuncat 原先多处用
 * `w-2 h-2 rounded-full bg-success/bg-destructive/…` 表示健康状态，四档状态
 * **全靠颜色区分**——色觉障碍用户与屏幕阅读器都无法分辨。
 * 这里的测试锁住「颜色只是装饰，语义必须由文本承载」这一约束。
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";

// vi.mock 会被提升到文件顶部，工厂内不能引用顶层变量——t 必须内联定义。
vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string): string => key,
    i18n: { language: "zh" },
  }),
}));

import { StatusDot, HealthDot } from "../StatusDot";

describe("StatusDot", () => {
  it("圆点 aria-hidden——颜色不承担语义", () => {
    const { container } = render(<StatusDot label="运行中" className="bg-success" />);
    const dot = container.querySelector('[aria-hidden="true"]');
    expect(dot).not.toBeNull();
    expect(dot?.className).toContain("bg-success");
  });

  it("状态以无障碍文本给出（sr-only + title）", () => {
    const { container } = render(<StatusDot label="运行中" className="bg-success" />);
    const text = screen.getByText("运行中");
    expect(text.className).toContain("sr-only");
    expect(container.querySelector("[title]")?.getAttribute("title")).toBe("运行中");
  });

  it("pulse 时加动画类", () => {
    const { container } = render(<StatusDot label="运行中" className="bg-success" pulse />);
    expect(container.querySelector('[aria-hidden="true"]')?.className).toContain("animate-pulse");
  });

  it("可覆盖尺寸", () => {
    const { container } = render(<StatusDot label="就绪" sizeClassName="w-2.5 h-2.5" />);
    expect(container.querySelector('[aria-hidden="true"]')?.className).toContain("w-2.5 h-2.5");
  });
});

describe("HealthDot", () => {
  it("四档状态各自映射到不同外观与无障碍文本", () => {
    const cases = [
      ["active", "bg-success", "health.active"],
      ["configured", "bg-success/60", "health.configured"],
      ["failed", "bg-destructive", "health.failed"],
    ] as const;

    for (const [status, cls, key] of cases) {
      const { container, unmount } = render(<HealthDot status={status} />);
      expect(container.querySelector('[aria-hidden="true"]')?.className).toContain(cls);
      expect(screen.getByText(key)).toBeInTheDocument();
      unmount();
    }
  });

  it("未配置 / 未知状态走兜底而不是空白", () => {
    const { container } = render(<HealthDot status="weird-status" />);
    expect(container.querySelector('[aria-hidden="true"]')?.className).toContain("border");
    expect(screen.getByText("health.unknown")).toBeInTheDocument();
  });

  it("status 为 undefined 时同样渲染兜底", () => {
    const { container } = render(<HealthDot />);
    expect(container.querySelector('[aria-hidden="true"]')).not.toBeNull();
    expect(screen.getByText("health.unknown")).toBeInTheDocument();
  });
});
