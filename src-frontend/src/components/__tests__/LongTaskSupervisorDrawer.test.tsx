/**
 * LongTaskSupervisorDrawer.test.tsx —— Phase 82 Supervisor 驾驶舱抽屉测试
 *
 * 验证：
 * 1. 抽屉关闭时不渲染
 * 2. 抽屉打开时渲染 5 个 Tab trigger
 * 3. 概览 Tab 空态显示 noLongTasks
 * 4. 概览 Tab 显示长任务卡片
 * 5. 点击卡片切换 activeTaskId
 * 6. Task 6.6: 抽屉打开/活跃任务变化时拉取 supervisor view；生命周期变化时 reconciliation 拉取；WS step 事件不触发 API
 */
import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { LongTaskSupervisorDrawer } from "../LongTaskSupervisorDrawer";
import { useAppStore } from "../../store/appStore";
import { useTaskStore } from "../../store/taskStore";
import { longTaskStepStart } from "../../hooks/wsHandlers/supervisorHandlers";
import type { TaskStore } from "../../types/task";

const mockApi = vi.fn().mockResolvedValue({
  task_id: "t1",
  status: "running",
  confirmed_goal: "goal",
  grading_levels: [],
  assumptions: [],
  plan_tree: [],
  current_step_id: null,
  elapsed_seconds: 0,
  estimated_remaining_seconds: null,
  review_log: [],
  stall_rounds: 0,
  suggestions: [],
});

vi.mock("../../hooks/useApi", () => ({
  useApi: () => ({ api: mockApi }),
}));

/** DetailDrawer 用 createPortal 到 #overlay-root，测试需提供该容器 */
function ensureOverlayRoot() {
  let root = document.getElementById("overlay-root");
  if (!root) {
    root = document.createElement("div");
    root.id = "overlay-root";
    document.body.appendChild(root);
  }
  return root;
}

const LONG_TASK = {
  taskId: "t1",
  agentId: "hermes",
  prompt: "Build a web app",
  status: "running" as const,
  chunks: [],
  thinkingChunks: [],
  toolCalls: [],
  toolResults: [],
  routeReason: "",
  errorMessage: null,
  isLongTask: true,
};

