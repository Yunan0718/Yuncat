import { useEffect, useCallback, useRef } from "react";
import i18next from "i18next";
import { useAppStore, type AppNotification, type ReasoningEffort, capabilityToReasoning } from "../store/appStore";
import { useTaskStore, type TaskState } from "../store/taskStore";
// dispatchToStore 已按消息域拆分到 wsHandlers/ 目录（handler map 模式）
import { dispatchToStore } from "./wsHandlers";

// 重新导出 getNotifiedSet 以保持向后兼容（useNotification.ts 仍从此模块导入）
export { getNotifiedSet } from "./wsHandlers";

/** Parse an API response and surface server-side errors to the caller. */
async function readJsonResponse<T>(response: Response): Promise<T> {
  let payload: unknown = null;
  try {
    payload = await response.json();
  } catch {
    // A successful empty response is valid for a few mutation endpoints.
  }
  if (!response.ok) {
    const detail = payload && typeof payload === "object" && "detail" in payload
      ? String((payload as { detail?: unknown }).detail || "")
      : "";
    throw new Error(detail || `HTTP ${response.status}`);
  }
  return payload as T;
}
/** Phase 49-D: 断线重连后恢复任务状态到 taskStore。 */
function restoreTaskStateSnapshot(taskId: string, payload: Record<string, unknown>) {
  const store = useTaskStore.getState();
  const task = store.tasks[taskId];
  if (!task) return;

  const state = (payload.task_state as Record<string, unknown>) || {};
  const messages = Array.isArray(payload.messages) ? payload.messages : [];

  const chunkSet = new Set<string>();
  const thinkingSet = new Set<string>();
  const chunks: string[] = [];
  const thinkingChunks: string[] = [];

  const pushChunk = (text: string) => {
    if (!text || chunkSet.has(text)) return;
    chunkSet.add(text);
    chunks.push(text);
  };
  const pushThinking = (text: string) => {
    if (!text || thinkingSet.has(text)) return;
    thinkingSet.add(text);
    thinkingChunks.push(text);
  };

  if (typeof state.generated_response === "string") pushChunk(state.generated_response);
  if (typeof state.output === "string") pushChunk(state.output);

  for (const m of messages) {
    if (!m || typeof m !== "object") continue;
    const raw = m as Record<string, unknown>;
    const role = String(raw.role || "").toLowerCase();
    const content = typeof raw.content === "string" ? raw.content : "";
    if (role === "assistant" || role === "agent") pushChunk(content);
    else if (role === "thinking" || role === "reasoning") pushThinking(content);
  }

  if (typeof state.thinking === "string") pushThinking(state.thinking);

  const toolCalls = Array.isArray(state.tool_calls)
    ? state.tool_calls.map((tc: unknown, idx: number) => {
        const raw = tc as Record<string, unknown>;
        return {
          index: typeof raw.index === "number" ? raw.index : idx,
          name: String(raw.name || raw.id || ""),
          args: raw.arguments || raw.args || "",
        };
      })
    : task.toolCalls;

  const remoteStatus = String(payload.status || state.status || "");
  let status = task.status;
  // G4: interrupted 也接受远端状态（重连后恢复优雅关闭标记的中断任务）
  if (
    remoteStatus === "completed" ||
    remoteStatus === "failed" ||
    remoteStatus === "cancelled" ||
    remoteStatus === "interrupted"
  ) {
    status = remoteStatus as TaskState["status"];
  } else if (
    remoteStatus === "running" &&
    !["completed", "failed", "cancelled", "interrupted"].includes(task.status)
  ) {
    status = "running";
  }

  useTaskStore.setState({
    tasks: { ...store.tasks, [taskId]: { ...task, chunks, thinkingChunks, toolCalls, status } },
  });
}

/* ══════════════════════════════════════════════════════════════
 * WebSocket Singleton — 对标 Figma/Discord
 * WS 长连接脱离 React 组件生命周期管理。
 * 消息分发直接在 `onmessage` 中处理，不依赖组件挂载次数。
 * ══════════════════════════════════════════════════════════════ */

export class WsConnection {
  private ws: WebSocket | null = null;
  private port = 0;
  private token = "";
  private reconnectCount = 0;
  private _authRefreshPending = false;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private heartbeatTimer: ReturnType<typeof setInterval> | null = null;
  private pongTimer: ReturnType<typeof setTimeout> | null = null;
  private destroyed = false;
  private shouldReconnect = true;
  /** Batch 8.5: 离线消息缓冲队列（WS 断开期间缓冲，重连后 flush） */
  private outbox: object[] = [];
  private static readonly OUTBOX_MAX = 50;

