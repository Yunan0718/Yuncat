import { useEffect, useRef } from "react";
import { useAppStore } from "../store/appStore";

/**
 * Phase 5: 后端假死检测 hook
 *
 * 每 15s 轮询 /api/v1/health。
 * 连续 3 次失败 → 设置 backendDead，前端显示连接断开提示。
 * 恢复后自动清零。
 */
export function useHealthCheck(intervalMs: number = 15000) {
  const port = useAppStore((s) => s.port);
  const token = useAppStore((s) => s.token);
  const backendReady = useAppStore((s) => s.backendReady);
  const failures = useRef(0);
  const setBackendDead = useAppStore((s) => s.setBackendDead);

  useEffect(() => {
    if (!backendReady || !port || !token) return;

    const check = async () => {
      try {
        const res = await fetch(`http://127.0.0.1:${port}/api/v1/health`, {
          signal: AbortSignal.timeout(5000),
        });
        if (res.ok) {
          failures.current = 0;
          if (useAppStore.getState().backendDead) setBackendDead(false);
          return;
        }
      } catch {
        // 网络错误或超时
      }
      failures.current++;
      if (failures.current >= 3) {
        setBackendDead(true);
      }
    };

    const timer = setInterval(check, intervalMs);
    return () => clearInterval(timer);
  }, [backendReady, port, token, intervalMs, setBackendDead]);
}
