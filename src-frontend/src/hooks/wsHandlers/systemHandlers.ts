import i18next from "i18next";
import { useAppStore, type AppNotification, type Diagnostic } from "../../store/appStore";
import { useTaskStore } from "../../store/taskStore";
import { emitGlobalToast } from "../../components/common/toast-context";

import type { Handler } from "./taskHandlers";

// auth_ok / pong / sync_state / resume_start / resume_end: 空 case → 不注册 handler

export const agentNotification: Handler = (msg) => {
  // Phase 106: 兼容旧格式，统一映射到 AppNotification
  useAppStore.getState().addNotification?.({
    id: msg.id || `legacy-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
    type: "agent_notification",
    title: msg.agent_id || "system",
    body: msg.message || "",
    link: "",
    level: (msg.level || "info") as AppNotification["level"],
    read: false,
    created_at: msg.at || Date.now(),
    agent_id: msg.agent_id || "system",
  });
};

export const notification: Handler = (msg) => {
  // Phase 106: 统一通知系统——后端 NotificationCenter 推送
  const notif = {
    id: msg.id || `ws-${Date.now()}`,
    type: msg.notification_type || msg.type || "info",
    title: msg.title || "",
    body: msg.body || "",
    link: msg.link || "",
    level: (msg.level || "info") as AppNotification["level"],
    read: !!msg.read,
    created_at: msg.created_at || Date.now(),
    task_id: msg.task_id,
    agent_id: msg.agent_id,
  };
  useAppStore.getState().addNotification?.(notif);
};

export const agentStatus: Handler = (msg) => {
  useAppStore.getState().setAgentStatus(msg.agent_id, msg.status === "ready" || msg.status === "busy");
  // 2026-09-25: 配置不可用（如 API Key 解不开）不是「进程挂了」，重启修不好。
  // 后端只报一次并附带可执行原因，这里直接转成提示，别再让用户对着空白回复框猜。
  if (msg.status === "config_error") {
    const agent = (msg.agent_id as string) || "yuncat";
    const reason = (msg.reason as string) || "";
    emitGlobalToast(i18next.t("task.agentConfigError", { agent, reason }), "error");
  }
};

export const adapterDegraded: Handler = (msg) => {
  // P0-1.11: adapter 重启上限耗尽——toast + 更新 appStore.adapterHealth
  const agentId = (msg.agent_id as string) || "unknown";
  useAppStore.getState().setAdapterHealth(agentId, "degraded");
  emitGlobalToast(i18next.t("task.adapterDegraded", { agent: agentId }), "error");
};

export const circuitBreakerTripped: Handler = (msg, store) => {
  store.setContextUsage(99, 0, 131072);
};

export const thinkingChunk: Handler = (msg) => {
  useTaskStore.getState().appendThinking(msg.task_id, msg.data);
};

export const contextEmergency: Handler = () => {
  useTaskStore.getState().setContextUsage(99.99, 0, useTaskStore.getState().contextUsage.window);
};

export const preCompactWarning: Handler = (msg) => {
  useTaskStore
    .getState()
    .setContextUsage(Number(msg.usage_pct) || 95, 0, useTaskStore.getState().contextUsage.window);
};

export const contextMicroCompact: Handler = (msg) => {
  useTaskStore
    .getState()
    .setContextUsage(Number(msg.usage_pct) || 80, 0, useTaskStore.getState().contextUsage.window);
};

export const fileChanged: Handler = () => {
  useAppStore.getState().bumpFileWatch();
};

export const diagnostics: Handler = (msg) => {
  // E3: 后端 PostToolUse hook 推送 ruff/tsc 诊断 → store → MonacoEditor 渲染 inline marker
  if (typeof msg.file === "string" && msg.file) {
    // 路径键标准化为正斜杠——后端 Windows 下返回 "shared\foo.py"，前端 filePath 为 "shared/foo.py"
    const normFile = msg.file.replace(/\\/g, "/");
    useAppStore.getState().setDiagnostics(normFile, (msg.diagnostics || []) as Diagnostic[]);
  }
};

export const skillInstallComplete: Handler = (msg) => {
  // IMPROVEMENTS P2: 启动时系统 Skill 自动安装完成通知
  const count = Number(msg.count) || 0;
  if (count > 0) {
    emitGlobalToast(i18next.t("skillMarket.systemInstallComplete", { count }), "success");
  }
};

// Phase 49-F4: approval_error——审批处理失败（如 token 过期/状态冲突）。
// 与 tool_confirm_error 类似，HTTP 响应已处理 UI——此 WS 消息是异步错误通知。
export const approvalError: Handler = (msg) => {
  emitGlobalToast(
    i18next.t("task.approvalError", { error: msg.error ?? "" }),
    "error",
  );
};

/**
 * `#记住` 快捷指令的确认——后端写完长期记忆后推回来的。
 *
 * 之前前端没有这个 handler：记忆确实存进 DB 了，但界面毫无反馈，
 * 用户只觉得自己"发了条消息没人理"。
 */
export const memorySaved: Handler = (msg) => {
  emitGlobalToast(
    msg.message || i18next.t("chat.memorySaved", { defaultValue: "已记住" }),
    "success",
  );
};

/**
 * 服务端斜杠命令匹配（DAG 模板 / 工具路由）——告诉用户命令被谁接走了。
 * 没有它时，用户只看到任务跑起来，却不知道自己的斜杠命令走了哪条路。
 */
export const slashCommandMatched: Handler = (msg) => {
  const name = msg.name || msg.command || "";
  emitGlobalToast(
    i18next.t("chat.slashCommandMatched", { name, defaultValue: `已匹配斜杠命令 ${name}` }),
    "info",
  );
};
