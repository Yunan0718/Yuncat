import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { OptimizedPromptPreview } from "../OptimizedPromptPreview";
import type { OptimizeResult } from "../../../hooks/usePromptOptimize";

const onApply = vi.fn();
const onDiscard = vi.fn();

const RESULT: OptimizeResult = {
  optimized: "best prompt",
  changes: ["add context", "clear format"],
  score: 0.9,
  candidates: [
    { optimized: "best prompt", changes: ["add context", "clear format"], score: 0.9 },
    { optimized: "ok prompt", changes: ["minor tweak"], score: 0.7 },
    { optimized: "plain prompt", changes: [], score: 0.5 },
  ],
};

describe("OptimizedPromptPreview", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders original and optimized prompts", () => {
    render(
      <OptimizedPromptPreview
        rawPrompt="raw"
        result={RESULT}
        onApply={onApply}
        onDiscard={onDiscard}
      />,
    );

    expect(screen.getByText("raw")).toBeInTheDocument();
    expect(screen.getByText("best prompt")).toBeInTheDocument();
    expect(screen.getByText("add context")).toBeInTheDocument();
  });

  it("renders candidate buttons with scores", () => {
    render(
      <OptimizedPromptPreview
        rawPrompt="raw"
        result={RESULT}
        onApply={onApply}
        onDiscard={onDiscard}
      />,
    );

    const buttons = screen.getAllByTestId(/^optimize-candidate-/);
    expect(buttons).toHaveLength(3);
    expect(buttons[0]).toHaveTextContent("90%");
    expect(buttons[1]).toHaveTextContent("70%");
    expect(buttons[2]).toHaveTextContent("50%");
  });

  it("switches optimized text when clicking a different candidate", () => {
    render(
      <OptimizedPromptPreview
        rawPrompt="raw"
        result={RESULT}
        onApply={onApply}
        onDiscard={onDiscard}
      />,
    );

    fireEvent.click(screen.getByTestId("optimize-candidate-1"));
    expect(screen.getByText("ok prompt")).toBeInTheDocument();
    expect(screen.getByText("minor tweak")).toBeInTheDocument();
    expect(screen.queryByText("add context")).not.toBeInTheDocument();
  });

  it("applies the currently selected candidate", () => {
    render(
      <OptimizedPromptPreview
        rawPrompt="raw"
        result={RESULT}
        onApply={onApply}
        onDiscard={onDiscard}
      />,
    );

    fireEvent.click(screen.getByTestId("optimize-candidate-2"));
    fireEvent.click(screen.getByTestId("optimize-apply"));
    expect(onApply).toHaveBeenCalledWith("plain prompt");
  });

  it("discards when discard button clicked", () => {
    render(
      <OptimizedPromptPreview
        rawPrompt="raw"
        result={RESULT}
        onApply={onApply}
        onDiscard={onDiscard}
      />,
    );

    fireEvent.click(screen.getByTestId("optimize-discard"));
    expect(onDiscard).toHaveBeenCalled();
  });

  it("does not render candidate buttons for single candidate", () => {
    const single: OptimizeResult = {
      optimized: "only",
      changes: [],
      score: 1,
      candidates: [{ optimized: "only", changes: [], score: 1 }],
    };

    render(
      <OptimizedPromptPreview
        rawPrompt="raw"
        result={single}
        onApply={onApply}
        onDiscard={onDiscard}
      />,
    );

    expect(screen.queryByTestId("optimize-candidates")).not.toBeInTheDocument();
  });
});
