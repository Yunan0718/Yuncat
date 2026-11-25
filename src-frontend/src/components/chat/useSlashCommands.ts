import { useCallback, useEffect, useState } from "react";
import type { SlashMenuState, SlashCommandDef } from "./InputArea";
import { useTaskStore } from "../../store/taskStore";

/**
 * P0-7: 斜杠命令 hook——从 ChatView.tsx 抽取（候选 7）。
 * 包含 commands 加载 + 本地命令拦截（/clear /compact /model /agent /help）。
 */
export interface UseSlashCommandsParams {
  api: <T>(path: string, options?: RequestInit) => Promise<T>;
  backendReady: boolean;
  agents: Array<{ id: string; name: string }>;
  currentSessionId: string | null;
  setSelectedAgent: (id: string) => void;
  setShowSearch: (v: boolean | ((p: boolean) => boolean)) => void;
  toast: (message: string, type?: "info" | "success" | "error") => void;
  t: (key: string, opts?: Record<string, unknown>) => string;
  wsPort: number | null;
  wsToken: string;
}

export function useSlashCommands(p: UseSlashCommandsParams) {
  // 解构而非直接用 `p.*`：调用方传的是内联对象字面量，`p` 每次渲染都是新引用。
  // 若把 `p` 整体放进依赖数组，下面的 commands 拉取会每次渲染重发请求。
  // 解构后每个值都是独立绑定，exhaustive-deps 才能正确校验。
  const {
    api,
    backendReady,
    agents,
    currentSessionId,
    setSelectedAgent,
    setShowSearch,
    toast,
    t,
    wsPort,
    wsToken,
  } = p;

  const [slashMenu, setSlashMenu] = useState<SlashMenuState>({ visible: false, query: "", cursorPos: 0 });
  const [commands, setCommands] = useState<SlashCommandDef[]>([]);

  // Phase 64: 加载 / 命令列表（含 local 标记）
  useEffect(() => {
    if (!backendReady) return;
    api<SlashCommandDef[]>("/api/v1/commands")
      .then(setCommands)
      .catch((e) => console.warn("useSlashCommands: commands load failed:", e));
  }, [backendReady, api]);

  const handleLocalSlashCommand = useCallback(
    async (cmd: string, arg: string): Promise<boolean> => {
      switch (cmd) {
        case "/clear": {
          try {
            const r = await api<{ session_id: string }>("/api/v1/sessions/new", { method: "POST" });
            useTaskStore.getState().clearTasks();
            useTaskStore.getState().setCurrentSession(r.session_id);
            useTaskStore.getState().bumpSessionList();
            toast(t("phase64.clearDone"), "success");
          } catch {
            toast(t("common.saveFailed"), "error");
          }
          return true;
        }
        case "/compact": {
          if (!wsPort || !wsToken || !currentSessionId) {
            toast(t("chat.compactRequestFailed"), "error");
            return true;
          }
          try {
            const r = await fetch(`http://127.0.0.1:${wsPort}/api/v1/sessions/${currentSessionId}/compact`, {
              method: "POST",
              headers: { Authorization: `Bearer ${wsToken}`, "Content-Type": "application/json" },
            });
            const d = await r.json();
            toast(d.ok ? t("chat.compactOk") : d.error || t("chat.compactFailed"), d.ok ? "success" : "error");
          } catch {
            toast(t("chat.compactRequestFailed"), "error");
          }
          return true;
        }
        case "/model": {
          setShowSearch(true);
          toast(t("phase64.modelPanelOpened"), "info");
          return true;
        }
        case "/agent": {
          if (!arg) {
            const list = agents.map((a) => `${a.id} (${a.name})`).join(" / ");
            toast(t("phase64.agentUsage", { list }), "info");
            return false;
          }
          const found = agents.find((a) => a.id === arg || a.id.toLowerCase() === arg.toLowerCase());
          if (!found) {
            toast(t("phase64.agentNotFound", { id: arg }), "error");
            return false;
          }
          setSelectedAgent(found.id);
          toast(t("phase64.agentSwitched", { id: found.id, name: found.name }), "success");
          return true;
        }
        case "/help": {
          const lines = commands
            .filter((c) => !c.slash_command.startsWith("/help"))
            .map((c) => `${c.slash_command}  —  ${c.description}`);
          toast(t("phase64.helpTitle") + "\n" + lines.join("\n"), "info");
          return true;
        }
        default:
          return false;
      }
    },
    [api, agents, commands, currentSessionId, setSelectedAgent, setShowSearch, toast, t, wsPort, wsToken],
  );

  return { slashMenu, setSlashMenu, commands, handleLocalSlashCommand };
}
