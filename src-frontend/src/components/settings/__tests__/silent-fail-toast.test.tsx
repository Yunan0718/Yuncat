import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";

// Task 6.9: 静默失败加 toast 反馈——契约测试
// 覆盖 5 处静默失败：SecuritySection.toggleRule/deleteRule、WorkspaceSection 保存、
// PendingRolesSection.promote/reject

// Mock useToast——捕获 toast 调用以断言失败反馈
const mockToast = vi.fn();
vi.mock("../../../hooks/use-toast", () => ({
  useToast: () => ({ toast: mockToast }),
}));

// Mock useApi 顶层声明（SecuritySection/WorkspaceSection/PendingRolesSection
// 都接收 api 作为 prop，所以不 mock useApi hook——但 ToolPolicyPanel 内部用 useApi，
// 已通过 mock ToolPolicyPanel 规避）
const mockApi = vi.fn();

// Mock ToolPolicyPanel 避免 useApi 内部 hook 依赖（SecuritySection 渲染它）
vi.mock("../../ToolPolicyPanel", () => ({
  ToolPolicyPanel: () => <div data-testid="tool-policy" />,
}));

// Mock Dialog 避免 base-ui Portal 复杂依赖（SecuritySection 用 Dialog，默认 closed）
vi.mock("../../ui/dialog", () => ({
  Dialog: ({ children, open }: { children: React.ReactNode; open: boolean }) =>
    open ? <div>{children}</div> : null,
  DialogContent: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  DialogHeader: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  DialogTitle: ({ children }: { children: React.ReactNode }) => <h2>{children}</h2>,
  DialogFooter: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
}));

import { SecuritySection } from "../SecuritySection";
import { WorkspaceSection } from "../WorkspaceSection";
import { PendingRolesSection } from "../PendingRolesSection";

// stableT：模拟 setup.ts 的 useTranslation mock（t(key) 返回 key 字符串）
// PendingRolesSection 接收 t 作为 prop
const stableT = (key: string) => String(key);

describe("Task 6.9: 静默失败加 toast 反馈", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  // 1. SecuritySection.toggleRule——原代码无 try/catch
  it("toggleRule API 失败时 toast settings.ruleToggleFailed", async () => {
    mockApi.mockImplementation(async (path: string, opts?: { method?: string }) => {
      if (path === "/api/v1/settings") return {};
      if (path === "/api/v1/rules")
        return [
          { id: "r1", description: "rule-desc", trigger_on: [], action: "block", enabled: true },
        ];
      // PendingRolesSection 子组件拉取——返回空数组避免 .map 崩溃
      if (path === "/api/v1/roles/pending") return [];
      if (path === "/api/v1/rules/r1" && opts?.method === "PATCH")
        throw new Error("network fail");
      return {};
    });
    render(<SecuritySection api={mockApi} />);
    await waitFor(() => expect(screen.getByText("rule-desc")).toBeInTheDocument());
    // toggle 按钮——当前 enabled=true，按钮文案为 t("common.enabled") = "common.enabled"
    fireEvent.click(screen.getByText("common.enabled"));
    await waitFor(() =>
      expect(mockToast).toHaveBeenCalledWith("settings.ruleToggleFailed", "error"),
    );
  });

  // 2. SecuritySection.deleteRule——原代码无 try/catch
  it("deleteRule API 失败时 toast settings.ruleDeleteFailed", async () => {
    mockApi.mockImplementation(async (path: string, opts?: { method?: string }) => {
      if (path === "/api/v1/settings") return {};
      if (path === "/api/v1/rules")
        return [
          { id: "r1", description: "rule-desc", trigger_on: [], action: "block", enabled: true },
        ];
      // PendingRolesSection 子组件拉取——返回空数组避免 .map 崩溃
      if (path === "/api/v1/roles/pending") return [];
      if (path === "/api/v1/rules/r1" && opts?.method === "DELETE")
        throw new Error("network fail");
      return {};
    });
    render(<SecuritySection api={mockApi} />);
    await waitFor(() => expect(screen.getByText("rule-desc")).toBeInTheDocument());
    fireEvent.click(screen.getByText("common.delete"));
    await waitFor(() =>
      expect(mockToast).toHaveBeenCalledWith("settings.ruleDeleteFailed", "error"),
    );
  });

  // 3. WorkspaceSection 保存——原代码无 try/catch
  it("WorkspaceSection 保存 API 失败时 toast settings.saveFailed", async () => {
    mockApi.mockImplementation(async (path: string, opts?: { method?: string }) => {
      if (path === "/api/v1/settings" && opts?.method === "POST")
        throw new Error("network fail");
      if (path === "/api/v1/settings") return { workspace: "/tmp" };
      return {};
    });
    render(<WorkspaceSection api={mockApi} />);
    await waitFor(() => expect(screen.getByDisplayValue("/tmp")).toBeInTheDocument());
    fireEvent.click(screen.getByText("settings.savePath"));
    await waitFor(() =>
      expect(mockToast).toHaveBeenCalledWith("settings.saveFailed", "error"),
    );
  });

  // 4. PendingRolesSection.promote——原代码 catch { // no-op }
  it("promote API 失败时 toast settings.rolePromoteFailed", async () => {
    mockApi.mockImplementation(async (path: string, opts?: { method?: string }) => {
      if (path === "/api/v1/roles/pending")
        return [{ node_id: "n1", role_name: "role-a", score: 0.9 }];
      if (path === "/api/v1/roles/promote" && opts?.method === "POST")
        throw new Error("network fail");
      return {};
    });
    render(<PendingRolesSection api={mockApi} t={stableT} />);
    await waitFor(() => expect(screen.getByText("role-a")).toBeInTheDocument());
    fireEvent.click(screen.getByText("settings.promote"));
    await waitFor(() =>
      expect(mockToast).toHaveBeenCalledWith("settings.rolePromoteFailed", "error"),
    );
  });

  // 5. PendingRolesSection.reject——原代码 catch { // no-op }
  it("reject API 失败时 toast settings.roleRejectFailed", async () => {
    mockApi.mockImplementation(async (path: string, opts?: { method?: string }) => {
      if (path === "/api/v1/roles/pending")
        return [{ node_id: "n1", role_name: "role-a", score: 0.9 }];
      if (path === "/api/v1/roles/reject" && opts?.method === "POST")
        throw new Error("network fail");
      return {};
    });
    render(<PendingRolesSection api={mockApi} t={stableT} />);
    await waitFor(() => expect(screen.getByText("role-a")).toBeInTheDocument());
    fireEvent.click(screen.getByText("settings.dismiss"));
    await waitFor(() =>
      expect(mockToast).toHaveBeenCalledWith("settings.roleRejectFailed", "error"),
    );
  });
});
