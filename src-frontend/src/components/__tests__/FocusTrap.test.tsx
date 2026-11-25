/**
 * FocusTrap.test.tsx —— 焦点陷阱外壳的横切行为
 *
 * 2026-09-15 a11y 审计发现：所有**自建模态**都复用这个外壳，但它只管焦点，
 * **没有锁定背景滚动** —— 弹窗打开时背后的长页面仍能滚动（键盘/滚轮都生效），
 * 与 Base UI 的 Dialog/Sheet（自带滚动锁）行为不一致。
 *
 * 修复放在这里而不是逐个组件里：一处修好，全部自建模态受益。
 * 本文件锁住三件事：打开时锁、关闭/卸载时**恢复原值**（不是硬写 ""）、非激活态不干预。
 */
import { describe, it, expect, afterEach } from "vitest";
import { render, cleanup } from "@testing-library/react";
import { FocusTrap } from "../FocusTrap";

afterEach(() => {
  cleanup();
  document.body.style.overflow = "";
});

describe("FocusTrap 滚动锁", () => {
  it("激活时锁定 body 滚动", () => {
    render(
      <FocusTrap active>
        <div>content</div>
      </FocusTrap>,
    );
    expect(document.body.style.overflow).toBe("hidden");
  });

  it("未激活时不干预 body 样式", () => {
    document.body.style.overflow = "auto";
    render(
      <FocusTrap active={false}>
        <div>content</div>
      </FocusTrap>,
    );
    expect(document.body.style.overflow).toBe("auto");
  });

  it("卸载后恢复**原有**值（而非硬写空串）", () => {
    document.body.style.overflow = "auto";
    const { unmount } = render(
      <FocusTrap active>
        <div>content</div>
      </FocusTrap>,
    );
    expect(document.body.style.overflow).toBe("hidden");

    unmount();
    // 关键：恢复成 "auto" 而不是 ""，否则会覆盖其它同样操作 body 样式的代码
    expect(document.body.style.overflow).toBe("auto");
  });

  it("active 从 true 变 false 时解锁", () => {
    const { rerender } = render(
      <FocusTrap active>
        <div>content</div>
      </FocusTrap>,
    );
    expect(document.body.style.overflow).toBe("hidden");

    rerender(
      <FocusTrap active={false}>
        <div>content</div>
      </FocusTrap>,
    );
    expect(document.body.style.overflow).toBe("");
  });
});
