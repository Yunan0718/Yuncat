import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { SessionList } from "../SessionList";
import { useAppStore } from "../../store/appStore";
import { useTaskStore } from "../../store/taskStore";
import type { SessionSummary } from "../../store/taskStore";

// 可切换语言的 i18n mock。lang.current 为空时返回裸 key（与全局 setup 一致，
// 保证本文件其余用例的断言不受影响）；非空时返回 `语言:key`，供「切语言后错误提示
// 跟随更新」这条回归用例使用。
// 用 vi.hoisted 是因为 vi.mock 的工厂会被提升到文件顶部，在 import 之前求值，
// 直接引用模块顶层变量会踩 TDZ。
const { lang } = vi.hoisted(() => ({ lang: { current: "" } }));
vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string) => (lang.current ? `${lang.current}:${key}` : key),
    i18n: { language: lang.current || "zh" },
  }),
  initReactI18next: { type: "3rdParty", init: vi.fn() },
}));

const mockApi = vi.fn();

vi.mock("../../hooks/useApi", () => ({
  useApi: () => ({ api: mockApi }),
}));

vi.mock("../../utils/sessionRestore", () => ({
  restoreSessionFromApi: vi.fn().mockResolvedValue(undefined),
}));

const SESSIONS: SessionSummary[] = [
  {
    session_id: "sess-a",
    title: "First chat",
    preview: "hello",
    msg_count: 2,
    last_at: Math.floor(Date.now() / 1000) - 60,
    agents: ["hermes"],
  },
  {
    session_id: "sess-b",
    title: "Second chat",
    preview: "world",
    msg_count: 5,
    last_at: Math.floor(Date.now() / 1000) - 120,
    agents: ["openclaw"],
  },
];

/** 按 URL 分派 mock：组件现在会并发发「会话列表 + 跨会话统计」两个请求，
 * 不能再靠 mockResolvedValueOnce 的排队顺序（顺序一变就会串味）。 */
function mockSessionApi(list: SessionSummary[] = SESSIONS) {
  mockApi.mockImplementation((path: string) => {
    if (path.includes("/sessions/stats")) {
      return Promise.resolve({ session_count: 0, task_count: 0, total_tokens: 0 });
    }
    if (path === "/api/v1/sessions") return Promise.resolve(list);
    if (path.includes("/api/v1/sessions/")) return Promise.resolve({});  // DELETE
    return Promise.resolve(list);
  });
}

function setupBackendReady() {
  useAppStore.setState({ backendReady: true });
  useTaskStore.setState({
    sessions: SESSIONS,
    currentSessionId: null,
    sessionListVersion: 0,
    tasks: {},
    activeTaskId: null,
  });
}

// 会话用量展示：账本口径的 token 用量（输入 + 输出）
describe("SessionList token 用量", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    lang.current = "";
    mockApi.mockResolvedValue(SESSIONS);
    setupBackendReady();
  });

  it("有用量的会话显示 token 用量，没用量的不显示", async () => {
    // 会话列表由接口返回——统计字段随列表一起回来
    mockApi.mockResolvedValue([
      { ...SESSIONS[0], total_tokens: 12345 },
      { ...SESSIONS[1], total_tokens: 0 },
    ]);
    render(<SessionList />);
    await waitFor(() => expect(mockApi).toHaveBeenCalledWith("/api/v1/sessions"));
    expect(screen.getAllByText("sessionList.tokenUsage")).toHaveLength(1);
  });

  it("顶部显示跨会话统计（会话数 / 任务数 / token）", async () => {
    mockApi.mockImplementation((path: string) => {
      if (path.includes("/sessions/stats")) {
        return Promise.resolve({ session_count: 3, task_count: 7, total_tokens: 12345 });
      }
      return Promise.resolve(SESSIONS);
    });
    render(<SessionList />);
    await waitFor(() => expect(screen.getByText("sessionList.statsLine")).toBeInTheDocument());
  });

  it("后端没给用量字段时也不显示（老库/旧接口不炸）", async () => {
    useTaskStore.setState({ sessions: SESSIONS });
    render(<SessionList />);
    await waitFor(() => expect(mockApi).toHaveBeenCalledWith("/api/v1/sessions"));
    expect(screen.queryByText("sessionList.tokenUsage")).not.toBeInTheDocument();
  });
});

