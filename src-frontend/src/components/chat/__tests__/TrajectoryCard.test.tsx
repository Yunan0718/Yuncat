import { describe, expect, it } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

import { TrajectoryCard, TrajectoryRow } from "../TrajectoryCard";
import type { TrajectoryItem } from "../../../lib/trajectory";

const item = (over: Partial<TrajectoryItem> = {}): TrajectoryItem => ({
  id: "x", kind: "tool", title: "read_file a.py", detail: "print(1)",
  status: "done", collapsible: true, ...over,
});

describe("TrajectoryRow", () => {
  it("默认折叠：只显示一行摘要", () => {
    render(<TrajectoryRow item={item()} />);
    expect(screen.getByText("read_file a.py")).toBeInTheDocument();
    expect(screen.queryByTestId("trajectory-detail-tool")).toBeNull();
  });

  it("点击展开显示细节，再点收起", () => {
    render(<TrajectoryRow item={item()} />);
    fireEvent.click(screen.getByTestId("trajectory-row-tool"));
    expect(screen.getByTestId("trajectory-detail-tool")).toHaveTextContent("print(1)");
    fireEvent.click(screen.getByTestId("trajectory-row-tool"));
    expect(screen.queryByTestId("trajectory-detail-tool")).toBeNull();
  });

  it("没有细节时不可展开", () => {
    render(<TrajectoryRow item={item({ detail: "  " })} />);
    expect(screen.getByTestId("trajectory-row-tool")).toBeDisabled();
  });

  it("三种状态各有标识", () => {
    const { rerender } = render(<TrajectoryRow item={item({ status: "running" })} />);
    expect(screen.getByLabelText("running")).toBeInTheDocument();
    rerender(<TrajectoryRow item={item({ status: "failed" })} />);
    expect(screen.getByLabelText("failed")).toBeInTheDocument();
    rerender(<TrajectoryRow item={item({ status: "done" })} />);
    expect(screen.getByLabelText("done")).toBeInTheDocument();
  });
});

describe("TrajectoryCard", () => {
  it("过程信息成行渲染，正文作为 children 挂在卡片底部", () => {
    render(
      <TrajectoryCard items={[item({ id: "t", kind: "thinking", title: "思考过程" }), item()]}>
        <p>最终回答</p>
      </TrajectoryCard>,
    );
    expect(screen.getByTestId("trajectory-card")).toBeInTheDocument();
    expect(screen.getByText("思考过程")).toBeInTheDocument();
    expect(screen.getByText("最终回答")).toBeInTheDocument();
  });

  it("正文与失败不在这里成行（由底部与错误提示负责）", () => {
    render(
      <TrajectoryCard
        items={[item({ id: "m", kind: "message", title: "回复" }), item({ id: "e", kind: "error", title: "失败" })]}
      >
        <p>回答</p>
      </TrajectoryCard>,
    );
    expect(screen.queryByTestId("trajectory-row-message")).toBeNull();
    expect(screen.queryByTestId("trajectory-row-error")).toBeNull();
  });

  it("什么都没有时整张卡片不渲染", () => {
    const { container } = render(<TrajectoryCard items={[]} />);
    expect(container.firstChild).toBeNull();
  });
});
