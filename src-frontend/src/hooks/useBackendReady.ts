import { useEffect, useRef } from "react";
import { useAppStore } from "../store/appStore";

/**
 * Backend readiness hook — Electron preload bridge.
 *
 * 通过 window.yuncat IPC 获取后端状态（替代 Tauri invoke/listen）。
 *
 * Pattern:
 *   1. Immediately query via getBackendInfo IPC → <1ms if backend already up.
 *   2. Event listener as accelerator → instant when backend starts.
 *   3. Fast poll (200ms) as safety net.
 */
declare global {
  interface Window {
    /** Batch 4a F7: 可选——纯浏览器环境无此桥（全仓调用均为 window.yuncat?. 形式） */
    yuncat?: {
      getBackendInfo: () => Promise<[number, string]>;
      setTitleBarTheme: (theme: string) => Promise<void>;
      restartBackend: () => Promise<{ ok: boolean; port?: number; error?: string }>;
      /** 多窗口：新开一个窗口（主进程决定是否允许） */
      newWindow: () => Promise<{ ok: boolean }>;
      /** 内置浏览器：打开/聚焦浏览器窗口（AI 与用户共用同一批标签页） */
      openBrowser: (url?: string) => Promise<{ ok: boolean; error?: string; state?: unknown }>;
      browserState: () => Promise<unknown>;
      showNotification: (title: string, body: string, link?: string) => Promise<{ ok: boolean; error?: string }>;
      openPath: (path: string) => Promise<{ ok: boolean; error?: string }>;
      showItemInFolder: (path: string) => Promise<{ ok: boolean; error?: string }>;
      openFileDialog: (options?: {
        selectFolder?: boolean;
        title?: string;
        multiSelections?: boolean;
        /** AUDIT #3b: 文件类型过滤器——对齐 main.js open_file_dialog 已支持的 options.filters
         * 格式同 Electron FileFilter: { name: string; extensions: string[] }[] */
        filters?: { name: string; extensions: string[] }[];
      }) => Promise<{ ok: boolean; canceled?: boolean; path?: string | string[]; error?: string }>;
      checkForUpdates: () => Promise<{ ok: boolean; version?: string; error?: string }>;
      downloadUpdate: () => Promise<{ ok: boolean; error?: string }>;
      installUpdate: () => Promise<{ ok: boolean }>;
      onBackendReady: (cb: (data: [number, string]) => void) => () => void;
      onBackendError: (cb: (data: string) => void) => () => void;
      onBackendStarting: (cb: (data: string) => void) => () => void;
      onTrayAction: (cb: (data: string) => void) => () => void;
      onUpdateAvailable: (cb: (data: unknown) => void) => () => void;
      onUpdateDownloaded: (cb: (data: unknown) => void) => () => void;
      /** Phase 94: 更新错误（含校验失败标志） */
      onUpdateError: (cb: (data: { message: string; isVerificationError: boolean }) => void) => () => void;
      /** Phase 94: 检查更新后无新版本 */
      onUpdateNotAvailable: (cb: (data: unknown) => void) => () => void;
      /** Phase 94: 下载进度（percent 0-100） */
      onDownloadProgress: (cb: (data: { percent: number }) => void) => () => void;
      /** Phase 92: 崩溃报告——查询最近未处理的 crash（已 dismissed 或无 crash 返回 null） */
      getLastCrash: () => Promise<{ ok: boolean; crash?: { timestamp: number; time: string; file: string; path: string; size: number } | null; error?: string }>;
      /** Phase 92: 打开 crash-reports 目录 */
      openCrashDir: () => Promise<{ ok: boolean; error?: string }>;
      /** Phase 92: 标记当前 crash 已处理（下次启动不弹窗） */
      dismissCrash: () => Promise<{ ok: boolean; error?: string }>;
      /** Phase 92: 重启应用（app.relaunch + app.exit） */
      relaunchApp: () => Promise<{ ok: boolean; error?: string }>;
      /** Phase 95: 查询开机自启状态（读取系统登录项） */
      getAutostart: () => Promise<{ ok: boolean; enabled?: boolean; error?: string }>;
      /** Phase 95: 设置开机自启（写入系统登录项，同步托盘勾选变化） */
      setAutostart: (enabled: boolean) => Promise<{ ok: boolean; enabled?: boolean; error?: string }>;
      /** Phase 95: 托盘勾选变化事件（用户在托盘切换开机自启时通知前端） */
      onAutostartChanged: (cb: (enabled: boolean) => void) => () => void;
      /** Phase 96: 查询 Electron 层用户偏好（如 minimizeToTray） */
      getPreference: (key: string) => Promise<{ ok: boolean; key?: string; value?: unknown; error?: string }>;
      /** Phase 96: 设置 Electron 层用户偏好 */
      setPreference: (key: string, value: unknown) => Promise<{ ok: boolean; key?: string; value?: unknown; error?: string }>;
      /** Phase 96: 用户偏好变化事件（主进程设置后通知前端同步） */
      onPreferenceChanged: (cb: (evt: { key: string; value: unknown }) => void) => () => void;
      /** Batch 7.6: 触发主进程刷新 access token */
      refreshAccessToken: () => Promise<{ ok: boolean; token?: string; error?: string }>;
      /** Batch 4a F7: 全局快捷键 Ctrl+Shift+N 新建会话（main.js registerGlobalShortcuts → 'new-chat'） */
      onNewChat: (cb: () => void) => () => void;
      /** Batch 4a F7: yuncat:// 深度链接（main.js parseDeepLink → { prompt, agent }） */
      onDeepLink: (cb: (data: { prompt: string; agent: string }) => void) => () => void;
      /** 系统通知点击（main.js show_notification → 'notification-click' → link） */
      onNotificationClick: (cb: (link: string) => void) => () => void;
    };
  }
}