  configure(port: number, token: string) {
    this.port = port;
    this.token = token;
  }

  connect() {
    if (this.destroyed || !this.port || !this.token) return;
    if (this.ws?.readyState === WebSocket.OPEN) return;
    if (this.ws?.readyState === WebSocket.CONNECTING) return;
    this.close();
    const ws = new WebSocket(`ws://127.0.0.1:${this.port}/ws`);
    this.ws = ws;
    ws.onopen = () => {
      ws.send(JSON.stringify({ type: "auth", token: this.token, language: i18next.language }));
      this.reconnectCount = 0;
      this.shouldReconnect = true;
      useAppStore.setState({ backendDead: false });
      const tasks = useTaskStore.getState().tasks;
      // P1: 重连后对所有 running task 发送 resume（后端继续推送 WS 事件）
      for (const [tid, t] of Object.entries(tasks)) {
        if (t.status === "running") ws.send(JSON.stringify({ type: "resume", task_id: tid }));
      }
      this._startHeartbeat();
      // P1: 状态快照恢复必须覆盖所有 running task——断线期间后端可能已结束某些任务，
      // 旧实现只恢复 activeTaskId 会导致非 active 的 running task 永久卡在 running 状态
      // （后端不再推送事件，前端无法对账）。现在对所有 running task 调 _restoreTaskState。
      for (const [tid, t] of Object.entries(tasks)) {
        if (t.status === "running") this._restoreTaskState(tid);
      }
      // Batch 8.5: 重连后 flush 离线期间缓冲的消息
      this._flushOutbox();
      // 重连后补拉通知历史——断线期间落库的通知不会经 WS 推送
      this._restoreNotifications();
    };
    ws.onmessage = (event) => {
      this._resetPongWatchdog();
      try {
        dispatchToStore(JSON.parse(event.data));
      } catch {
        // no-op
      }
    };
    ws.onclose = (ev) => {
      this.ws = null;
      this._stopHeartbeat();
      // 2026-10-07：认证失败（后端 close 1008）必须换 token 再重连。
      // access token TTL 只有 15 分钟，而这里原先只是原样重连——用同一枚过期
      // token 反复撞 1008，日志表现为 `WS auth rejected: have_token=True
      // token_match=False` 每隔约半小时出现一次，界面像是「随机掉线」。
      // 刷新走主进程（它持有 refresh token），成功则 setToken 触发重连。
      if (ev && ev.code === 1008 && !this._authRefreshPending) {
        this._authRefreshPending = true;
        void this._refreshTokenThenReconnect();
        return;
      }
      this._scheduleReconnect();
    };
    ws.onerror = () => {};
  }

  disconnect() {
    this.shouldReconnect = false;
    this.close();
  }

  close() {
    this._stopHeartbeat();
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    if (this.ws) {
      this.ws.onclose = null;
      this.ws.close();
      this.ws = null;
    }
  }

