import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { NotificationBell } from "../NotificationBell";
import { useAppStore, type AppNotification } from "../../store/appStore";

const mocks = vi.hoisted(() => ({
  api: vi.fn(),
  toast: vi.fn(),
  isAbortedError: vi.fn(),
}));

vi.mock("../../hooks/useApi", () => ({
  useApi: () => ({ api: mocks.api }),
  isAbortedError: mocks.isAbortedError,
}));

vi.mock("../../hooks/use-toast", () => ({
  useToast: () => ({ toast: mocks.toast }),
}));

const UNREAD: AppNotification = {
  id: "notification-1",
  type: "task_completed",
  title: "Task complete",
  body: "The task finished",
  link: "",
  level: "success",
  read: false,
  created_at: 1_700_000_000,
};

function ensureOverlayRoot() {
  let root = document.getElementById("overlay-root");
  if (!root) {
    root = document.createElement("div");
    root.id = "overlay-root";
    document.body.appendChild(root);
  }
  return root;
}

function renderBell() {
  return render(<MemoryRouter><NotificationBell /></MemoryRouter>);
}

beforeEach(() => {
  ensureOverlayRoot();
  mocks.api.mockReset();
  mocks.toast.mockReset();
  mocks.isAbortedError.mockReset();
  mocks.isAbortedError.mockReturnValue(false);
  mocks.api.mockResolvedValue([]);
  useAppStore.setState({ notifications: [UNREAD] });
});

describe("NotificationBell", () => {
  it("opening the panel does not silently mark unread notifications as read", async () => {
    renderBell();
    fireEvent.click(screen.getByRole("button", { name: "phase106.unreadCount" }));
    expect(screen.getByRole("dialog", { name: "legacy.notifBell_f831" })).toBeInTheDocument();
    expect(screen.getByText("Task complete")).toBeInTheDocument();
    expect(useAppStore.getState().notifications[0].read).toBe(false);
    expect(mocks.api).not.toHaveBeenCalledWith("/api/v1/notifications/read-all", expect.anything());
  });

  it("marks all notifications as read only after the explicit action succeeds", async () => {
    mocks.api.mockImplementation((url: string) => {
      if (url === "/api/v1/notifications/read-all") return Promise.resolve({ status: "ok" });
      return Promise.resolve([]);
    });
    renderBell();
    fireEvent.click(screen.getByRole("button", { name: "phase106.unreadCount" }));
    fireEvent.click(screen.getByText("phase106.markAllRead"));
    await waitFor(() => expect(useAppStore.getState().notifications[0].read).toBe(true));
    expect(mocks.api).toHaveBeenCalledWith("/api/v1/notifications/read-all", { method: "POST" });
  });

  it("exposes delete as a separate keyboard-accessible button", () => {
    renderBell();
    fireEvent.click(screen.getByRole("button", { name: "phase106.unreadCount" }));
    expect(screen.getByRole("button", { name: "common.delete" })).toBeInTheDocument();
  });

  it("renders the notification panel through overlay-root", async () => {
    renderBell();
    fireEvent.click(screen.getByRole("button", { name: "phase106.unreadCount" }));
    const dialog = screen.getByRole("dialog", { name: "legacy.notifBell_f831" });
    expect(document.getElementById("overlay-root")).toContainElement(dialog);
  });

  it("ignores an aborted initial notification load", async () => {
    const aborted = Object.assign(new Error("Request was aborted"), { aborted: true });
    mocks.isAbortedError.mockImplementation((error: unknown) => error === aborted);
    mocks.api.mockRejectedValue(aborted);
    renderBell();
    await waitFor(() => expect(mocks.api).toHaveBeenCalledWith("/api/v1/notifications"));
    fireEvent.click(screen.getByRole("button", { name: "phase106.unreadCount" }));
    expect(screen.queryByText(/phase106.loadFailed/)).not.toBeInTheDocument();
  });
});