const POLL_MS = 200;
const TIMEOUT_MS = 30_000;
const MAX_POLL_TIME_MS = 300_000; // Phase 49: 5 分钟绝对上限——torch/chromadb 首次冷启动 + 低配置机器需要更久

export function useBackendReady() {
  const setPort = useAppStore((s) => s.setPort);
  const setToken = useAppStore((s) => s.setToken);
  const setBackendReady = useAppStore((s) => s.setBackendReady);
  const cancelled = useRef(false);
  const deadTimerRef = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const startTimeRef = useRef(Date.now());

  // 后端就绪轮询与 IPC 监听，只在挂载时运行一次；store setters 来自 Zustand selector，引用稳定

  useEffect(() => {
    // 非 Electron 环境（纯浏览器）——尝试 dev-only 后端发现，否则跳过
    // 用途：Vite 独立 dev server（无 Electron 桥）时，通过 Vite env 注入的
    // 端口 + token 让前端在浏览器预览中完成初始化（对标 Electron 的 window.yuncat 桥）。
    if (!window.yuncat) {
      if (import.meta.env.DEV) {
        const devPort = Number((import.meta.env as any).VITE_BACKEND_PORT);
        const devToken = (import.meta.env as any).VITE_BACKEND_TOKEN as string | undefined;
        if (devPort > 0 && devToken) {
          let cancelled = false;
          const tryConnect = async () => {
            if (cancelled || useAppStore.getState().backendReady) return;
            try {
              const res = await fetch(`http://127.0.0.1:${devPort}/api/v1/health`, {
                headers: { Authorization: `Bearer ${devToken}` },
              });
              if (res.ok) {
                const store = useAppStore.getState();
                store.setPort(devPort);
                store.setToken(devToken);
                store.setBackendReady(true);
              }
            } catch {
              /* backend not up yet */
            }
          };
          tryConnect();
          const iv = setInterval(tryConnect, 1000);
          const to = setTimeout(() => clearInterval(iv), 300_000);
          return () => {
            cancelled = true;
            clearInterval(iv);
            clearTimeout(to);
          };
        }
      }
      return;
    }

    startTimeRef.current = Date.now();

    const tryConnect = async () => {
      if (cancelled.current || useAppStore.getState().backendReady) return true;
      // 绝对超时保护——避免 onBackendStarting 无限重置 deadline
      if (Date.now() - startTimeRef.current > MAX_POLL_TIME_MS) {
        if (!useAppStore.getState().backendReady) {
          useAppStore.getState().setBackendDead(true);
          done();
        }
        return false;
      }
      try {
        // Batch 4a F7: yuncat 改为可选类型——闭包内 narrowing 失效，需重新判空
        const bridge = window.yuncat;
        if (!bridge) return false;
        const [port, token] = await bridge.getBackendInfo();
        if (port > 0 && token) {
          done();
          setPort(port);
          setToken(token);
          setBackendReady(true);
          return true;
        }
      } catch {
        /* not ready */
      }
      return false;
    };

    // 3. Fast poll（const，避免 prefer-const；done 在其后定义，通过闭包使用）
    const pollTimer = setInterval(tryConnect, POLL_MS);

    const done = () => {
      clearTimeout(deadTimerRef.current);
      clearInterval(pollTimer);
    };

    const resetDeadline = () => {
      clearTimeout(deadTimerRef.current);
      deadTimerRef.current = setTimeout(() => {
        if (!useAppStore.getState().backendReady) {
          useAppStore.getState().setBackendDead(true);
          clearInterval(pollTimer);
        }
      }, TIMEOUT_MS);
    };

    // 1. Event listener — instant when backend starts
    const unlistenReady = window.yuncat.onBackendReady(([port, token]) => {
      done();
      setPort(port);
      setToken(token);
      setBackendReady(true);
    });

    // 2. Immediate query
    tryConnect();

    // 4. Timeout — 每次收到 backend-starting（解压/重试等）重置计时器
    resetDeadline();
    const unlistenStarting = window.yuncat.onBackendStarting(() => {
      // 后端发来进度消息 → 仍在启动中，延长超时
      if (!useAppStore.getState().backendReady) {
        resetDeadline();
      }
    });

    return () => {
      cancelled.current = true;
      unlistenReady();
      unlistenStarting();
      done();
    };
  }, [setBackendReady, setPort, setToken]);
}
