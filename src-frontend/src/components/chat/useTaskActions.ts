import { useMemo } from "react";
import { useAppStore } from "../../store/appStore";
import type { TaskActions } from "./MessageItem";
import type { TaskState } from "../../store/taskStore";
import type { VersionNode, VersionEdge } from "../MessageVersionTree";
import type { CtxMenuState } from "./types";

/**
 * P0-7: taskActions 对象工厂 hook——从 ChatView.tsx 抽取（候选 1）。
 * 16 个回调方法天然分组，是文件中最大的可独立单元。
 * 用 useMemo 避免每次 render 创建新对象导致 MessageList 不必要重渲染。
 */
export interface UseTaskActionsParams {
  setCtxMenu: (m: CtxMenuState | null) => void;
  setEditingTaskId: (id: string | null) => void;
  setInput: React.Dispatch<React.SetStateAction<string>>;
  setTaskSuperseded: (id: string) => void;
  submitTask: (
    prompt: string,
    agentId?: string,
    sessionId?: string,
    mode?: string,
    attachments?: Array<{ name: string; type: string; data?: string; path?: string; bytes?: number; sha256?: string }>,
    capabilityLevel?: number,
    reasoningEffort?: "none" | "minimal" | "low" | "medium" | "high" | "xhigh",
  ) => void;
  cancelTask: (taskId: string) => void;
  currentSessionId: string | null;
  setCurrentSession: (id: string) => void;
  api: <T>(path: string, options?: RequestInit) => Promise<T>;
  toast: (message: string, type?: "info" | "success" | "error") => void;
  t: (key: string, opts?: Record<string, unknown>) => string;
  store: {
    tasks: Record<string, TaskState>;
    switchVersion: (parentTaskId: string, versionTaskId: string) => void;
  };
  openFile: (path: string, name: string) => void;
  setVersionTrees: React.Dispatch<
    React.SetStateAction<Record<string, { nodes: VersionNode[]; edges: VersionEdge[] }>>
  >;
  wsPort: number | null;
  wsToken: string;
  setStatusMsg: (s: string) => void;
}

