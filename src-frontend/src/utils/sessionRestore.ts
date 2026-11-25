import { useTaskStore } from "../store/taskStore";
import i18next from "i18next";

/** Phase 17: 从 API 加载会话消息，恢复到 taskStore。
 *
 * 会话表只存用户消息 + 最终文本回复。中间 tool call/result 在
 * context["history"] 中流转，不持久化——与 Claude Code / Cursor 一致。
 */

export interface SessionMessage {
  role: string;
  content: string;
  id: string;
  agent_id: string;
}

export type SessionApi = <T>(path: string, options?: RequestInit) => Promise<T>;

// Batch 4a F1: 会话切换竞态防护——模块级递增序号 + AbortController。
// 快速切换 A→B 时 A 的慢响应后到，凭「发起时序号」判定过期直接丢弃，
// 不再无条件 setCurrentSession + clearTasks 覆盖 B。等价于 React Query
// 按 queryKey 取消旧请求的轻量实现。
let _restoreSeq = 0;
let _activeController: AbortController | null = null;

/** Batch 4a F1: 拉取会话消息并恢复（fetch + 竞态防护一体）。
 * 新调用发起时 abort 上一个未完成请求；被 abort / 序号过期的响应静默丢弃；
 * 真实网络错误向上抛出供调用方记录。 */
export async function switchSessionFromApi(sessionId: string, api: SessionApi): Promise<void> {
  const seq = ++_restoreSeq;
  _activeController?.abort();
  const controller = new AbortController();
  _activeController = controller;

  let msgs: SessionMessage[];
  try {
    msgs = await api<SessionMessage[]>(`/api/v1/sessions/${sessionId}/messages`, {
      signal: controller.signal,
    });
  } catch (err) {
    // 被更新的切换 abort 或响应已过期——丢弃；否则是真实错误，向上抛
    if (controller.signal.aborted || seq !== _restoreSeq) return;
    throw err;
  }
  if (seq !== _restoreSeq) return; // 过期响应——已有更新的切换，丢弃
  applySessionMessages(sessionId, msgs, () => seq === _restoreSeq);
}

/** Phase 17: 将已拉取的消息恢复到 taskStore（保留直接调用入口）。
 * Batch 4a F1: 写 store 前登记序号——若已有更新的切换发起则整体跳过。 */
export async function restoreSessionFromApi(sessionId: string, msgs: SessionMessage[]): Promise<void> {
  const seq = ++_restoreSeq;
  applySessionMessages(sessionId, msgs, () => seq === _restoreSeq);
}

function applySessionMessages(
  sessionId: string,
  msgs: SessionMessage[],
  isCurrent: () => boolean,
): void {
  const store = useTaskStore.getState();
  if (!isCurrent()) return;
  store.setCurrentSession(sessionId);
  store.clearTasks();

  let currentTaskId = "";
  for (const m of msgs) {
    if (!isCurrent()) return;
    if (m.role === "user") {
      if (currentTaskId) {
        store.setTaskDone(currentTaskId);
      }
      currentTaskId = m.id;
      store.createTask(m.id, m.agent_id || "unknown", m.content, i18next.t("sessionRestore.restored"), m.id);
    } else if ((m.role === "assistant" || m.role === "agent") && currentTaskId) {
      store.appendChunk(currentTaskId, m.content);
      store.setTaskDone(currentTaskId);
      currentTaskId = "";
    }
  }
  if (currentTaskId && isCurrent()) {
    store.setTaskDone(currentTaskId);
  }
}
