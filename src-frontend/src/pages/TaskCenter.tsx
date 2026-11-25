import { useEffect } from "react";
import { useSearchParams } from "react-router-dom";
import { useApi } from "../hooks/useApi";
import { useAppStore } from "../store/appStore";
import { useTaskStore } from "../store/taskStore";
import { switchSessionFromApi } from "../utils/sessionRestore";
import { ChatView } from "../components/chat/ChatView";

/**
 * TaskCenter — Phase 49-J: 已改为 ChatView 的薄包装。
 *
 * 保留 `/chat` 路由入口，仅额外处理 `?session_id=` 查询参数。
 * 所有聊天 UI 逻辑统一收敛到 ChatView，避免 ChatView / TaskCenter / UnifiedWorkspace
 * 三套实现并行维护。
 */
export function TaskCenter() {
  const [searchParams] = useSearchParams();
  const presetSessionId = searchParams.get("session_id") || undefined;
  const backendReady = useAppStore((s) => s.backendReady);
  const { api } = useApi();

  useEffect(() => {
    if (!backendReady || !presetSessionId) return;
    if (useTaskStore.getState().currentSessionId === presetSessionId) return;

    // Batch 4a F1: 竞态防护的恢复——与 SessionList/ChatView 切换共用同一序号 guard
    switchSessionFromApi(presetSessionId, api).catch((e) =>
      console.warn("TaskCenter: preset session restore failed:", e),
    );
  }, [backendReady, presetSessionId, api]);

  return <ChatView />;
}
