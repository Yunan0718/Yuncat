import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ToastProvider } from "../Toast";
import { useToast } from "../../../hooks/use-toast";

function ToastHarness() {
  const { toast } = useToast();
  return (
    <>
      <button type="button" onClick={() => toast("saved", "success")}>success</button>
      <button type="button" onClick={() => toast("broken", "error")}>error</button>
      <button type="button" onClick={() => toast("info", "info")}>info</button>
      <button type="button" onClick={() => { for (let index = 0; index < 6; index += 1) toast(`item-${index}`, "info"); }}>many</button>
    </>
  );
}

function renderToasts() {
  return render(<ToastProvider><ToastHarness /></ToastProvider>);
}

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("Toast", () => {
  it("uses alert for errors and status for info/success", () => {
    renderToasts();
    fireEvent.click(screen.getByText("error"));
    fireEvent.click(screen.getByText("success"));
    expect(screen.getByRole("alert")).toHaveTextContent("broken");
    expect(screen.getAllByRole("status")).toHaveLength(1);
    expect(screen.getByRole("status")).toHaveTextContent("saved");
  });

  it("keeps at most five visible notifications", () => {
    renderToasts();
    fireEvent.click(screen.getByText("many"));
    expect(screen.getAllByRole("status")).toHaveLength(5);
    expect(screen.queryByText("item-0")).not.toBeInTheDocument();
    expect(screen.getByText("item-5")).toBeInTheDocument();
  });

  it("dismisses after the hold and exit animation", () => {
    vi.useFakeTimers();
    renderToasts();
    fireEvent.click(screen.getByText("info"));
    expect(screen.getByRole("status")).toBeInTheDocument();
    act(() => vi.advanceTimersByTime(4_000));
    act(() => vi.advanceTimersByTime(150));
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("pauses the dismissal timer while hovered", () => {
    vi.useFakeTimers();
    renderToasts();
    fireEvent.click(screen.getByText("info"));
    act(() => vi.advanceTimersByTime(3_000));
    fireEvent.mouseEnter(screen.getByRole("status"));
    act(() => vi.advanceTimersByTime(5_000));
    expect(screen.getByRole("status")).toBeInTheDocument();
    fireEvent.mouseLeave(screen.getByRole("status"));
    act(() => vi.advanceTimersByTime(1_000));
    act(() => vi.advanceTimersByTime(150));
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });
});
