import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

const mockToast = vi.fn();
vi.mock("../../../hooks/use-toast", () => ({
  useToast: () => ({ toast: mockToast }),
}));

const mockApi = vi.fn();
vi.mock("../../../hooks/useApi", () => ({
  useApi: () => ({ api: mockApi }),
}));

// base-ui Switch 依赖 PointerEvent，jsdom 不支持——复用 AutostartCard 的 mock 模式
vi.mock("../../ui/switch", () => ({
  Switch: ({
    checked,
    onCheckedChange,
    disabled,
    ...props
  }: {
    checked: boolean;
    onCheckedChange?: (v: boolean) => void;
    disabled?: boolean;
  }) => (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      data-checked={checked ? "" : undefined}
      disabled={disabled}
      onClick={() => onCheckedChange?.(!checked)}
      {...props}
    >
      switch
    </button>
  ),
}));

import { OCGatewaySection } from "../OCGatewaySection";

// Task 6.1: Gateway 启停开关测试
describe("OCGatewaySection", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockApi.mockReset().mockResolvedValue({});
    mockToast.mockReset();
  });

  it("加载中显示 checking 状态", async () => {
    mockApi.mockImplementation(() => new Promise(() => {}));
    render(<OCGatewaySection />);
    expect(screen.getByText("settings.checking")).toBeInTheDocument();
  });

  it("运行状态时 Switch 为 checked 且工具 checkbox 可用", async () => {
    mockApi.mockResolvedValue({ status: "running", port: 18789, version: "1.0" });
    render(<OCGatewaySection />);
    await waitFor(() => {
      expect(screen.getByRole("switch")).toBeChecked();
    });
    expect(screen.getByText("settings.gatewayRunning")).toBeInTheDocument();
    const firstToolCheckbox = screen.getAllByRole("checkbox")[0];
    expect(firstToolCheckbox).not.toBeDisabled();
  });

  it("停止状态时 Switch 为 unchecked 且工具 checkbox 禁用", async () => {
    mockApi.mockResolvedValue({ status: "stopped" });
    render(<OCGatewaySection />);
    await waitFor(() => {
      expect(screen.getByRole("switch")).not.toBeChecked();
    });
    expect(screen.getByText("settings.gatewayStopped")).toBeInTheDocument();
    const toolCheckboxes = screen.getAllByRole("checkbox");
    toolCheckboxes.forEach((cb) => expect(cb).toBeDisabled());
  });

  it("点击 Switch 从 stopped 切换到 running 调用 start 并刷新状态", async () => {
    // 使用状态化 mockImplementation 替代 mockResolvedValueOnce 链——兼容 React Strict Mode 双 mount
    let started = false;
    mockApi.mockImplementation((path: string) => {
      if (path === "/api/v1/agents/openclaw/status") {
        return Promise.resolve(started ? { status: "running", port: 18789 } : { status: "stopped" });
      }
      if (path === "/api/v1/agents/openclaw/start") {
        started = true;
        return Promise.resolve({});
      }
      return Promise.resolve({});
    });

    render(<OCGatewaySection />);
    await waitFor(() => {
      expect(screen.getByRole("switch")).not.toBeChecked();
    });

    fireEvent.click(screen.getByRole("switch"));

    await waitFor(() => {
      expect(mockApi).toHaveBeenCalledWith("/api/v1/agents/openclaw/start", { method: "POST" });
    });
    await waitFor(() => {
      expect(screen.getByRole("switch")).toBeChecked();
    });
    expect(mockToast).toHaveBeenCalledWith("settings.gatewayStart", "success");
  });

  it("点击 Switch 从 running 切换到 stopped 调用 stop 并刷新状态", async () => {
    let stopped = false;
    mockApi.mockImplementation((path: string) => {
      if (path === "/api/v1/agents/openclaw/status") {
        return Promise.resolve(stopped ? { status: "stopped" } : { status: "running", port: 18789 });
      }
      if (path === "/api/v1/agents/openclaw/stop") {
        stopped = true;
        return Promise.resolve({});
      }
      return Promise.resolve({});
    });

    render(<OCGatewaySection />);
    await waitFor(() => {
      expect(screen.getByRole("switch")).toBeChecked();
    });

    fireEvent.click(screen.getByRole("switch"));

    await waitFor(() => {
      expect(mockApi).toHaveBeenCalledWith("/api/v1/agents/openclaw/stop", { method: "POST" });
    });
    await waitFor(() => {
      expect(screen.getByRole("switch")).not.toBeChecked();
    });
    expect(mockToast).toHaveBeenCalledWith("settings.gatewayStop", "success");
  });

  it("start 失败时回滚 Switch 状态并 toast 错误", async () => {
    mockApi.mockImplementation((path: string) => {
      if (path === "/api/v1/agents/openclaw/status") return Promise.resolve({ status: "stopped" });
      if (path === "/api/v1/agents/openclaw/start") return Promise.reject(new Error("start failed"));
      return Promise.resolve({});
    });

    render(<OCGatewaySection />);
    await waitFor(() => {
      expect(screen.getByRole("switch")).not.toBeChecked();
    });

    fireEvent.click(screen.getByRole("switch"));

    await waitFor(() => {
      expect(mockApi).toHaveBeenCalledWith("/api/v1/agents/openclaw/start", { method: "POST" });
    });
    await waitFor(() => {
      expect(screen.getByRole("switch")).not.toBeChecked();
    });
    expect(mockToast).toHaveBeenCalledWith("settings.gatewayStartFailed", "error");
  });

  it("工具开关保持原有 toggleTool 行为", async () => {
    mockApi.mockResolvedValue({ status: "running", port: 18789 });
    render(<OCGatewaySection />);
    await waitFor(() => {
      expect(screen.getByRole("switch")).toBeChecked();
    });

    const webSearchCheckbox = screen.getByRole("checkbox", { name: /web_search/i });
    fireEvent.click(webSearchCheckbox);

    await waitFor(() => {
      expect(mockApi).toHaveBeenCalledWith(
        "/api/v1/settings",
        expect.objectContaining({
          method: "POST",
          body: expect.stringContaining("oc_tools_disabled"),
        }),
      );
    });
  });
});
