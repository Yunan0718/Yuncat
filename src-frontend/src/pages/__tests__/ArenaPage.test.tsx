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

vi.mock("../../components/ResultRenderer", () => ({
  ResultRenderer: ({ content }: { content?: string }) => (
    <div data-testid="result-renderer">{content}</div>
  ),
}));

vi.mock("../../components/common/LoadingCurve", () => ({
  LoadingCurve: () => <div data-testid="loading-curve">loading</div>,
}));

import { ArenaPage } from "../ArenaPage";

const MODELS = [
  { provider: "deepseek", model: "deepseek-chat", display_name: "DeepSeek-V3" },
  { provider: "openai", model: "gpt-4o", display_name: "GPT-4o" },
];

const BATTLE = {
  battle_id: "battle-1",
  status: "ready",
  response_a: "response A",
  response_b: "response B",
  created_at: 1700000000,
};

const REVEALED = {
  model_a: { provider: "deepseek", model: "deepseek-chat", display_name: "DeepSeek-V3" },
  model_b: { provider: "openai", model: "gpt-4o", display_name: "GPT-4o" },
};

const LEADERBOARD = {
  entries: [
    { provider: "deepseek", model: "deepseek-chat", battles: 5, wins: 3, win_rate: 0.6 },
  ],
};

describe("ArenaPage", () => {
  beforeEach(() => {
    mockApi.mockReset();
    mockStore.backendReady = true;
  });

  it("shows startup placeholder when backend is not ready", () => {
    mockStore.backendReady = false;
    render(<ArenaPage />);
    expect(screen.getByText("chat.startup")).toBeInTheDocument();
  });

  it("loads models and leaderboard on mount", async () => {
    mockApi.mockResolvedValueOnce(MODELS);
    mockApi.mockResolvedValueOnce(LEADERBOARD);

    render(<ArenaPage />);

    await waitFor(() => {
      expect(mockApi).toHaveBeenCalledWith("/api/v1/arena/models");
    });
    await waitFor(() => {
      expect(mockApi).toHaveBeenCalledWith("/api/v1/arena/leaderboard");
    });

    expect(screen.getByText("arena.leaderboard")).toBeInTheDocument();
    expect(screen.getByText("deepseek-chat")).toBeInTheDocument();
  });

  it("shows need-two-models warning and disables start when fewer than 2 models", async () => {
    mockApi.mockResolvedValueOnce([MODELS[0]]);
    mockApi.mockResolvedValueOnce({ entries: [] });

    render(<ArenaPage />);

    await waitFor(() => {
      expect(screen.getByText("arena.needTwoModels")).toBeInTheDocument();
    });

    expect(screen.getByText("arena.startBattle")).toBeDisabled();
  });

  it("starts a battle and displays responses", async () => {
    mockApi.mockResolvedValueOnce(MODELS);
    mockApi.mockResolvedValueOnce(LEADERBOARD);
    mockApi.mockResolvedValueOnce(BATTLE);

    render(<ArenaPage />);

    const textarea = await screen.findByPlaceholderText("arena.promptPlaceholder");
    fireEvent.change(textarea, { target: { value: "hello" } });

    fireEvent.click(screen.getByText("arena.startBattle"));

    await waitFor(() => {
      expect(mockApi).toHaveBeenLastCalledWith("/api/v1/arena/battle", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prompt: "hello" }),
      });
    });

    const renderers = await screen.findAllByTestId("result-renderer");
    expect(renderers[0]).toHaveTextContent("response A");
    expect(renderers[1]).toHaveTextContent("response B");
  });

  it("includes manually selected models in battle request", async () => {
    mockApi.mockResolvedValueOnce(MODELS);
    mockApi.mockResolvedValueOnce(LEADERBOARD);
    mockApi.mockResolvedValueOnce(BATTLE);

    render(<ArenaPage />);

    const selects = await screen.findAllByRole("combobox");
    expect(selects.length).toBeGreaterThanOrEqual(2);

    fireEvent.change(selects[0], { target: { value: "deepseek/deepseek-chat" } });
    fireEvent.change(selects[1], { target: { value: "openai/gpt-4o" } });

    const textarea = screen.getByPlaceholderText("arena.promptPlaceholder");
    fireEvent.change(textarea, { target: { value: "compare" } });

    fireEvent.click(screen.getByText("arena.startBattle"));

    await waitFor(() => {
      const lastCall = mockApi.mock.calls[mockApi.mock.calls.length - 1];
      const body = JSON.parse(lastCall[1].body as string);
      expect(body.prompt).toBe("compare");
      expect(body.model_a).toEqual(MODELS[0]);
      expect(body.model_b).toEqual(MODELS[1]);
    });
  });

  it("votes for A, reveals models, and refreshes leaderboard", async () => {
    mockApi
      .mockResolvedValueOnce(MODELS)
      .mockResolvedValueOnce(LEADERBOARD)
      .mockResolvedValueOnce(BATTLE)
      .mockResolvedValueOnce({ ok: true })
      .mockResolvedValueOnce(REVEALED)
      .mockResolvedValueOnce(LEADERBOARD);

    render(<ArenaPage />);

    const textarea = await screen.findByPlaceholderText("arena.promptPlaceholder");
    fireEvent.change(textarea, { target: { value: "vote test" } });
    fireEvent.click(screen.getByText("arena.startBattle"));

    await screen.findAllByTestId("result-renderer");

    fireEvent.click(screen.getByText("arena.voteA"));

    await waitFor(() => {
      expect(mockApi).toHaveBeenCalledWith(`/api/v1/arena/battle/${BATTLE.battle_id}/vote`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ choice: "a" }),
      });
    });

    await waitFor(() => {
      expect(mockApi).toHaveBeenCalledWith(`/api/v1/arena/battle/${BATTLE.battle_id}/reveal`);
    });

    await waitFor(() => {
      expect(mockApi).toHaveBeenCalledWith("/api/v1/arena/leaderboard");
    });

    expect(screen.getByText("DeepSeek-V3 (deepseek)")).toBeInTheDocument();
  });

  it("ties without vote API, then reveal fetches model identities", async () => {
    mockApi
      .mockResolvedValueOnce(MODELS)
      .mockResolvedValueOnce(LEADERBOARD)
      .mockResolvedValueOnce(BATTLE)
      .mockResolvedValueOnce(REVEALED)
      .mockResolvedValueOnce(LEADERBOARD);

    render(<ArenaPage />);

    const textarea = await screen.findByPlaceholderText("arena.promptPlaceholder");
    fireEvent.change(textarea, { target: { value: "tie test" } });
    fireEvent.click(screen.getByText("arena.startBattle"));

    await screen.findAllByTestId("result-renderer");

    fireEvent.click(screen.getByText("arena.tie"));

    expect(mockApi).not.toHaveBeenCalledWith(
      expect.stringContaining("/vote"),
      expect.anything(),
    );

    fireEvent.click(await screen.findByText("arena.reveal"));

    await waitFor(() => {
      expect(mockApi).toHaveBeenCalledWith(`/api/v1/arena/battle/${BATTLE.battle_id}/reveal`);
    });

    await waitFor(() => {
      expect(screen.getByText("GPT-4o (openai)")).toBeInTheDocument();
    });
  });

  it("returns to idle and shows error when battle fails", async () => {
    mockApi.mockResolvedValueOnce(MODELS);
    mockApi.mockResolvedValueOnce(LEADERBOARD);
    mockApi.mockRejectedValueOnce(new Error("model down"));

    render(<ArenaPage />);

    const textarea = await screen.findByPlaceholderText("arena.promptPlaceholder");
    fireEvent.change(textarea, { target: { value: "fail" } });

    fireEvent.click(screen.getByText("arena.startBattle"));

    await waitFor(() => {
      expect(screen.getByText("model down")).toBeInTheDocument();
    });

    expect(screen.getByText("arena.startBattle")).toBeInTheDocument();
  });

  it("resets state when clicking new battle", async () => {
    mockApi
      .mockResolvedValueOnce(MODELS)
      .mockResolvedValueOnce(LEADERBOARD)
      .mockResolvedValueOnce(BATTLE);

    render(<ArenaPage />);

    const textarea = await screen.findByPlaceholderText("arena.promptPlaceholder");
    fireEvent.change(textarea, { target: { value: "reset test" } });
    fireEvent.click(screen.getByText("arena.startBattle"));

    await screen.findAllByTestId("result-renderer");

    fireEvent.click(screen.getByText("arena.newBattle"));

    await waitFor(() => {
      expect(screen.getByPlaceholderText("arena.promptPlaceholder")).toHaveValue("");
    });
    expect(screen.getByText("arena.startBattle")).toBeInTheDocument();
  });

  it("parses models wrapped in { models: [...] }", async () => {
    mockApi.mockResolvedValueOnce({ models: MODELS });
    mockApi.mockResolvedValueOnce(LEADERBOARD);

    render(<ArenaPage />);

    await waitFor(() => {
      expect(mockApi).toHaveBeenCalledWith("/api/v1/arena/models");
    });

    const selects = await screen.findAllByRole("combobox");
    expect(selects[0].querySelectorAll("option").length).toBe(3); // Auto + 2 models
  });
});
