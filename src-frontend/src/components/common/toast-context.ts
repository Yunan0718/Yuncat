import { createContext } from "react";

export interface Toast {
  id: number;
  message: string;
  type: "info" | "success" | "error";
}

export interface ToastContextValue {
  toast: (message: string, type?: "info" | "success" | "error") => void;
}

export const ToastContext = createContext<ToastContextValue>({ toast: () => {} });

// ═══════════════════════════════════════════════════════════════
// P0-3 (2026-07-04): 全局 Toast 桥——允许非 React 代码（如 WS 消息处理器）
// 触发 toast。ToastProvider 在挂载时订阅 emitter，卸载时取消订阅。
// 对标 Sonner / react-hot-toast 的 global emitter 模式。
// ═══════════════════════════════════════════════════════════════
type ToastListener = (message: string, type: "info" | "success" | "error") => void;
let _listener: ToastListener | null = null;

/**
 * 全局触发 toast——供非 React 代码（useWebSocket dispatchToStore 等）使用。
 * React 组件内仍应优先使用 useToast() hook（直接 useContext）。
 */
export function emitGlobalToast(message: string, type: "info" | "success" | "error" = "info"): void {
  if (_listener) {
    _listener(message, type);
  }
}

/** ToastProvider 内部使用——注册全局 listener */
export function _setGlobalToastListener(listener: ToastListener | null): void {
  _listener = listener;
}
