import { useAppStore } from "../../store/appStore";
import { useTaskStore } from "../../store/taskStore";
import type { TodoItem } from "../../types/task";
import type { Handler } from "./taskHandlers";

/**
 * 会话标题更新（后端自动生成或用户重命名）。
 *
 * 同步两处：会话列表（taskStore.sessions）与已打开的会话 tab（appStore）。
 * 多窗口下后端会广播，其他窗口也会跟着更新。
 */
export const sessionTitleUpdated: Handler = (msg, store) => {
  const sessionId = String(msg.session_id || "");
  const title = String(msg.title || "").trim();
  if (!sessionId || !title) return;
  store.setSessions(
    store.sessions.map((s) => (s.session_id === sessionId ? { ...s, title } : s)),
  );
  useAppStore.getState().updateSessionTabTitle(sessionId, title);
};

/** 会话任务清单更新（Agent 调 todo_write 后的广播）。 */
export const todoUpdated: Handler = (msg, store) => {
  const sessionId = String(msg.session_id || "");
  if (!sessionId) return;
  if (store.currentSessionId && store.currentSessionId !== sessionId) return;
  const raw = Array.isArray(msg.todos) ? msg.todos : [];
  const todos: TodoItem[] = raw
    .filter((item: unknown): item is Record<string, unknown> => (
      !!item && typeof item === "object" && typeof (item as { content?: unknown }).content === "string"
    ))
    .map((item) => ({
      content: String(item.content),
      status: (["pending", "in_progress", "completed"] as const).includes(item.status as never)
        ? (item.status as TodoItem["status"])
        : "pending",
    }));
  store.setSessionTodos(todos);
};

/** 交付物变更（present 声明完成）——通知面板重新拉取。 */
export const deliverablesUpdated: Handler = () => {
  useTaskStore.getState().bumpDeliverables();
};