  send(data: object) {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(data));
      return;
    }
    if (this.ws?.readyState === WebSocket.CONNECTING) {
      this.ws.addEventListener(
        "open",
        () => {
          if (this.ws?.readyState === WebSocket.OPEN) this.ws!.send(JSON.stringify(data));
        },
        { once: true },
      );
      return;
    }
    // Batch 8.5: WS 断开时缓冲到 outbox（替代原 broken setTimeout 无限重试）
    // 有界 FIFO——满时丢弃最老，避免内存泄漏
    this.outbox.push(data);
    if (this.outbox.length > WsConnection.OUTBOX_MAX) {
      this.outbox.shift();
    }
    useAppStore.setState({ outboxCount: this.outbox.length });
  }

  /** Batch 8.5: 重连成功后 flush outbox 中缓冲的消息 */
  private _flushOutbox() {
    if (this.outbox.length === 0) return;
    if (this.ws?.readyState !== WebSocket.OPEN) return;
    const queued = this.outbox;
    this.outbox = [];
    useAppStore.setState({ outboxCount: 0 });
    for (const msg of queued) {
      try {
        this.ws!.send(JSON.stringify(msg));
      } catch (e) {
        console.warn("useWebSocket: outbox flush failed for msg:", e);
      }
    }
  }

  destroy() {
    this.destroyed = true;
    this.outbox = [];
    useAppStore.setState({ outboxCount: 0 });
    this.disconnect();
  }


  /** 2026-10-07：认证失败（1008）时先换 token，再重连。 */
  private async _refreshTokenThenReconnect(): Promise<void> {
    try {
      const res = await window.yuncat?.refreshAccessToken?.();
      if (res?.ok && res.token) {
        useAppStore.getState().setToken(res.token);
        this.token = res.token;   // configure() 之外也同步，避免重连仍用旧值
      }
    } catch {
      // 刷新失败：交给 _scheduleReconnect 的退避重试，不在此处吞掉重连机会
    } finally {
      this._authRefreshPending = false;
      this._scheduleReconnect();
    }
  }

  _scheduleReconnect() {
    if (this.destroyed || !this.shouldReconnect) return;
    const delay = Math.min(1000 * Math.pow(2, Math.min(this.reconnectCount, 5)), 30000) + Math.random() * 1000;
    this.reconnectCount++;
    useAppStore.setState({ backendDead: true });
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    this.reconnectTimer = setTimeout(() => this.connect(), delay);
  }

  private _restoreTaskState(taskId: string) {
    if (!this.port || !this.token) return;
    fetch(`http://127.0.0.1:${this.port}/api/v1/tasks/${taskId}/state`, {
      headers: { Authorization: `Bearer ${this.token}` },
    })
      .then((r) => r.json())
      .then((data) => restoreTaskStateSnapshot(taskId, data as Record<string, unknown>))
      .catch((e) => console.warn(`useWebSocket: task state restore failed for ${taskId}:`, e));
  }

  /** 重连后补拉通知历史——断线期间落库的通知不会被 WS 推送，须主动对账。 */
  private _restoreNotifications() {
    if (!this.port || !this.token) return;
    fetch(`http://127.0.0.1:${this.port}/api/v1/notifications`, {
      headers: { Authorization: `Bearer ${this.token}` },
    })
      .then((r) => r.json())
      .then((rows: AppNotification[]) => {
        const existing = new Set(
          useAppStore.getState().notifications.map((n) => n.id),
        );
        for (const n of rows) {
          if (!existing.has(n.id)) useAppStore.getState().addNotification(n);
        }
      })
      .catch((e) => console.warn("useWebSocket: notifications restore failed:", e));
  }

  private _startHeartbeat() {
    this._stopHeartbeat();
    this.heartbeatTimer = setInterval(() => {
      if (this.ws?.readyState === WebSocket.OPEN) {
        this.ws.send(JSON.stringify({ type: "ping", ts: Date.now() }));
        this._resetPongWatchdog();
      }
    }, 30000);
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({ type: "ping", ts: Date.now() }));
    }
    this._resetPongWatchdog();
  }
  private _stopHeartbeat() {
    if (this.heartbeatTimer) {
      clearInterval(this.heartbeatTimer);
      this.heartbeatTimer = null;
    }
    if (this.pongTimer) {
      clearTimeout(this.pongTimer);
      this.pongTimer = null;
    }
  }
  private _resetPongWatchdog() {
    if (this.pongTimer) clearTimeout(this.pongTimer);
    this.pongTimer = setTimeout(() => {
      if (this.ws) {
        this.ws.onclose = null;
        this.ws.close();
        this.ws = null;
      }
      this._stopHeartbeat();
      this._scheduleReconnect();
    }, 40000);
  }
}

const _ws = new WsConnection();

/** 任意组件可直接调用发送 WS 消息（无需 useWebSocket hook） */
export function sendWsMessage(data: object) {
  _ws.send(data);
}

/* ══════════════════════════════════════════════════════════════
 * React Hook —— 薄消费层。只暴露 send/confirm/cancel。
 * 消息分发已完全在 WsConnection + dispatchToStore 中处理。
 * ══════════════════════════════════════════════════════════════ */

