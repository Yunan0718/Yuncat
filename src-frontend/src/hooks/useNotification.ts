import { useEffect } from "react";
import { useTaskStore } from "../store/taskStore";
import { getNotifiedSet } from "./useWebSocket";
import i18next from "i18next";

/**
 * 系统通知 hook — 任务完成/失败时弹出桌面通知。
 *
 * 共享去重：与 useWebSocket.tryNotify 共用同一个 Set，
 * 同一 taskId 无论从 WS stream_done 还是 taskStore 变化触发，只通知一次。
 *
 * 防刷屏：仅窗口不可见时触发（可见时 UI 已有反馈）。
 */
export function useNotification() {
  // 桌面通知只需订阅一次 Zustand store；deps 为空是刻意的

  useEffect(() => {
    const _notified = getNotifiedSet();
    if (!("Notification" in window)) return;

    const unsubscribe = useTaskStore.subscribe((state) => {
      if (document.hidden !== true) return; // 窗口可见 → 不弹
      for (const task of Object.values(state.tasks)) {
        if (task.status !== "completed" && task.status !== "failed") continue;
        if (_notified.has(task.taskId)) continue; // 共享去重
        _notified.add(task.taskId);
        const title = task.status === "completed" ? i18next.t("notification.taskCompleted") : i18next.t("notification.taskFailed");
        const body = task.status === "completed" ? i18next.t("notification.agentDone", { agent: task.agentId }) : task.errorMessage || i18next.t("notification.unknownError");
        showNotification(title, body, `/chat?task=${task.taskId}`);
      }
    });

    return unsubscribe;
  }, []);
}

function showNotification(title: string, body: string, link?: string) {
  // 统一走 Electron 主进程系统通知（含 setAppUserModelId + 点击导航）；无 window.yuncat 时降级 HTML5
  if (window.yuncat?.showNotification) {
    window.yuncat.showNotification(title, body, link).catch(() => {
      // 回退：主进程通知失败时用浏览器通知兜底——否则通知会静默消失，
      // 用户只看到"任务完成了但没弹提示"。
      showBrowserNotification(title, body);
    });
    return;
  }
  showBrowserNotification(title, body);
}

function showBrowserNotification(title: string, body: string) {
  if (Notification.permission === "granted") {
    new Notification(title, { body });
  } else if (Notification.permission === "default") {
    Notification.requestPermission().then((perm) => {
      if (perm === "granted") {
        new Notification(title, { body });
      }
    });
  }
}
