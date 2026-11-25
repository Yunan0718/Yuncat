import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { AgentManager } from "../AgentManager";
import { useAppStore } from "../../store/appStore";

const mocks = vi.hoisted(() => ({
  api: vi.fn(),
  isAbortedError: vi.fn(),
}));

vi.mock("../../hooks/useApi", () => ({
  useApi: () => ({ api: mocks.api }),
  isAbortedError: mocks.isAbortedError,
}));

const AGENT = {
  id: "yuncat",
  name: "Yuncat",
  status: "stopped",
  detected: true,
  enabled: true,
  capabilities: { supports_stream: true, supports_tools: true },
};

beforeEach(() => {
  mocks.api.mockReset();
  mocks.isAbortedError.mockReset();
  mocks.isAbortedError.mockReturnValue(false);
  useAppStore.setState({ backendReady: true });
});

describe("AgentManager", () => {
  it("loads agents and clears the initial loading state", async () => {
    mocks.api.mockImplementation((url: string) => {
      if (url === "/api/v1/agents") return Promise.resolve([AGENT]);
      if (url === "/api/v1/monitoring") return Promise.resolve({ agents: {} });
      if (url.endsWith("/tools")) return Promise.resolve({ tools: [{ name: "read_file" }] });
      return Promise.reject(new Error(`unexpected ${url}`));
    });
    render(<AgentManager />);
    expect(screen.getByRole("status")).toHaveTextContent("agents.loading");
    await waitFor(() => expect(screen.getByText("Yuncat")).toBeInTheDocument());
    expect(screen.getByText("agents.count")).toBeInTheDocument();
    expect(screen.queryByText(/agents.loadFailed/)).not.toBeInTheDocument();
  });

  it("does not surface an aborted request as a page error", async () => {
    const aborted = Object.assign(new Error("Request was aborted"), { aborted: true });
    mocks.isAbortedError.mockImplementation((error: unknown) => error === aborted);
    mocks.api.mockImplementation((url: string) => {
      if (url === "/api/v1/agents") return Promise.reject(aborted);
      if (url === "/api/v1/monitoring") return Promise.resolve({ agents: {} });
      return Promise.reject(new Error("unexpected"));
    });
    render(<AgentManager />);
    await waitFor(() => expect(screen.queryByText("agents.loading")).not.toBeInTheDocument());
    expect(screen.queryByText(/agents.loadFailed/)).not.toBeInTheDocument();
  });

  it("shows the real load error instead of an empty-success state", async () => {
    mocks.api.mockImplementation((url: string) => {
      if (url === "/api/v1/agents") return Promise.reject(new Error("backend unavailable"));
      if (url === "/api/v1/monitoring") return Promise.resolve({ agents: {} });
      return Promise.reject(new Error("unexpected"));
    });
    render(<AgentManager />);
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("agents.loadFailed"));
    expect(screen.queryByText("agents.noAgents")).not.toBeInTheDocument();
  });
});
