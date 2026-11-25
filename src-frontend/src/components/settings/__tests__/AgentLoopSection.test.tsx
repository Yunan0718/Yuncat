import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";

// Mock useToast
const mockToast = vi.fn();
vi.mock("../../../hooks/use-toast", () => ({
  useToast: () => ({ toast: mockToast }),
}));

// Mock useApi
const mockApi = vi.fn();
vi.mock("../../../hooks/useApi", () => ({
  useApi: () => ({ api: mockApi }),
}));

import { AgentLoopSection } from "../AgentLoopSection";

describe("AgentLoopSection", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // 默认 GET /api/v1/settings 返回空对象
    mockApi.mockResolvedValue({});
  });

  it("挂载时加载 max_tool_rounds 并显示当前值", async () => {
    mockApi.mockResolvedValue({ max_tool_rounds: "250" });
    render(<AgentLoopSection />);

    await waitFor(() => {
      expect(mockApi).toHaveBeenCalledWith("/api/v1/settings");
    });

    const numberInput = screen.getByRole("spinbutton") as HTMLInputElement;
    expect(numberInput.value).toBe("250");
  });

  it("未配置时显示默认值 100", async () => {
    render(<AgentLoopSection />);
    await waitFor(() => {
      expect(screen.getByRole("spinbutton")).toHaveValue(100);
    });
  });

  it("输入低于下限时自动钳制到 10", async () => {
    render(<AgentLoopSection />);
    const numberInput = await screen.findByRole("spinbutton");

    fireEvent.change(numberInput, { target: { value: "5" } });

    await waitFor(() => {
      expect(numberInput).toHaveValue(10);
    });
  });

  it("输入超过上限时自动钳制到 1000", async () => {
    render(<AgentLoopSection />);
    const numberInput = await screen.findByRole("spinbutton");

    fireEvent.change(numberInput, { target: { value: "5000" } });

    await waitFor(() => {
      expect(numberInput).toHaveValue(1000);
    });
  });

  it("点击保存时 POST 钳制后的值并提示成功", async () => {
    render(<AgentLoopSection />);
    const numberInput = await screen.findByRole("spinbutton");
    fireEvent.change(numberInput, { target: { value: "200" } });

    const saveButton = screen.getByText("common.save");
    fireEvent.click(saveButton);

    await waitFor(() => {
      expect(mockApi).toHaveBeenLastCalledWith("/api/v1/settings", {
        method: "POST",
        body: JSON.stringify({ key: "max_tool_rounds", value: "200" }),
      });
    });
    await waitFor(() => {
      expect(mockToast).toHaveBeenCalledWith("common.saved", "success");
    });
  });

  it("保存失败时提示失败", async () => {
    mockApi.mockImplementation(async (_path, opts) => {
      if (opts && typeof opts === "object" && opts.method === "POST") {
        throw new Error("network error");
      }
      return {};
    });
    render(<AgentLoopSection />);
    await screen.findByRole("spinbutton");

    fireEvent.click(screen.getByText("common.save"));

    await waitFor(() => {
      expect(mockToast).toHaveBeenCalledWith("common.saveFailed", "error");
    });
  });
});
