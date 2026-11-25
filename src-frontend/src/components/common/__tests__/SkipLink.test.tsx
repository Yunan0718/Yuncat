import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { SkipLink } from "../SkipLink";

describe("SkipLink", () => {
  it("renders a link pointing to the target section", () => {
    render(<SkipLink targetId="main-content" label="Skip to main content" />);
    const link = screen.getByRole("link", { name: "Skip to main content" });
    expect(link).toHaveAttribute("href", "#main-content");
  });

  it("is visually hidden until focused", () => {
    render(<SkipLink targetId="main" label="Skip" />);
    const link = screen.getByRole("link", { name: "Skip" });
    expect(link).toHaveClass("sr-only");
    expect(link).toHaveClass("focus:not-sr-only");
  });
});