describe("LongTaskSupervisorDrawer", () => {
  beforeEach(() => {
    ensureOverlayRoot();
    vi.clearAllMocks();
    mockApi.mockReset().mockResolvedValue({
      task_id: "t1",
      status: "running",
      confirmed_goal: "goal",
      grading_levels: [],
      assumptions: [],
      plan_tree: [],
      current_step_id: null,
      elapsed_seconds: 0,
      estimated_remaining_seconds: null,
      review_log: [],
      stall_rounds: 0,
      suggestions: [],
    });
    useAppStore.setState({ supervisorDrawerOpen: false });
    useTaskStore.setState({
      tasks: {},
      supervisorActiveTaskId: null,
      supervisorViews: {},
    });
  });

  it("抽屉关闭时不渲染", () => {
    useAppStore.setState({ supervisorDrawerOpen: false });
    render(<LongTaskSupervisorDrawer />);
    const root = document.getElementById("overlay-root")!;
    expect(root.children.length).toBe(0);
  });

  it("抽屉打开时渲染 5 个 Tab trigger", () => {
    useAppStore.setState({ supervisorDrawerOpen: true });
    render(<LongTaskSupervisorDrawer />);
    expect(screen.getByText("supervisor.overview")).toBeInTheDocument();
    expect(screen.getByText("supervisor.goal")).toBeInTheDocument();
    expect(screen.getByText("supervisor.tree")).toBeInTheDocument();
    expect(screen.getByText("supervisor.review")).toBeInTheDocument();
    expect(screen.getByText("supervisor.modify")).toBeInTheDocument();
  });

  it("概览 Tab 空态显示 noLongTasks", () => {
    useAppStore.setState({ supervisorDrawerOpen: true });
    render(<LongTaskSupervisorDrawer />);
    expect(screen.getByText("supervisor.noLongTasks")).toBeInTheDocument();
  });

  it("概览 Tab 显示长任务卡片", () => {
    useAppStore.setState({ supervisorDrawerOpen: true });
    useTaskStore.setState({ tasks: { t1: LONG_TASK } });
    render(<LongTaskSupervisorDrawer />);
    expect(screen.getByText("Build a web app")).toBeInTheDocument();
  });

  it("点击长任务卡片切换 activeTaskId", () => {
    useAppStore.setState({ supervisorDrawerOpen: true });
    useTaskStore.setState({ tasks: { t1: LONG_TASK } });
    render(<LongTaskSupervisorDrawer />);
    fireEvent.click(screen.getByText("Build a web app"));
    expect(useTaskStore.getState().supervisorActiveTaskId).toBe("t1");
  });

  it("Phase 105: 概览 Tab 显示 4 个角色卡片", () => {
    useAppStore.setState({ supervisorDrawerOpen: true });
    useTaskStore.setState({
      tasks: { t1: LONG_TASK },
      supervisorActiveTaskId: "t1",
    });
    render(<LongTaskSupervisorDrawer />);
    expect(screen.getByText("supervisor.roles.pm")).toBeInTheDocument();
    expect(screen.getByText("supervisor.roles.dev")).toBeInTheDocument();
    expect(screen.getByText("supervisor.roles.reviewer")).toBeInTheDocument();
    expect(screen.getByText("supervisor.roles.merge")).toBeInTheDocument();
  });

  it("Task 6.13: 当前角色卡片标记 aria-current='step'", () => {
    useAppStore.setState({ supervisorDrawerOpen: true });
    useTaskStore.setState({
      tasks: {
        t1: { ...LONG_TASK, lifecycle: { phase: "executing", stepIndex: 0, totalSteps: 5 } },
      },
      supervisorActiveTaskId: "t1",
    });
    render(<LongTaskSupervisorDrawer />);
    const devCard = screen.getByText("supervisor.roles.dev").parentElement;
    expect(devCard).toHaveAttribute("role", "listitem");
    expect(devCard).toHaveAttribute("aria-current", "step");
  });

  it("Task 6.13: 长任务状态徽章显示可访问的状态文本", () => {
    useAppStore.setState({ supervisorDrawerOpen: true });
    useTaskStore.setState({ tasks: { t1: LONG_TASK } });
    render(<LongTaskSupervisorDrawer />);
    expect(screen.getByText("running")).toBeInTheDocument();
  });

  // ── Task 6.6: WS 事件驱动 + reconciliation 拉取 ──

  it("抽屉打开且选中任务时拉取 supervisor view", async () => {
    useAppStore.setState({ supervisorDrawerOpen: true });
    useTaskStore.setState({
      tasks: { t1: LONG_TASK },
      supervisorActiveTaskId: "t1",
    });
    render(<LongTaskSupervisorDrawer />);
    await waitFor(() => {
      expect(mockApi).toHaveBeenCalledWith("/api/v1/tasks/t1/supervisor");
    });
  });

  it("long_task_step_start 仅 patch store，不触发 API 拉取", async () => {
    useAppStore.setState({ supervisorDrawerOpen: true });
    useTaskStore.setState({
      tasks: { t1: LONG_TASK },
      supervisorActiveTaskId: "t1",
      supervisorViews: {
        t1: {
          task_id: "t1",
          status: "running",
          confirmed_goal: "goal",
          plan_tree: [],
          current_step_id: null,
          review_log: [],
        } as never,
      },
    });
    render(<LongTaskSupervisorDrawer />);
    await waitFor(() => {
      expect(mockApi).toHaveBeenCalledWith("/api/v1/tasks/t1/supervisor");
    });
    mockApi.mockClear();

    // 模拟 WS 事件：只更新 store，不调用 api
    longTaskStepStart({ task_id: "t1", step_id: "s2" } as never, {} as TaskStore);
    expect(useTaskStore.getState().supervisorViews.t1.current_step_id).toBe("s2");
    expect(mockApi).not.toHaveBeenCalled();
  });

  it("生命周期 phase/stepIndex 变化时触发 reconciliation 拉取", async () => {
    useAppStore.setState({ supervisorDrawerOpen: true });
    useTaskStore.setState({
      tasks: {
        t1: {
          ...LONG_TASK,
          lifecycle: { phase: "clarifying", stepIndex: 0, totalSteps: 5 },
        } as never,
      },
      supervisorActiveTaskId: "t1",
    });
    render(<LongTaskSupervisorDrawer />);
    await waitFor(() => {
      expect(mockApi).toHaveBeenCalledWith("/api/v1/tasks/t1/supervisor");
    });
    const initialCalls = mockApi.mock.calls.length;

    // 模拟生命周期变化
    useTaskStore.setState({
      tasks: {
        t1: {
          ...LONG_TASK,
          lifecycle: { phase: "executing", stepIndex: 1, totalSteps: 5 },
        } as never,
      },
    });

    await waitFor(() => {
      expect(mockApi.mock.calls.length).toBeGreaterThan(initialCalls);
    });
    expect(mockApi).toHaveBeenLastCalledWith("/api/v1/tasks/t1/supervisor");
  });

  it("抽屉关闭后不触发 API 拉取", async () => {
    useAppStore.setState({ supervisorDrawerOpen: false });
    useTaskStore.setState({
      tasks: { t1: LONG_TASK },
      supervisorActiveTaskId: "t1",
    });
    render(<LongTaskSupervisorDrawer />);
    await new Promise((r) => setTimeout(r, 50));
    expect(mockApi).not.toHaveBeenCalled();
  });

  it("点击 header 刷新按钮触发拉取", async () => {
    useAppStore.setState({ supervisorDrawerOpen: true });
    useTaskStore.setState({
      tasks: { t1: LONG_TASK },
      supervisorActiveTaskId: "t1",
    });
    render(<LongTaskSupervisorDrawer />);
    const refreshBtn = await screen.findByRole("button", { name: "supervisor.refresh" });
    mockApi.mockClear();
    fireEvent.click(refreshBtn);
    await waitFor(() => {
      expect(mockApi).toHaveBeenCalledWith("/api/v1/tasks/t1/supervisor");
    });
  });
});