// Task 6.9: 会话删除二次确认测试
describe("SessionList delete confirmation", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    lang.current = "";
    mockApi.mockResolvedValue(SESSIONS);
    setupBackendReady();
  });

  it("右键会话显示删除菜单", async () => {
    render(<SessionList />);
    await waitFor(() => expect(mockApi).toHaveBeenCalledWith("/api/v1/sessions"));

    const firstSession = screen.getByText("First chat");
    fireEvent.contextMenu(firstSession);

    expect(screen.getByText("sessionList.delete")).toBeInTheDocument();
  });

  it("点击删除后弹出确认对话框并显示会话标题", async () => {
    render(<SessionList />);
    await waitFor(() => expect(mockApi).toHaveBeenCalledWith("/api/v1/sessions"));

    fireEvent.contextMenu(screen.getByText("First chat"));
    fireEvent.click(screen.getByText("sessionList.delete"));

    expect(screen.getByText("sessionList.confirmDeleteTitle")).toBeInTheDocument();
    expect(screen.getByText(/First chat/)).toBeInTheDocument();
  });

  it("点击取消不调用 DELETE 且对话框关闭", async () => {
    render(<SessionList />);
    await waitFor(() => expect(mockApi).toHaveBeenCalledWith("/api/v1/sessions"));

    fireEvent.contextMenu(screen.getByText("First chat"));
    fireEvent.click(screen.getByText("sessionList.delete"));

    fireEvent.click(screen.getByText("common.cancel"));

    await waitFor(() => {
      expect(screen.queryByText("sessionList.confirmDeleteTitle")).not.toBeInTheDocument();
    });
    expect(mockApi).not.toHaveBeenCalledWith(
      expect.stringContaining("/api/v1/sessions/sess-a"),
      expect.objectContaining({ method: "DELETE" }),
    );
  });

  it("点击确认删除调用 DELETE 并刷新会话列表", async () => {
    mockSessionApi();
    render(<SessionList />);
    await waitFor(() => expect(mockApi).toHaveBeenCalledWith("/api/v1/sessions"));

    fireEvent.contextMenu(screen.getByText("First chat"));
    fireEvent.click(screen.getByText("sessionList.delete"));

    fireEvent.click(screen.getByText("sessionList.confirmDelete"));

    await waitFor(() => {
      expect(mockApi).toHaveBeenCalledWith("/api/v1/sessions/sess-a", { method: "DELETE" });
    });
    expect(useTaskStore.getState().sessionListVersion).toBe(1);
  });

  it("删除当前会话时清空当前会话状态", async () => {
    useTaskStore.setState({ currentSessionId: "sess-a" });
    mockSessionApi();
    render(<SessionList />);
    await waitFor(() => expect(mockApi).toHaveBeenCalledWith("/api/v1/sessions"));

    fireEvent.contextMenu(screen.getByText("First chat"));
    fireEvent.click(screen.getByText("sessionList.delete"));
    fireEvent.click(screen.getByText("sessionList.confirmDelete"));

    await waitFor(() => {
      expect(useTaskStore.getState().currentSessionId).toBeNull();
    });
  });

  // 回归：error 状态存的是 i18n key 而非翻译后的文案。
  // 若改回存文案，切语言后这条提示会一直停在旧语言——要等下次请求失败才更新。
  it("加载失败后切换语言，错误提示改用新语言渲染", async () => {
    lang.current = "zh";
    mockApi.mockImplementation((path: string) => {
      if (path === "/api/v1/sessions") return Promise.reject(new Error("boom"));
      return Promise.resolve([]);
    });

    render(<SessionList />);
    await waitFor(() => {
      expect(screen.getByText("zh:sessionList.loadFailed")).toBeInTheDocument();
    });

    // 切语言。仅改 lang.current 不会触发渲染，借搜索框输入制造一次重渲染。
    lang.current = "en";
    fireEvent.change(screen.getByPlaceholderText("zh:sessionList.searchPlaceholder"), {
      target: { value: "x" },
    });

    expect(screen.getByText("en:sessionList.loadFailed")).toBeInTheDocument();
  });
});
