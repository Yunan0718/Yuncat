import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

const { mockApi, mockStore } = vi.hoisted(() => ({
  mockApi: vi.fn(),
  mockStore: { backendReady: true },
}));

vi.mock("../../hooks/useApi", () => ({
  useApi: () => ({ api: mockApi }),
}));

vi.mock("../../store/appStore", () => ({
  useAppStore: (selector: (s: typeof mockStore) => unknown) => selector(mockStore),
}));

vi.mock("../../hooks/useConfirmDialog", () => ({
  useConfirmDialog: () => ({ confirm: vi.fn(), confirmDialog: null }),
}));

import { McpManager } from "../McpManager";

const SERVERS = [
  {
    id: "srv-1",
    name: "filesystem",
    command: "npx",
    args: [],
    env: {},
    enabled: true,
    created_at: 1,
    updated_at: 1,
  },
];

const TOOLS = {
  ok: true,
  tools: [
    {
      name: "read_file",
      description: "读取文件",
      input_schema: {
        type: "object",
        properties: { path: { type: "string" }, limit: { type: "integer" } },
        required: ["path"],
      },
    },
  ],
};

describe("McpManager 工具列表", () => {
  beforeEach(() => {
    mockApi.mockReset();
    mockStore.backendReady = true;
  });

  it("列出服务器提供的工具和参数", async () => {
    mockApi.mockImplementation((url: string) => {
      if (url === "/api/v1/mcp/servers") return Promise.resolve(SERVERS);
      if (url.includes("/tools")) return Promise.resolve(TOOLS);
      return Promise.resolve([]);
    });

    render(<McpManager />);
    await waitFor(() => expect(screen.getByText("filesystem")).toBeInTheDocument());

    fireEvent.click(screen.getByText("mcp.viewTools"));

    await waitFor(() => expect(screen.getByText("read_file")).toBeInTheDocument());
    expect(screen.getByText("读取文件")).toBeInTheDocument();
    // 参数来自后端返回的 JSON Schema
    expect(screen.getByText("path")).toBeInTheDocument();
    expect(screen.getByText("limit")).toBeInTheDocument();
  });

  it("取工具失败时显示失败原因，而不是伪装成「没有工具」", async () => {
    mockApi.mockImplementation((url: string) => {
      if (url === "/api/v1/mcp/servers") return Promise.resolve(SERVERS);
      if (url.includes("/tools")) {
        return Promise.reject(new Error("MCP 服务器通信失败：tools/list failed: timeout"));
      }
      return Promise.resolve([]);
    });

    render(<McpManager />);
    await waitFor(() => expect(screen.getByText("filesystem")).toBeInTheDocument());

    fireEvent.click(screen.getByText("mcp.viewTools"));

    await waitFor(() => expect(screen.getByText(/mcp\.toolsFailed/)).toBeInTheDocument());
    expect(screen.getByText(/timeout/)).toBeInTheDocument();
    expect(screen.queryByText("mcp.noTools")).not.toBeInTheDocument();
  });

  it("禁用的服务器不提供「查看工具」入口", async () => {
    mockApi.mockResolvedValueOnce([{ ...SERVERS[0], enabled: false }]);
    render(<McpManager />);
    await waitFor(() => expect(screen.getByText("filesystem")).toBeInTheDocument());
    expect(screen.getByText("mcp.viewTools").closest("button")).toBeDisabled();
  });
});