export function useTaskActions(p: UseTaskActionsParams): TaskActions {
  // 解构而非直接用 `p.*`：调用方传的是内联对象字面量，`p` 每次渲染都是新引用。
  // 若照 exhaustive-deps 的原始提示把 `p` 整体列入依赖，依赖它的 effect/callback
  // 会在每次渲染重跑。解构后每个值是独立绑定，规则才能正确校验。
  const {
    api,
    cancelTask,
    currentSessionId,
    openFile,
    setCtxMenu,
    setCurrentSession,
    setEditingTaskId,
    setInput,
    setStatusMsg,
    setTaskSuperseded,
    setVersionTrees,
    store,
    submitTask,
    t,
    toast,
    wsPort,
    wsToken,
  } = p;
  const capabilityLevel = useAppStore((s) => s.capabilityLevel);
  const reasoningEffort = useAppStore((s) => s.reasoningEffort);
  return useMemo<TaskActions>(
    () => ({
      onContextMenu: (e, task) => {
        e.preventDefault();
        setCtxMenu({
          x: Math.min(e.clientX, window.innerWidth - 190),
          y: Math.min(e.clientY, window.innerHeight - 120),
          taskId: task.taskId,
          prompt: task.prompt,
          seq: task.msgSeq,
        });
      },
      onEditPrompt: (taskId, prompt) => {
        setEditingTaskId(taskId);
        setInput(prompt);
      },
      onRegenerate: (task) => {
        setTaskSuperseded(task.taskId);
        submitTask(task.prompt, task.agentId, currentSessionId || undefined, "agent", undefined, capabilityLevel, reasoningEffort);
      },
      onFork: async (taskId) => {
        if (!currentSessionId) return;
        try {
          const r = await api<{ new_session_id: string }>(
            `/api/v1/conversations/${currentSessionId}/fork?from_message_id=${encodeURIComponent(taskId)}`,
            { method: "POST" },
          );
          if (r?.new_session_id) {
            setCurrentSession(r.new_session_id);
            toast(t("chat.forkSessionCreated", { id: r.new_session_id.slice(0, 8) }), "success");
          }
        } catch {
          toast(t("chat.forkFailed"), "error");
        }
      },
      onSaveAsTemplate: (taskId) => {
        api<{ ok: boolean; workflow_id?: string; error?: string }>(
          `/api/v1/tasks/${taskId}/save-as-workflow`,
          { method: "POST" },
        )
          .then((r) => {
            if (r.ok) setStatusMsg(t("chat.savedAsTemplate", { id: r.workflow_id }));
            else setStatusMsg(r.error || t("common.saveFailed"));
          })
          .catch(() => setStatusMsg(t("common.saveFailed")));
      },
      onUndo: async (taskId) => {
        try {
          const r = await api<{
            ok: boolean;
            status?: string;
            restored_files?: number;
            removed_new_files?: number;
            skipped?: string[];
            error?: string;
          }>(
            `/api/v1/tasks/${taskId}/undo`,
            { method: "POST" },
          );
          if (r && r.ok) {
            // 部分撤销必须说出来：恢复了几个、删掉几个新建文件、几个没恢复
            let msg = t("chat.undoDone", { count: r.restored_files || 0 });
            if (r.removed_new_files) {
              msg += t("chat.undoRemovedNew", { count: r.removed_new_files });
            }
            if (r.skipped?.length) {
              msg += t("chat.undoSkipped", { count: r.skipped.length });
            }
            toast(msg, r.skipped?.length ? "info" : "success");
          } else toast(r?.error || t("chat.undoFailed"), "error");
        } catch {
          toast(t("chat.undoFailed"), "error");
        }
      },
      onRate: async (taskId, rating) => {
        try {
          await api(`/api/v1/conversations/${taskId}/rate`, {
            method: "POST",
            body: JSON.stringify({ rating }),
          });
        } catch {
          // no-op
        }
      },
      onShare: async () => {
        if (!currentSessionId) return;
        try {
          const r = await api<{ markdown?: string }>(`/api/v1/sessions/${currentSessionId}/export`);
          if (r?.markdown) {
            const blob = new Blob([r.markdown], { type: "text/markdown" });
            const url = URL.createObjectURL(blob);
            const a = document.createElement("a");
            a.href = url;
            a.download = `session_${currentSessionId.slice(0, 8)}.md`;
            a.click();
            URL.revokeObjectURL(url);
          }
        } catch {
          toast(t("common.saveFailed"), "error");
        }
      },
      onPause: async (taskId) => {
        if (!wsPort || !wsToken) return;
        try {
          await fetch(`http://127.0.0.1:${wsPort}/api/v1/tasks/${taskId}/pause`, {
            method: "POST",
            headers: { Authorization: `Bearer ${wsToken}` },
          });
        } catch {
          // no-op
        }
      },
      onForcePause: async (taskId) => {
        if (!wsPort || !wsToken) return;
        try {
          await fetch(`http://127.0.0.1:${wsPort}/api/v1/tasks/${taskId}/pause?force=true`, {
            method: "POST",
            headers: { Authorization: `Bearer ${wsToken}` },
          });
        } catch {
          // no-op
        }
      },
      onResume: async (taskId) => {
        if (!wsPort || !wsToken) return;
        try {
          await fetch(`http://127.0.0.1:${wsPort}/api/v1/tasks/${taskId}/resume`, {
            method: "POST",
            headers: { Authorization: `Bearer ${wsToken}` },
          });
        } catch {
          // no-op
        }
      },
      onStop: (taskId) => cancelTask(taskId),
      onRetry: (task) => {
        const retryPrompt = task.errorMessage
          ? `${t("chat.lastFailed", { error: task.errorMessage })}\n\n${task.prompt}`
          : task.prompt;
        submitTask(retryPrompt, task.agentId, currentSessionId || undefined, "agent", undefined, capabilityLevel, reasoningEffort);
      },
      onOpenFile: async (path, newContent?) => {
        // 2.14 修复: onApplyDiff 完整传递 newContent——先写入文件再打开
        // 对标 Cursor/Claude Code Accept diff 直接写入工作区文件
        if (newContent !== undefined) {
          try {
            await api("/api/v1/workspace/files/save", {
              method: "POST",
              body: JSON.stringify({ path, content: newContent }),
            });
          } catch {
            // 写入失败仍打开文件（用户可手动粘贴）
          }
        }
        openFile(path, path.split(/[/\\]/).pop() || path);
      },
      onExecutePlan: async (taskId) => {
        try {
          await api(`/api/v1/tasks/${taskId}/execute-plan`, { method: "POST" });
        } catch {
          // no-op
        }
      },
      onShowVersionTree: async (taskId) => {
        const task = store.tasks[taskId];
        const convId = task?.convId;
        if (!convId) return;
        try {
          const r = await api<{
            root_id: string;
            nodes: Array<Record<string, unknown>>;
            edges: Array<Record<string, unknown>>;
          }>(`/api/v1/conversations/${convId}/versions`);
          const nodes: VersionNode[] = (r?.nodes || []).map((n) => ({
            id: String(n.id),
            label: n.version_index != null ? `V${n.version_index}` : String(n.id).slice(0, 8),
            prompt: typeof n.content === "string" ? n.content : undefined,
            role: typeof n.role === "string" ? n.role : undefined,
            createdAt: typeof n.created_at === "number" ? n.created_at * 1000 : undefined,
          }));
          const edges: VersionEdge[] = (r?.edges || []).map((e) => ({
            source: String(e.from),
            target: String(e.to),
          }));
          setVersionTrees((prev) => ({ ...prev, [taskId]: { nodes, edges } }));
        } catch {
          // no-op
        }
      },
      onSwitchVersion: (parentTaskId, versionTaskId) => {
        store.switchVersion(parentTaskId, versionTaskId);
      },
    }),
    // 依赖列表——所有外部状态/回调变化时重建
    [
      setCtxMenu,
      setEditingTaskId,
      setInput,
      setTaskSuperseded,
      submitTask,
      cancelTask,
      currentSessionId,
      setCurrentSession,
      api,
      toast,
      t,
      store,
      openFile,
      setVersionTrees,
      wsPort,
      wsToken,
      setStatusMsg,
      capabilityLevel,
      reasoningEffort,
    ],
  );
}
