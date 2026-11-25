import { beforeEach, describe, expect, it } from "vitest";
import { useAppStore } from "../../../store/appStore";
import { useTaskStore } from "../../../store/taskStore";
import { sessionTitleUpdated, todoUpdated } from "../sessionHandlers";

describe("sessionTitleUpdated", () => {
  beforeEach(() => {
    useTaskStore.setState({
      sessions: [{
        session_id: "s1", title: "旧标题", preview: "", msg_count: 1,
        last_at: 1, agents: [],
      }],
    });
    useAppStore.setState({
      openSessionTabs: [{ sessionId: "s1", title: "旧标题" }],
      activeSessionTab: "s1",
    });
  });

  it("同时更新会话列表与已打开的 tab 标题", () => {
    sessionTitleUpdated({ session_id: "s1", title: "新标题" }, useTaskStore.getState());
    expect(useTaskStore.getState().sessions[0].title).toBe("新标题");
    expect(useAppStore.getState().openSessionTabs[0].title).toBe("新标题");
  });

  it("忽略缺标题或缺 session_id 的消息", () => {
    sessionTitleUpdated({ session_id: "s1", title: "  " }, useTaskStore.getState());
    sessionTitleUpdated({ title: "无 id" }, useTaskStore.getState());
    expect(useTaskStore.getState().sessions[0].title).toBe("旧标题");
  });

  it("todo_updated 更新当前会话的清单", () => {
    useTaskStore.setState({ currentSessionId: "s1" });
    todoUpdated({
      session_id: "s1",
      todos: [{ content: "写实现", status: "in_progress" }],
    }, useTaskStore.getState());
    expect(useTaskStore.getState().sessionTodos).toEqual([
      { content: "写实现", status: "in_progress" },
    ]);
  });

  it("别的会话的清单更新不污染当前会话", () => {
    useTaskStore.setState({ currentSessionId: "s1", sessionTodos: [] });
    todoUpdated({
      session_id: "s2",
      todos: [{ content: "别的会话", status: "pending" }],
    }, useTaskStore.getState());
    expect(useTaskStore.getState().sessionTodos).toEqual([]);
  });
});
