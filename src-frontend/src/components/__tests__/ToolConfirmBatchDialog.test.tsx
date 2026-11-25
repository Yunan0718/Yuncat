import { describe, it, expect, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { ToolConfirmBatchDialog } from "../ToolConfirmBatchDialog";

const items = [
  { toolName: "write_file", preview: { path: "src/app.ts" }, confirmToken: "tok-a" },
  { toolName: "run_command", preview: { command: "npm test" }, confirmToken: "tok-b" },
];

describe("ToolConfirmBatchDialog", () => {
  it("渲染标题和每个 item 的摘要", () => {
    render(<ToolConfirmBatchDialog open items={items} onConfirm={() => {}} />);
    expect(screen.getByText("toolConfirmBatch.title")).toBeInTheDocument();
    expect(screen.getByText("src/app.ts")).toBeInTheDocument();
    expect(screen.getByText("npm test")).toBeInTheDocument();
  });

  it("点击「全部执行」触发 onConfirm(true, false)", async () => {
    const onConfirm = vi.fn();
    render(<ToolConfirmBatchDialog open items={items} onConfirm={onConfirm} />);
    screen.getByText("toolConfirmBatch.approveAll").click();
    await waitFor(() => expect(onConfirm).toHaveBeenCalledWith(true, false));
  });

  it("点击「全部拒绝」触发 onConfirm(false, false)", async () => {
    const onConfirm = vi.fn();
    render(<ToolConfirmBatchDialog open items={items} onConfirm={onConfirm} />);
    screen.getByText("toolConfirmBatch.rejectAll").click();
    await waitFor(() => expect(onConfirm).toHaveBeenCalledWith(false, false));
  });

  it("勾选「始终允许」后批准触发 onConfirm(true, true)", async () => {
    const onConfirm = vi.fn();
    render(<ToolConfirmBatchDialog open items={items} onConfirm={onConfirm} />);
    fireEvent.click(screen.getByLabelText("toolConfirmBatch.alwaysAllowSession"));
    screen.getByText("toolConfirmBatch.approveAll").click();
    await waitFor(() => expect(onConfirm).toHaveBeenCalledWith(true, true));
  });

  it("Escape 键触发拒绝", async () => {
    const onConfirm = vi.fn();
    render(<ToolConfirmBatchDialog open items={items} onConfirm={onConfirm} />);
    fireEvent.keyDown(document, { key: "Escape" });
    await waitFor(() => expect(onConfirm).toHaveBeenCalledWith(false, false));
  });
});
