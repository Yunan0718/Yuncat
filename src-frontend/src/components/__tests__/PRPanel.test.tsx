import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { PRPanel } from "../vcs/PRPanel";

const mockApi = vi.fn();

vi.mock("../../hooks/useApi", () => ({
  useApi: () => ({ api: mockApi }),
}));

const defaultProps = {
  open: true,
  defaultOwner: "Yuncat",
  defaultRepo: "Yuncat",
  defaultHead: "feature",
  defaultBase: "main",
  defaultProvider: "github",
  onClose: vi.fn(),
  onStatusMsg: vi.fn(),
};

describe("PRPanel", () => {
  beforeEach(() => {
    mockApi.mockReset();
  });

  it("打开时加载 providers 与 PR 列表", async () => {
    mockApi
      .mockResolvedValueOnce({ ok: true, providers: [{ provider: "github", token_mask: "***abcd" }] })
      .mockResolvedValueOnce({ ok: true, data: [{ number: 1, title: "feat", state: "open", user: { login: "alice" } }] });

    render(<PRPanel {...defaultProps} />);
    await waitFor(() => {
      expect(screen.getByText("feat")).toBeInTheDocument();
    });
    expect(screen.getByText("#1")).toBeInTheDocument();
  });

  it("空列表时显示占位文案", async () => {
    mockApi
      .mockResolvedValueOnce({ ok: true, providers: [{ provider: "github", token_mask: "***abcd" }] })
      .mockResolvedValueOnce({ ok: true, data: [] });

    render(<PRPanel {...defaultProps} />);
    await waitFor(() => {
      expect(screen.getByText("vcs.empty")).toBeInTheDocument();
    });
  });

  it("点击创建按钮弹出表单", async () => {
    mockApi
      .mockResolvedValueOnce({ ok: true, providers: [{ provider: "github", token_mask: "***abcd" }] })
      .mockResolvedValueOnce({ ok: true, data: [] });

    render(<PRPanel {...defaultProps} />);
    await waitFor(() => {
      expect(screen.getByText("vcs.createPR")).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText("vcs.createPR"));
    expect(screen.getAllByText("vcs.createPR").length).toBeGreaterThanOrEqual(2);
  });

  it("提交创建表单调用 API", async () => {
    mockApi
      .mockResolvedValueOnce({ ok: true, providers: [{ provider: "github", token_mask: "***abcd" }] })
      .mockResolvedValueOnce({ ok: true, data: [] })
      .mockResolvedValueOnce({ ok: true, data: { number: 2 } });

    render(<PRPanel {...defaultProps} />);
    await waitFor(() => {
      expect(screen.getByText("vcs.createPR")).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText("vcs.createPR"));

    const titleInput = screen.getAllByRole("textbox")[0];
    fireEvent.change(titleInput, { target: { value: "new feature" } });

    const createButtons = screen.getAllByText("vcs.createPR");
    fireEvent.click(createButtons[createButtons.length - 1]);

    await waitFor(() => {
      expect(mockApi).toHaveBeenLastCalledWith(
        "/api/v1/vcs/prs",
        expect.objectContaining({ method: "POST" }),
      );
    });
  });
});
