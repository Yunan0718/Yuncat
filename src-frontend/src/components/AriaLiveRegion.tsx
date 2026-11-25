import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { useAppStore } from "../store/appStore";
import { useTaskStore } from "../store/taskStore";

/**
 * Phase 49-B: 可访问性实时播报区域
 *
 * 监听后端状态、任务暂停/恢复、任务失败等变化，通过 aria-live="polite"
 * 向屏幕阅读器播报，避免视觉信息无法被辅助技术感知。
 */
export function AriaLiveRegion() {
  const { t } = useTranslation();
  const [message, setMessage] = useState("");

  useEffect(() => {
    const unsubAgent = useAppStore.subscribe((state, prev) => {
      const next = state.agentStatus;
      const prevStatus = prev.agentStatus;
      const changed = Object.entries(next).find(([id, ready]) => prevStatus[id] !== ready);
      if (changed) {
        const [id, ready] = changed;
        setMessage(`${id} ${ready ? t("agents.running") : t("agents.stopped")}`);
      }
    });

    const unsubTasks = useTaskStore.subscribe((state, prev) => {
      const next = state.tasks;
      const prevTasks = prev.tasks;
      for (const [taskId, task] of Object.entries(next)) {
        const prevTask = prevTasks[taskId];
        if (!prevTask) continue;

        if (prevTask.status !== "paused" && task.status === "paused") {
          setMessage(`${t("chat.pause")}: ${task.prompt.slice(0, 60)}`);
        }
        if (prevTask.status === "paused" && task.status === "running") {
          setMessage(`${t("chat.resume")}: ${task.prompt.slice(0, 60)}`);
        }
        if (!prevTask.errorMessage && task.errorMessage) {
          setMessage(`${t("chat.failed")}: ${task.errorMessage}`);
        }
      }
    });

    return () => {
      unsubAgent();
      unsubTasks();
    };
  }, [t]);

  return (
    <div aria-live="polite" aria-atomic="true" className="sr-only">
      {message}
    </div>
  );
}
