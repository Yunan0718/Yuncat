import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

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

import { Dashboard } from "../Dashboard";

const TOKEN_DAYS_KEY = "yuncat.dashboard.tokenDays";
const CHARTS_OPEN_KEY = "yuncat.dashboard.chartsOpen";

describe("Dashboard", () => {
  beforeEach(() => {
    mockApi.mockReset();
    mockStore.backendReady = true;
    localStorage.removeItem(TOKEN_DAYS_KEY);
    localStorage.removeItem(CHARTS_OPEN_KEY);
  });

  function renderDashboard() {
    mockApi.mockResolvedValue({});
    return render(
      <MemoryRouter>
        <Dashboard />
      </MemoryRouter>,
    );
  }

  it("loads analytics with default 7-day token usage", async () => {
    renderDashboard();

    await waitFor(() => {
      expect(mockApi).toHaveBeenCalledWith("/api/v1/analytics/summary");
    });
    await waitFor(() => {
      expect(mockApi).toHaveBeenCalledWith("/api/v1/analytics/token-usage?days=7");
    });

    expect(screen.getByText("7d")).toBeInTheDocument();
    expect(localStorage.getItem(TOKEN_DAYS_KEY)).toBe("7");
  });

  it("restores tokenDays from localStorage", async () => {
    localStorage.setItem(TOKEN_DAYS_KEY, "30");

    renderDashboard();

    await waitFor(() => {
      expect(mockApi).toHaveBeenCalledWith("/api/v1/analytics/token-usage?days=30");
    });
    expect(screen.getByText("30d")).toBeInTheDocument();
  });

  it("persists tokenDays when switching to 30 days", async () => {
    renderDashboard();
    await waitFor(() => {
      expect(mockApi).toHaveBeenCalledWith("/api/v1/analytics/token-usage?days=7");
    });

    fireEvent.click(screen.getByText("30d"));

    await waitFor(() => {
      expect(localStorage.getItem(TOKEN_DAYS_KEY)).toBe("30");
    });
    await waitFor(() => {
      expect(mockApi).toHaveBeenCalledWith("/api/v1/analytics/token-usage?days=30");
    });
  });

  function getCollapsibleTrigger() {
    return document.querySelector('[data-slot="collapsible-trigger"]') as HTMLElement | null;
  }

  it("restores chartsOpen=false from localStorage", async () => {
    localStorage.setItem(CHARTS_OPEN_KEY, "false");

    renderDashboard();
    await waitFor(() => {
      expect(mockApi).toHaveBeenCalledWith("/api/v1/analytics/summary");
    });

    const trigger = getCollapsibleTrigger();
    expect(trigger).toHaveAttribute("aria-expanded", "false");
    expect(localStorage.getItem(CHARTS_OPEN_KEY)).toBe("false");
  });

  it("toggles chartsOpen and persists to localStorage", async () => {
    localStorage.setItem(CHARTS_OPEN_KEY, "false");

    renderDashboard();
    await waitFor(() => {
      expect(mockApi).toHaveBeenCalledWith("/api/v1/analytics/summary");
    });

    const trigger = getCollapsibleTrigger();
    expect(trigger).toHaveAttribute("aria-expanded", "false");

    fireEvent.click(trigger!);

    await waitFor(() => {
      expect(getCollapsibleTrigger()).toHaveAttribute("aria-expanded", "true");
    });
    expect(localStorage.getItem(CHARTS_OPEN_KEY)).toBe("true");
  });

  it("统计接口失败时显式提示并可重试", async () => {
    mockApi.mockImplementation(async (url: string) => {
      if (String(url).includes("/analytics/token-usage")) throw new Error("boom");
      return {};
    });

    render(
      <MemoryRouter>
        <Dashboard />
      </MemoryRouter>,
    );

    // 2026-10-22：失败必须显式提示——否则界面上的 0 与"真的没数据"无法区分
    await waitFor(() => {
      expect(screen.getByText("dashboard.statsLoadFailed")).toBeInTheDocument();
    });
    const before = mockApi.mock.calls.length;
    fireEvent.click(screen.getByText("common.retry"));
    await waitFor(() => {
      expect(mockApi.mock.calls.length).toBeGreaterThan(before);
    });
  });

  it("显示缓存命中率（后端账本口径）", async () => {
    mockApi.mockImplementation(async (url: string) => {
      if (String(url).includes("/analytics/cache-stats")) {
        return { hit_rate_pct: 75, total_cache_tokens: 1234 };
      }
      return {};
    });

    render(
      <MemoryRouter>
        <Dashboard />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(screen.getByText("75%")).toBeInTheDocument();
    });
  });
});
