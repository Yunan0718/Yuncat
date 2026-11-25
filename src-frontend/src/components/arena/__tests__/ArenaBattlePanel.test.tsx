import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { ArenaBattlePanel, RevealedInfo } from "../ArenaBattlePanel";

vi.mock("../../ResultRenderer", () => ({
  ResultRenderer: ({ content }: { content?: string }) => (
    <div data-testid="result-renderer">{content}</div>
  ),
}));

describe("ArenaBattlePanel", () => {
  const onVote = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders both responses with model labels", () => {
    render(
      <ArenaBattlePanel
        responseA="response from model A"
        responseB="response from model B"
        onVote={onVote}
      />,
    );

    expect(screen.getByText("arena.modelA")).toBeInTheDocument();
    expect(screen.getByText("arena.modelB")).toBeInTheDocument();

    const renderers = screen.getAllByTestId("result-renderer");
    expect(renderers[0]).toHaveTextContent("response from model A");
    expect(renderers[1]).toHaveTextContent("response from model B");
  });

  it("calls onVote with 'a' when vote A button clicked", () => {
    render(
      <ArenaBattlePanel
        responseA="A"
        responseB="B"
        onVote={onVote}
      />,
    );

    fireEvent.click(screen.getByText("arena.voteA"));
    expect(onVote).toHaveBeenCalledWith("a");
  });

  it("calls onVote with 'b' when vote B button clicked", () => {
    render(
      <ArenaBattlePanel
        responseA="A"
        responseB="B"
        onVote={onVote}
      />,
    );

    fireEvent.click(screen.getByText("arena.voteB"));
    expect(onVote).toHaveBeenCalledWith("b");
  });

  it("calls onVote with 'tie' when tie button clicked", () => {
    render(
      <ArenaBattlePanel
        responseA="A"
        responseB="B"
        onVote={onVote}
      />,
    );

    fireEvent.click(screen.getByText("arena.tie"));
    expect(onVote).toHaveBeenCalledWith("tie");
  });

  it("disables vote buttons after voted", () => {
    render(
      <ArenaBattlePanel
        responseA="A"
        responseB="B"
        onVote={onVote}
        voted
      />,
    );

    expect(screen.getByText("arena.voteA")).toBeDisabled();
    expect(screen.getByText("arena.voteB")).toBeDisabled();
    expect(screen.getByText("arena.tie")).toBeDisabled();
  });

  it("shows revealed model names and hides vote buttons", () => {
    const revealed: RevealedInfo = {
      modelA: "DeepSeek-V3 (deepseek)",
      modelB: "GPT-4o (openai)",
    };

    render(
      <ArenaBattlePanel
        responseA="A"
        responseB="B"
        onVote={onVote}
        revealed={revealed}
      />,
    );

    expect(screen.getByText("DeepSeek-V3 (deepseek)")).toBeInTheDocument();
    expect(screen.getByText("GPT-4o (openai)")).toBeInTheDocument();

    expect(screen.queryByText("arena.voteA")).not.toBeInTheDocument();
    expect(screen.queryByText("arena.voteB")).not.toBeInTheDocument();
    expect(screen.queryByText("arena.tie")).not.toBeInTheDocument();
  });
});
