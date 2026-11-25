import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { ShortcutReferenceDialog } from "../ShortcutReferenceDialog";

// Mock shadcn Dialog wrapper to avoid base-ui portal complexity in jsdom
vi.mock("../ui/dialog", () => ({
  Dialog: ({ children, open }: { children: React.ReactNode; open?: boolean }) => (
    <div data-testid="dialog-root" data-open={open}>
      {children}
    </div>
  ),
  DialogContent: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="dialog-content">{children}</div>
  ),
  DialogHeader: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  DialogTitle: ({ children }: { children: React.ReactNode }) => <h2>{children}</h2>,
  DialogDescription: ({ children }: { children: React.ReactNode }) => <p>{children}</p>,
}));

describe("ShortcutReferenceDialog", () => {
  const onOpenChange = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
  });

  it("closed 时不渲染内容", () => {
    render(<ShortcutReferenceDialog open={false} onOpenChange={onOpenChange} />);
    expect(screen.queryByText("shortcutEditor.referenceTitle")).not.toBeInTheDocument();
  });

  it("open 时列出所有快捷键——默认全部禁用（空组合，对标 Linear 默认无快捷键）", () => {
    render(<ShortcutReferenceDialog open={true} onOpenChange={onOpenChange} />);
    expect(screen.getByText("shortcutEditor.referenceTitle")).toBeInTheDocument();
    expect(screen.getByText("shortcutEditor.commandPalette")).toBeInTheDocument();
    // SHORTCUT_DEFS 默认 defaultKeys 均为 ""——不再预置 Ctrl+K / Shift+?
    expect(screen.queryByText("Ctrl+K")).not.toBeInTheDocument();
    expect(screen.queryByText("Shift+?")).not.toBeInTheDocument();
  });

  it("读取用户自定义覆盖的组合", () => {
    localStorage.setItem("yuncat-shortcuts", JSON.stringify({ command_palette: "Ctrl+Shift+P" }));
    render(<ShortcutReferenceDialog open={true} onOpenChange={onOpenChange} />);
    expect(screen.getByText("Ctrl+Shift+P")).toBeInTheDocument();
  });
});
