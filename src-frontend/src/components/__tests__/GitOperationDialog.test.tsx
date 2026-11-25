import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { GitOperationDialog } from "../GitOperationDialog";

describe("GitOperationDialog", () => {
  it("确认按钮触发 onConfirm", () => {
    const onConfirm = vi.fn();
    const onCancel = vi.fn();
    render(
      <GitOperationDialog
        open={true}
        title="Confirm"
        message="Are you sure?"
        onConfirm={onConfirm}
        onCancel={onCancel}
      />,
    );
    fireEvent.click(screen.getByText("common.confirm"));
    expect(onConfirm).toHaveBeenCalled();
    expect(onCancel).not.toHaveBeenCalled();
  });

  it("取消按钮触发 onCancel", () => {
    const onConfirm = vi.fn();
    const onCancel = vi.fn();
    render(
      <GitOperationDialog
        open={true}
        title="Confirm"
        message="Are you sure?"
        onConfirm={onConfirm}
        onCancel={onCancel}
      />,
    );
    fireEvent.click(screen.getByText("common.cancel"));
    expect(onCancel).toHaveBeenCalled();
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("传入 input 时渲染输入框", () => {
    const onChange = vi.fn();
    render(
      <GitOperationDialog
        open={true}
        title="Input"
        message="Enter value"
        onConfirm={vi.fn()}
        onCancel={vi.fn()}
        input={{ value: "hello", onChange, placeholder: "type here" }}
      />,
    );
    const input = screen.getByDisplayValue("hello") as HTMLInputElement;
    expect(input).toBeInTheDocument();
    fireEvent.change(input, { target: { value: "world" } });
    expect(onChange).toHaveBeenCalledWith("world");
  });
});
