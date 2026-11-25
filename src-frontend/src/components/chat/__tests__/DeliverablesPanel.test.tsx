import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useAppStore } from "../../../store/appStore";
import { useTaskStore } from "../../../store/taskStore";

const apiMock = vi.fn();

vi.mock("../../../hooks/useApi", () => ({ useApi: () => ({ api: apiMock }) }));

import { DeliverablesPanel } from "../DeliverablesPanel";

describe("DeliverablesPanel", () => {
  beforeEach(() => {
    apiMock.mockReset();
    useTaskStore.setState({ currentSessionId: "s1", deliverablesVersion: 0 });
    useAppStore.setState({ pendingWorkspaceFile: null });
  });

  it("没有交付物时不渲染入口", async () => {
    apiMock.mockResolvedValue({ presented: [], changed: [] });
    const { container } = render(<DeliverablesPanel />);
    await waitFor(() => expect(apiMock).toHaveBeenCalled());
    expect(container.firstChild).toBeNull();
  });

  it("列出交付文件并可点击打开", async () => {
    apiMock.mockResolvedValue({
      presented: [{ rel_path: "out.txt", file_path: "C:/ws/out.txt", description: "产物" }],
      changed: [{ path: "a.py" }],
    });
    render(<DeliverablesPanel />);
    await waitFor(() => expect(screen.getByText("2")).toBeInTheDocument());
    fireEvent.click(screen.getByLabelText("deliverables.title"));
    expect(screen.getByText("out.txt")).toBeInTheDocument();
    expect(screen.getByText("a.py")).toBeInTheDocument();
    fireEvent.click(screen.getByText("out.txt"));
    expect(useAppStore.getState().pendingWorkspaceFile).toBe("out.txt");
  });

  it("显示快照口径的增删行数与二进制标记", async () => {
    apiMock.mockResolvedValue({
      presented: [],
      changed: [],
      changes: [
        { path: "shared/a.py", display: "shared/a.py", added: 3, deleted: 1 },
        { path: "shared/bin.dat", display: "shared/bin.dat", binary: true },
      ],
    });
    render(<DeliverablesPanel />);
    await waitFor(() => expect(screen.getByText("2")).toBeInTheDocument());
    fireEvent.click(screen.getByLabelText("deliverables.title"));
    expect(screen.getByText("+3")).toBeInTheDocument();
    expect(screen.getByText("-1")).toBeInTheDocument();
    expect(screen.getByText("deliverables.binary")).toBeInTheDocument();
  });

  it("列出 shell 命令改动的文件并标注来源", async () => {
    // shell 改的文件工具层看不见——必须单独出现在交付物清单里
    apiMock.mockResolvedValue({
      presented: [],
      changed: [],
      changes: [],
      shell: [
        {
          rel_path: "node_modules/pkg/package.json",
          file_path: "C:/ws/node_modules/pkg/package.json",
          source: "shell",
        },
      ],
    });
    render(<DeliverablesPanel />);
    await waitFor(() => expect(screen.getByText("1")).toBeInTheDocument());
    fireEvent.click(screen.getByLabelText("deliverables.title"));
    expect(screen.getByText("node_modules/pkg/package.json")).toBeInTheDocument();
    expect(screen.getByText("deliverables.shellTag")).toBeInTheDocument();
    // shell 改动没有行数统计，但仍可看 diff（旧侧取 git HEAD）
    expect(screen.getByLabelText("deliverables.viewDiff")).toBeInTheDocument();
  });

  it("点击查看改动会拉取并渲染 diff", async () => {
    apiMock.mockImplementation(async (url: string) => {
      if (url.includes("/deliverables/diff")) {
        return {
          path: "shared/a.py",
          display: "shared/a.py",
          kind: "text",
          diff: "@@ -1 +1 @@\n-old\n+new\n",
          added: 1,
          deleted: 1,
        };
      }
      return {
        presented: [],
        changed: [],
        changes: [{
          path: "C:/ws/shared/a.py",
          rel_path: "shared/a.py",
          display: "shared/a.py",
          added: 1,
          deleted: 1,
        }],
      };
    });
    render(<DeliverablesPanel />);
    await waitFor(() => expect(screen.getByText("1")).toBeInTheDocument());
    fireEvent.click(screen.getByLabelText("deliverables.title"));
    fireEvent.click(screen.getByLabelText("deliverables.viewDiff"));
    await waitFor(() => expect(screen.getByText("old")).toBeInTheDocument());
    expect(screen.getByText("new")).toBeInTheDocument();
    // diff 用绝对路径定位，打开文件用相对路径（工作区按相对路径匹配）
    expect(
      apiMock.mock.calls.some(([u]) => String(u).includes(encodeURIComponent("C:/ws/shared/a.py"))),
    ).toBe(true);
  });
});