export function useWebSocket() {
  const port = useAppStore((s) => s.port) ?? 0;
  const token = useAppStore((s) => s.token);
  const backendReady = useAppStore((s) => s.backendReady);

  useEffect(() => {
    _ws.configure(port, token ?? "");
  }, [port, token]);

  const didConnect = useRef(false);
  useEffect(() => {
    if (backendReady && port > 0 && token && !didConnect.current) {
      didConnect.current = true;
      _ws.connect();
    }
  }, [backendReady, port, token]);

  useEffect(() => {
    const onBeforeUnload = () => _ws.disconnect();
    window.addEventListener("beforeunload", onBeforeUnload);
    return () => window.removeEventListener("beforeunload", onBeforeUnload);
  }, []);

  const submitTask = useCallback(
    (
      prompt: string,
      agentId?: string,
      sessionId?: string,
      mode: string = "agent",
      attachments?: Array<{ name: string; type: string; data?: string; path?: string; bytes?: number; sha256?: string }>,
      capabilityLevel?: number,
      reasoningEffort?: ReasoningEffort,
    ) => {
      // 能力等级 0=轻量 / 1=标准 / 2=强力；默认 1。max_mode 由等级推导（L2 ⇒ MAX）。
      const _cap = capabilityLevel ?? 1;
      const _maxMode = _cap >= 2;
      // 思考强度默认随能力等级联动（0→low / 1→medium / 2→high），可被前端单独覆盖。
      const _reasoning = reasoningEffort ?? capabilityToReasoning(_cap);
      // 长任务模式：发送 long_task_confirm 而非 submit_task（后端走 orchestrator 分解执行）
      if (mode === "long_task") {
        _ws.send({
          type: "long_task_confirm",
          prompt,
          agent_id: agentId ?? undefined,
          session_id: sessionId,
          language: i18next.language,
          max_mode: _maxMode,
          capability_level: _cap,
          reasoning_effort: _reasoning,
        });
        return;
      }
      _ws.send({
        type: "submit_task",
        prompt,
        agent_id: agentId ?? undefined,
        session_id: sessionId,
        mode,
        language: i18next.language,
        max_mode: _maxMode,
        capability_level: _cap,
        reasoning_effort: _reasoning,
        attachments: attachments && attachments.length > 0 ? attachments : undefined,
      });
    },
    [],
  );

  const steerTask = useCallback((taskId: string, prompt: string) => {
    _ws.send({ type: "steer", task_id: taskId, prompt });
  }, []);

  const answerUserQuestion = useCallback((token: string, answer: string, cancelled = false) => {
    _ws.send({ type: "user_question_response", token, answer, cancelled });
  }, []);

  const confirmTool = useCallback(
    (confirmToken: string, confirmed: boolean, port: number, token: string, remember: boolean = false, toolName: string = "") => {
      if (confirmed) {
        return fetch(`http://127.0.0.1:${port}/api/v1/tools/confirm`, {
          method: "POST",
          headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
          body: JSON.stringify({ confirm_token: confirmToken, remember, tool_name: toolName }),
        }).then(readJsonResponse);
      }
      // P0 修复: 拒绝必须通知后端——原实现直接 return，后端 confirm Future
      // 悬挂至 1800s 超时，任务假死 30 分钟。走已有 /approvals/{token}/reject
      // (resolve_rejected 会 resolve_confirmation 唤醒 agent 协程)。
      return fetch(`http://127.0.0.1:${port}/api/v1/approvals/${encodeURIComponent(confirmToken)}/reject`, {
        method: "POST",
        headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
        body: JSON.stringify({ reason: "user rejected" }),
      }).then(readJsonResponse);
    },
    [],
  );

  // Batch 3.2: 批量工具确认——一次性 approve/reject 多个 confirm_token
  const confirmToolBatch = useCallback(
    (tokens: string[], approved: boolean, port: number, token: string, remember: boolean = false) => {
      if (!tokens.length) return Promise.resolve({ status: "ok", results: [] });
      const endpoint = approved ? "approve" : "reject";
      return fetch(`http://127.0.0.1:${port}/api/v1/approvals/batch/${endpoint}`, {
        method: "POST",
        headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
        body: JSON.stringify({ tokens, remember }),
      }).then(readJsonResponse);
    },
    [],
  );

  const cancelTask = useCallback(
    async (taskId: string) => {
      useTaskStore.getState().setTaskCancelled(taskId);
      if (!port || !token) return;
      try {
        await fetch(`http://127.0.0.1:${port}/api/v1/tasks/${taskId}/cancel`, {
          method: "POST",
          headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
        });
      } catch {
        // no-op
      }
    },
    [port, token],
  );

  const disconnect = useCallback(() => {
    _ws.disconnect();
  }, []);

  // Phase 78: 发送目标澄清答案给服务端
  // - needMore=false → 服务端 refine_goal → 推 goal_confirmed
  // - needMore=true  → 服务端 generate_questions(previous_qa=answers) → 推 followup 请求
  const sendGoalClarificationResponse = useCallback(
    (
      taskId: string,
      answers: { question: string; answer: string }[],
      needMore: boolean = false,
    ) => {
      _ws.send({
        type: "goal_clarification_response",
        task_id: taskId,
        answers,
        need_more: needMore,
      });
    },
    [],
  );

  // Phase 78: 本地清除澄清状态（用户跳过 / needMore 触发新一轮前清旧）
  const clearClarification = useCallback((taskId: string) => {
    useTaskStore.getState().clearClarification(taskId);
  }, []);

  return { submitTask, steerTask, answerUserQuestion, confirmTool, confirmToolBatch, cancelTask, disconnect, sendGoalClarificationResponse, clearClarification };
}
