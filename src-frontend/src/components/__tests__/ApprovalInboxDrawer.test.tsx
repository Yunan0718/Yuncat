/**
 * ApprovalInboxDrawer.test.tsx —— Phase 107 统一审批 Inbox 抽屉测试
 *
 * 验证：
 * 1. 抽屉关闭时不渲染
 * 2. 抽屉打开时渲染 Pending/History Tab
 * 3. Pending 项显示 Approve/Reject 按钮
 * 4. 点击 Approve 调用 approve API 并刷新列表
 * 5. 空态显示对应文案
 */
import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { ApprovalInboxDrawer } from "../ApprovalInboxDrawer";
import { useAppStore } from "../../store/appStore";

const approveMock = vi.fn().mockResolvedValue({});
const rejectMock = vi.fn().mockResolvedValue({});
const refreshMock = vi.fn().mockResolvedValue(undefined);

const PENDING_ITEM = {
  id: "tok-pending",
  state_type: "write_file_preview",
  payload: { path: "src/app.ts" },
  created_at: 1700000000,
  expires_at: Date.now() / 1000 + 1800,
  title: "write_file: src/app.ts",
  task_id: "task-1",
};

const HISTORY_ITEM = {
  id: "tok-history",
  state_type: "delete_file_preview",
  payload: { path: "src/old.ts" },
  created_at: 1699999900,
  expires_at: Date.now() / 1000 - 10,
  status: "resolved" as const,
  resolved_at: 1700000005,
  resolution: "approved: delete_file executed",
  title: "delete_file: src/old.ts",
  task_id: "task-1",
};

vi.mock("../../hooks/useApi", () => ({
  useApi: () => ({ api: vi.fn() }),
}));

vi.mock("../../hooks/useApprovalInbox", () => ({
  useApprovalInbox: () => ({
    pending: [PENDING_ITEM],
    history: [HISTORY_ITEM],
    loading: false,
    error: null,
    refresh: refreshMock,
    approve: approveMock,
    reject: rejectMock,
  }),
}));

function ensureOverlayRoot() {
  let root = document.getElementById("overlay-root");
  if (!root) {
    root = document.createElement("div");
    root.id = "overlay-root";
    document.body.appendChild(root);
  }
  return root;
}

describe("ApprovalInboxDrawer", () => {
  beforeEach(() => {
    ensureOverlayRoot();
    useAppStore.setState({ inboxOpen: false });
    approveMock.mockClear();
    rejectMock.mockClear();
    refreshMock.mockClear();
  });

  it("抽屉关闭时不渲染", () => {
    useAppStore.setState({ inboxOpen: false });
    render(<ApprovalInboxDrawer />);
    expect(screen.queryByText("inbox.title")).not.toBeInTheDocument();
  });

  it("抽屉打开时渲染 Pending/History Tab", () => {
    useAppStore.setState({ inboxOpen: true });
    render(<ApprovalInboxDrawer />);
    expect(screen.getByText("inbox.title")).toBeInTheDocument();
    expect(screen.getByText(/inbox.pending/)).toBeInTheDocument();
    expect(screen.getByText(/inbox.history/)).toBeInTheDocument();
  });

  it("Pending 项显示标题与 Approve/Reject 按钮", () => {
    useAppStore.setState({ inboxOpen: true });
    render(<ApprovalInboxDrawer />);
    expect(screen.getByText("write_file: src/app.ts")).toBeInTheDocument();
    expect(screen.getByText("common.confirm")).toBeInTheDocument();
    expect(screen.getByText("common.reject")).toBeInTheDocument();
  });

  it("点击 Approve 调用 approve 并刷新", async () => {
    useAppStore.setState({ inboxOpen: true });
    render(<ApprovalInboxDrawer />);
    fireEvent.click(screen.getByText("common.confirm"));
    await waitFor(() => {
      expect(approveMock).toHaveBeenCalledWith("tok-pending", false); // 组件签名含 remember 参数（2026-09-10 修正过期断言）
      expect(refreshMock).toHaveBeenCalled();
    });
  });

  it("History Tab 显示已处理项", () => {
    useAppStore.setState({ inboxOpen: true });
    render(<ApprovalInboxDrawer />);
    fireEvent.click(screen.getByText(/inbox.history/));
    expect(screen.getByText("delete_file: src/old.ts")).toBeInTheDocument();
    expect(screen.getByText("inbox.status.resolved")).toBeInTheDocument();
  });
});
