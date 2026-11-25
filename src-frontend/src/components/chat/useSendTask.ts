import { useCallback, useEffect, useRef, useState } from "react";
import {
  parseReferences,
  stripReferences,
  resolveMemoryRefs,
  resolveFileRefs,
  resolveSessionRefs,
  resolveFolderRefs,
  resolveGitRefs,
  resolveWebRefs,
} from "../context-pills";
import { useAppStore } from "../../store/appStore";
import { useTaskStore, type TaskState } from "../../store/taskStore";
import type { SlashCommandDef } from "./InputArea";

type OutgoingAttachment = {
  name: string;
  type: string;
  data?: string;
  path?: string;
  bytes?: number;
  sha256?: string;
};

function attachmentBlob(att: OutgoingAttachment): Blob {
  const binary = atob(att.data || "");
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
  return new Blob([bytes], { type: att.type });
}

/**
 * P0-7: 发送任务 hook——从 ChatView.tsx 抽取（候选 2）。
 * 完整封装发送状态机：sending/pendingPrompt 状态 + 15s 超时 + task_created 重置 +
 * 编辑重试父链 + doSend 主流程。时序链必须作为整体搬迁，不可进一步拆分
 * （否则会引入 P0-7 修复前的"输入丢失"bug）。
 */
export interface UseSendTaskParams {
  input: string;
  setInput: React.Dispatch<React.SetStateAction<string>>;
  commands: SlashCommandDef[];
  handleLocalSlashCommand: (cmd: string, arg: string) => Promise<boolean>;
  submitTask: (
    prompt: string,
    agentId?: string,
    sessionId?: string,
    mode?: string,
    attachments?: OutgoingAttachment[],
    capabilityLevel?: number,
    reasoningEffort?: "none" | "minimal" | "low" | "medium" | "high" | "xhigh",
  ) => void;
  /** 运行中任务追加指令（steer）；未提供时退回新建任务 */
  steerTask?: (taskId: string, prompt: string) => void;
  selectedAgent: string;
  api: <T>(path: string, options?: RequestInit) => Promise<T>;
  mode: string;
  currentSessionId: string | null;
  setCurrentSession: (id: string) => void;
  editingTaskId: string | null;
  setEditingTaskId: (id: string | null) => void;
  setTaskSuperseded: (id: string) => void;
  store: {
    tasks: Record<string, TaskState>;
    setTaskParent: (taskId: string, parentId: string) => void;
    addTaskVersion: (parentId: string, version: { taskId: string; prompt: string; chunks: string[]; createdAt: number }) => void;
  };
  t: (key: string, opts?: Record<string, unknown>) => string;
  toast: (message: string, type?: "info" | "success" | "error") => void;
  wsPort: number | null;
  wsToken: string;
  setSlashMenu: (v: { visible: boolean; query: string; cursorPos: number }) => void;
  attachmentPreviews: Array<{ name: string; data: string; type: string }>;
  setAttachmentPreviews: React.Dispatch<React.SetStateAction<Array<{ name: string; data: string; type: string }>>>;
  setStatusMsg: (s: string) => void;
}

