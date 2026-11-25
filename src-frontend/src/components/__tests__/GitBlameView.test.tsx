import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { GitBlameView } from "../GitBlameView";

describe("GitBlameView", () => {
  const lines = [
    { hash: "abc1234", content: "first", author: "Alice", "author-time": "2 days ago", summary: "init" },
    { hash: "def5678", content: "second", author: "Bob", "author-time": "1 day ago", summary: "add" },
  ];

  it("渲染所有 blame 行", () => {
    render(<GitBlameView lines={lines} />);
    expect(screen.getByText("Alice")).toBeInTheDocument();
    expect(screen.getByText("Bob")).toBeInTheDocument();
    expect(screen.getByText("abc1234")).toBeInTheDocument();
    expect(screen.getByText("def5678")).toBeInTheDocument();
  });

  it("空数据时显示占位文案", () => {
    render(<GitBlameView lines={[]} />);
    expect(screen.getByText("gitPanel.blame.empty")).toBeInTheDocument();
  });
});
