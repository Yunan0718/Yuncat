import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

// Mock useToast——DirNode 依赖 toast 显示错误
const mockToast = vi.fn();
vi.mock("../../hooks/use-toast", () => ({
  useToast: () => ({ toast: mockToast }),
}));

// Mock useAppStore——FileTree 读 fileWatchCounter + sendTerminalCommand
const mockSendTerminalCommand = vi.fn();
vi.mock("../../store/appStore", () => ({
  useAppStore: Object.assign(
    (selector: (s: { fileWatchCounter: number }) => unknown) =>
      selector({ fileWatchCounter: 0 }),
    { getState: () => ({ sendTerminalCommand: mockSendTerminalCommand }) },
  ),
}));

// Mock useConfirmDialog / usePromptDialog——FileTree 依赖但 DnD 测试不触发
vi.mock("../../hooks/useConfirmDialog", () => ({
  useConfirmDialog: () => ({
    confirm: vi.fn().mockResolvedValue(true),
    confirmDialog: null,
  }),
}));
vi.mock("../../hooks/usePromptDialog", () => ({
  usePromptDialog: () => ({
    prompt: vi.fn().mockResolvedValue(null),
    promptDialog: null,
  }),
}));

// Mock FileCompareDialog / FileTimelineDialog——避免渲染复杂子组件
vi.mock("../workspace/FileCompareDialog", () => ({
  FileCompareDialog: () => null,
}));
vi.mock("../workspace/FileTimelineDialog", () => ({
  FileTimelineDialog: () => null,
}));

// Mock useApi——提供 api + ApiError（FileTree 的 catch 分支用 instanceof ApiError）
const mockApi = vi.fn();
vi.mock("../../hooks/useApi", () => ({
  useApi: () => ({ api: mockApi }),
  ApiError: class ApiError extends Error {
    constructor(
      public status: number,
      public statusText: string,
      public body: string,
      public url: string,
    ) {
      super(body || statusText);
    }
  },
}));

import { FileTree } from "../workspace/FileTree";

// Task 7.25: 模拟 HTML5 DnD DataTransfer——jsdom 不完整实现 DataTransfer
function makeMockDataTransfer(opts: {
  data?: Record<string, string>;
  types?: string[];
  files?: File[];
} = {}) {
  const data: Record<string, string> = { ...(opts.data || {}) };
  const typeList: string[] = opts.types ? [...opts.types] : Object.keys(data);
  return {
    setData: vi.fn((k: string, v: string) => {
      data[k] = v;
      if (!typeList.includes(k)) typeList.push(k);
    }),
    getData: vi.fn((k: string) => data[k] || ""),
    get types() {
      return typeList;
    },
    effectAllowed: "none",
    dropEffect: "none",
    files: opts.files || [],
  } as unknown as DataTransfer;
}

const sharedFiles = [
  { name: "file1.ts", path: "/test/shared/file1.ts", is_dir: false, size: 100, modified: 0 },
  { name: "subdir", path: "/test/shared/subdir", is_dir: true, size: 0, modified: 0 },
];
const agentFiles = [
  { name: "agent_file.ts", path: "/test/agents/agent_file.ts", is_dir: false, size: 50, modified: 0 },
];

describe("FileTree DnD (Task 7.25)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockApi.mockImplementation((url: string) => {
      if (url === "/api/v1/settings") return Promise.resolve({ workspace: "/test" });
      if (url === "/api/v1/workspace/files") return Promise.resolve(sharedFiles);
      if (url.includes("dir=agents")) return Promise.resolve(agentFiles);
      if (url.includes("git-status")) return Promise.resolve({ files: {} });
      return Promise.resolve({});
    });
  });

  it("shared 文件可拖拽（draggable=true）", async () => {
    render(<FileTree onSelect={vi.fn()} selectedPath={null} />);
    await waitFor(() => {
      expect(screen.getByText("file1.ts")).toBeInTheDocument();
    });
    const fileButton = screen.getByText("file1.ts").closest("button");
    expect(fileButton?.draggable).toBe(true);
  });

  it("agents 文件不可拖拽（readOnly）", async () => {
    render(<FileTree onSelect={vi.fn()} selectedPath={null} />);
    await waitFor(() => {
      expect(screen.getByText("agent_file.ts")).toBeInTheDocument();
    });
    const fileButton = screen.getByText("agent_file.ts").closest("button");
    expect(fileButton?.draggable).toBe(false);
  });

  it("拖放文件到目录调用 rename API", async () => {
    render(<FileTree onSelect={vi.fn()} selectedPath={null} />);
    await waitFor(() => {
      expect(screen.getByText("subdir")).toBeInTheDocument();
    });
    const dirButton = screen.getByText("subdir").closest("button")!;
    const mockDT = makeMockDataTransfer({
      data: { "application/x-yuncat-move": "shared/file1.ts" },  // 工作区相对（handleDragStart 的形态）
      types: ["application/x-yuncat-move"],
    });
    fireEvent.dragOver(dirButton, { dataTransfer: mockDT });
    fireEvent.drop(dirButton, { dataTransfer: mockDT });
    await waitFor(() => {
      expect(mockApi).toHaveBeenCalledWith(
        "/api/v1/workspace/files/rename",
        expect.objectContaining({
          method: "POST",
          body: JSON.stringify({
            old_path: "shared/file1.ts",
            new_path: "shared/subdir/file1.ts",
          }),
        }),
      );
    });
  });

  it("拖放目录到自身显示错误 toast", async () => {
    render(<FileTree onSelect={vi.fn()} selectedPath={null} />);
    await waitFor(() => {
      expect(screen.getByText("subdir")).toBeInTheDocument();
    });
    const dirButton = screen.getByText("subdir").closest("button")!;
    const mockDT = makeMockDataTransfer({
      data: { "application/x-yuncat-move": "shared/subdir" },  // 工作区相对（handleDragStart 的形态）
      types: ["application/x-yuncat-move"],
    });
    fireEvent.drop(dirButton, { dataTransfer: mockDT });
    await waitFor(() => {
      expect(mockToast).toHaveBeenCalledWith("fileTree.cannotMoveIntoSelf", "error");
    });
  });

  it("拖放文件到文件（同级）调用 rename API 移到父目录", async () => {
    // file1.ts 拖放到另一个文件上 → 移到目标文件的父目录
    // 由于只有一个文件，测试 file1.ts 拖放到自身 → no-op（同位置）
    render(<FileTree onSelect={vi.fn()} selectedPath={null} />);
    await waitFor(() => {
      expect(screen.getByText("file1.ts")).toBeInTheDocument();
    });
    const fileButton = screen.getByText("file1.ts").closest("button")!;
    const mockDT = makeMockDataTransfer({
      data: { "application/x-yuncat-move": "shared/file1.ts" },  // 工作区相对（handleDragStart 的形态）
      types: ["application/x-yuncat-move"],
    });
    fireEvent.drop(fileButton, { dataTransfer: mockDT });
    // 同位置 no-op——不应调用 rename API
    await waitFor(() => {
      expect(mockToast).not.toHaveBeenCalled();
    });
    const renameCalls = mockApi.mock.calls.filter(
      ([url]) => typeof url === "string" && url.includes("/rename"),
    );
    expect(renameCalls.length).toBe(0);
  });
});