export function useSendTask(p: UseSendTaskParams) {
  // 解构而非直接用 `p.*`：调用方传的是内联对象字面量，`p` 每次渲染都是新引用。
  // 若照 exhaustive-deps 的原始提示把 `p` 整体列入依赖，依赖它的 effect/callback
  // 会在每次渲染重跑。解构后每个值是独立绑定，规则才能正确校验。
  const {
    api,
    attachmentPreviews,
    commands,
    currentSessionId,
    editingTaskId,
    handleLocalSlashCommand,
    input,
    mode,
    selectedAgent,
    setAttachmentPreviews,
    setCurrentSession,
    setEditingTaskId,
    setInput,
    setSlashMenu,
    setStatusMsg,
    setTaskSuperseded,
    store,
    submitTask,
    steerTask,
    t,
    toast,
    wsPort,
    wsToken,
  } = p;
  const [sending, setSending] = useState(false);
  const [pendingPrompt, setPendingPrompt] = useState("");
  const capabilityLevel = useAppStore((s) => s.capabilityLevel);
  const reasoningEffort = useAppStore((s) => s.reasoningEffort);
  // P0-7: 捕获原文——15s 超时分支用于回填输入框
  const lastSentInputRef = useRef<string>("");
  const pendingEditParents = useRef<Map<string, string>>(new Map());

  // Phase 49-C: 编辑重试产生的新任务链接到父任务
  useEffect(() => {
    const tasks = Object.values(store.tasks);
    for (const task of tasks) {
      const parentId = pendingEditParents.current.get(task.taskId);
      if (parentId && !task.parentTaskId) {
        store.setTaskParent(task.taskId, parentId);
        store.addTaskVersion(parentId, {
          taskId: task.taskId,
          prompt: task.prompt,
          chunks: [...task.chunks],
          createdAt: Date.now(),
        });
        pendingEditParents.current.delete(task.taskId);
      }
    }
  }, [store.tasks, store]);

  // Send reset: task_created 到达时清除 sending + pendingPrompt
  const prevCountRef = useRef(-1);
  // Batch 4a F5: 直接订阅 store（selector 返回 number，仅任务增减时变化）——
  // 原实现从 store.tasks 渲染期取值，依赖父组件恰好重渲才刷新（ChatView 移除
  // taskCount 订阅后该链断裂，task_created 永远无法重置 sending）
  const taskCount = useTaskStore((s) => Object.keys(s.tasks).length);
  useEffect(() => {
    if (prevCountRef.current >= 0 && taskCount > prevCountRef.current) {
      setSending(false);
      setPendingPrompt("");
      lastSentInputRef.current = "";
    }
    prevCountRef.current = taskCount;
  }, [taskCount]);

  // 安全超时——15s 后强制重置（防止 task_created 永远不到）
  // P0-7: 超时分支回填输入框（lastSentInputRef）+ 红色 toast 提示
  // P2-2: 超时前检查 taskCount 是否已增加——若已增加说明 task_created 在途，
  // 不恢复输入（避免用户重发导致重复任务），仅重置 sending 状态
  useEffect(() => {
    if (!sending) return;
    const countAtSend = prevCountRef.current;
    const sentAt = Date.now();
    const timer = setTimeout(async () => {
      // P2-2: 若 taskCount 已增加，说明后端已创建任务（task_created WS 消息可能丢失或延迟）
      // 此时不应恢复输入——避免用户重发导致重复任务
      if (Object.keys(store.tasks).length > countAtSend) {
        setSending(false);
        setPendingPrompt("");
        lastSentInputRef.current = "";
        return;
      }
      // dev 模式 WS 断连（Vite HMR）可能导致 task_created 丢失，但任务实际已在后端
      // 创建并运行。超时前通过 HTTP 对账：若发现发送窗口内新建的 running 任务，
      // 说明任务在跑，静默重置而非误报"发送超时"（避免用户重发产生重复任务）。
      try {
        const running = await api<Array<{ id: string; status: string; created_at: number }>>(
          "/api/v1/tasks?status=running&limit=10",
        );
        const windowStart = Math.floor(sentAt / 1000) - 30; // 容差 30s
        const found = running.some((t) => t.created_at >= windowStart);
        if (found) {
          setSending(false);
          setPendingPrompt("");
          lastSentInputRef.current = "";
          return;
        }
      } catch {
        // 对账失败——按原逻辑处理（可能真的没创建）
      }
      setSending(false);
      setPendingPrompt("");
      const lostInput = lastSentInputRef.current;
      if (lostInput) {
        setInput(lostInput);
        lastSentInputRef.current = "";
        toast(t("chat.sendTimeout"), "error");
      }
    }, 15000);
    return () => clearTimeout(timer);
    // Batch 4a F5: 依赖数组移除 store.tasks——原实现每chunk 变引用导致 timer 被
    // 无限重置，任务 B 的 task_created 丢失时 sending 永久卡死。回调内通过
    // useTaskStore.getState() 实时读取（stableStore getter），无需作为依赖。
  }, [sending, t, toast, setInput, store, api]);

  const doSend = useCallback(async (textOverride?: string) => {
    const text = (textOverride ?? input).trim();
    if (!text || sending) return;

    // Phase 64: 本地命令拦截
    if (text.startsWith("/")) {
      const firstToken = text.split(/\s+/)[0]!.toLowerCase();
      const cmdDef = commands.find((c) => c.slash_command.toLowerCase() === firstToken);
      if (cmdDef?.local) {
        const arg = text.slice(firstToken.length).trim();
        const handled = await handleLocalSlashCommand(firstToken, arg);
        if (handled) {
          setInput("");
          setSlashMenu({ visible: false, query: "", cursorPos: 0 });
          return;
        }
        return;
      }
    }

    setSending(true);
    setInput("");
    setPendingPrompt(text);
    lastSentInputRef.current = text;
    // 2.1 修复: 先读取附件到局部变量再清空，避免闭包丢失——原代码传 [] 导致附件永不到达后端
    const attachmentsToSend = attachmentPreviews;
    setAttachmentPreviews([]);
    const refs = parseReferences(text);
    const effAgent = refs.find((r) => r.type === "agent")?.value || selectedAgent || undefined;
    const [mc, fc, sc, folc, wc, gc] = await Promise.all([
      resolveMemoryRefs(refs, api, t).catch((e) => {
        console.warn("resolveMemoryRefs failed", e);
        toast(t("phase49.contextPills.resolveFail"), "error");
        return "";
      }),
      resolveFileRefs(refs, api, t).catch((e) => {
        console.warn("resolveFileRefs failed", e);
        toast(t("phase49.contextPills.resolveFail"), "error");
        return "";
      }),
      resolveSessionRefs(refs, api, t).catch((e) => {
        console.warn("resolveSessionRefs failed", e);
        toast(t("phase49.contextPills.resolveFail"), "error");
        return "";
      }),
      resolveFolderRefs(refs, api, t).catch((e) => {
        console.warn("resolveFolderRefs failed", e);
        toast(t("phase49.contextPills.resolveFail"), "error");
        return "";
      }),
      resolveWebRefs(refs, api, t).catch((e) => {
        console.warn("resolveWebRefs failed", e);
        toast(t("phase49.contextPills.resolveFail"), "error");
        return "";
      }),
      resolveGitRefs(refs, api, t).catch((e) => {
        console.warn("resolveGitRefs failed", e);
        toast(t("phase49.contextPills.resolveFail"), "error");
        return "";
      }),
    ]);
    let fp = [mc, fc, sc, folc, wc, gc, stripReferences(text, refs)].filter(Boolean).join("\n\n---\n");
    const ec = useAppStore.getState().editorContext;
    if (ec?.activeFile) {
      const fileWithCursor = ec.cursorLine ? `${ec.activeFile}:${ec.cursorLine}L` : ec.activeFile;
      fp = `${t("chat.editorPrefix", { file: fileWithCursor })}\n\n` + fp;
    }
    // 当前查看的任务仍在运行时，把输入作为 steer 追加到下一步。
    // 显式“编辑并重试”优先，避免用户意图被运行中的其他任务截走。
    const activeId = useTaskStore.getState().activeTaskId;
    const activeTask = activeId ? store.tasks[activeId] : undefined;
    if (!editingTaskId && activeTask?.status === "running" && steerTask && attachmentsToSend.length === 0) {
      steerTask(activeTask.taskId, fp);
      setSending(false);
      setPendingPrompt("");
      toast(t("chat.steerQueued"), "info");
      return;
    }
    if (editingTaskId) {
      const parentTask = store.tasks[editingTaskId];
      const convId = parentTask?.convId;
      if (convId) {
        setTaskSuperseded(editingTaskId);
        setEditingTaskId(null);
        try {
          const r = await api<{ conv_id: string; task_id?: string }>(`/api/v1/conversations/${convId}/edit`, {
            method: "POST",
            body: JSON.stringify({ content: fp, task_id: editingTaskId }),
          });
          if (r?.task_id) {
            pendingEditParents.current.set(r.task_id, editingTaskId);
          }
          return;
        } catch {
          // no-op: fall through to normal submit
        }
      }
    }
    if (mode === "long_task" && attachmentsToSend.length > 0) {
      setSending(false);
      setPendingPrompt("");
      setInput(text);
      setAttachmentPreviews(attachmentsToSend);
      toast(t("chat.longTaskAttachmentsUnsupported"), "error");
      return;
    }
    let sid = currentSessionId;
    if (!sid) {
      try {
        const r = await api<{ session_id: string }>("/api/v1/sessions/new", { method: "POST" });
        if (r?.session_id) {
          setCurrentSession(r.session_id);
          sid = r.session_id;
        }
      } catch {
        // no-op
      }
    }
    try {
      let outboundAttachments: OutgoingAttachment[] = attachmentsToSend;
      if (attachmentsToSend.some((att) => att.type.startsWith("image/"))) {
        outboundAttachments = await Promise.all(attachmentsToSend.map(async (att) => {
          if (!att.type.startsWith("image/")) return att;
          const query = new URLSearchParams({ session_id: sid || "unscoped", filename: att.name });
          const uploaded = await api<OutgoingAttachment>(
            `/api/v1/attachments?${query.toString()}`,
            { method: "POST", headers: { "Content-Type": att.type }, body: attachmentBlob(att) },
          );
          return { name: uploaded.name, type: uploaded.type, path: uploaded.path, bytes: uploaded.bytes, sha256: uploaded.sha256 };
        }));
      }
      submitTask(fp, effAgent, sid || undefined, mode, outboundAttachments, capabilityLevel, reasoningEffort);
      // P3-1: WS 断开时 submitTask 消息进入 outbox——给用户即时反馈
      // 避免 "sending" 状态下用户不知道消息实际被排队
      if (useAppStore.getState().outboxCount > 0) {
        toast(t("chat.sendQueued"), "info");
      }
    } catch (err) {
      setSending(false);
      setPendingPrompt("");
      setInput(text);
      setAttachmentPreviews(attachmentsToSend);
      toast(typeof err === "string" ? err : t("chat.sendFailed"), "error");
    }
  }, [
    input,
    sending,
    submitTask,
    steerTask,
    selectedAgent,
    api,
    mode,
    currentSessionId,
    editingTaskId,
    setCurrentSession,
    setTaskSuperseded,
    store.tasks,
    t,
    toast,
    commands,
    handleLocalSlashCommand,
    setInput,
    setSlashMenu,
    attachmentPreviews,
    setAttachmentPreviews,
    setEditingTaskId,
    capabilityLevel,
    reasoningEffort,
  ]);

  const handleCompact = async () => {
    if (!wsPort || !wsToken || !currentSessionId) return;
    try {
      const r = await fetch(`http://127.0.0.1:${wsPort}/api/v1/sessions/${currentSessionId}/compact`, {
        method: "POST",
        headers: { Authorization: `Bearer ${wsToken}`, "Content-Type": "application/json" },
      });
      const d = await r.json();
      setStatusMsg(d.ok ? t("chat.compactOk") : d.error || t("chat.compactFailed"));
    } catch {
      setStatusMsg(t("chat.compactRequestFailed"));
    }
  };

  return { sending, pendingPrompt, doSend, handleCompact };
}
