import { useCallback, useEffect, useRef, useState } from "react";
import { useApi } from "./useApi";
import { useAppStore } from "../store/appStore";

/**
 * P1 (2026-07-04): 审批 Inbox 轮询间隔——30s。
 * 旧实现仅 mount 时刷新一次，WS 消息丢失则 pending 审批永远不会出现。
 * 30s 轮询 + 通知触发刷新双重保障，对标 Slack/Linear 的通知 inbox 轮询频率。
 */
const APPROVAL_POLL_INTERVAL_MS = 30_000;

/**
 * P1 (2026-07-04): 触发即时刷新的 WS 通知类型——这些类型到达意味着有新审批项需要拉取。
 */
const APPROVAL_NOTIFICATION_TYPES = new Set([
  "tool_confirm_required",
  "bridge_tool_approval",
  "workflow_hitl",
  "approval_required",
]);

export interface ApprovalItem {
  id: string;
  state_type: string;
  payload: Record<string, unknown>;
  created_at: number;
  expires_at: number;
  session_id?: string;
  project_id?: string;
  status?: "pending" | "resolved" | "rejected" | "expired";
  resolved_at?: number;
  resolution?: string;
  title?: string;
  task_id?: string;
}

interface UseApprovalInboxReturn {
  pending: ApprovalItem[];
  history: ApprovalItem[];
  loading: boolean;
  error: string | null;
  refresh: () => Promise<void>;
  approve: (token: string, remember?: boolean) => Promise<unknown>;
  reject: (token: string, reason?: string) => Promise<unknown>;
}

/**
 * Phase 107: 统一审批 Inbox Hook
 *
 * 封装 `/api/v1/approvals/*` 路由，自动维护 pending/history 列表，
 * 并监听全局通知变化触发刷新。
 */
export function useApprovalInbox(): UseApprovalInboxReturn {
  const { api } = useApi();
  const [pending, setPending] = useState<ApprovalItem[]>([]);
  const [history, setHistory] = useState<ApprovalItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [p, h] = await Promise.all([
        api<ApprovalItem[]>("/api/v1/approvals/pending"),
        api<ApprovalItem[]>("/api/v1/approvals/history"),
      ]);
      setPending(p);
      setHistory(h);
      useAppStore.getState().setInboxBadgeCount(p.length);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [api]);

  const approve = useCallback(
    async (token: string, remember = false) => {
      const result = await api(`/api/v1/approvals/${token}/approve`, {
        method: "POST",
        body: JSON.stringify({ remember }),
      });
      await refresh();
      return result;
    },
    [api, refresh],
  );

  const reject = useCallback(
    async (token: string, reason = "") => {
      const result = await api(`/api/v1/approvals/${token}/reject`, {
        method: "POST",
        body: JSON.stringify({ reason }),
      });
      await refresh();
      return result;
    },
    [api, refresh],
  );

  // 首次加载 + inbox 打开时刷新
  useEffect(() => {
    refresh();
  }, [refresh]);

  // P1 (2026-07-04): 30s 轮询——WS 消息丢失时仍能拉到 pending 审批项
  // 对标 Slack/Linear/Notion 的通知 inbox 轮询机制（用户不打开 inbox 也能看到 badge 数）
  const refreshRef = useRef(refresh);
  refreshRef.current = refresh;
  useEffect(() => {
    const timer = setInterval(() => {
      refreshRef.current();
    }, APPROVAL_POLL_INTERVAL_MS);
    return () => clearInterval(timer);
  }, []);

  // P1 (2026-07-04): 订阅通知 store——收到审批类通知时立即刷新（不等下一次轮询）
  // 旧实现注释声称"监听全局通知变化触发刷新"但未实现，此处补全
  useEffect(() => {
    let lastNotifId: string | null = null;
    const unsubscribe = useAppStore.subscribe((state) => {
      const latest = state.notifications[0];
      if (!latest || latest.id === lastNotifId) return;
      lastNotifId = latest.id;
      if (APPROVAL_NOTIFICATION_TYPES.has(latest.type)) {
        refreshRef.current();
      }
    });
    return unsubscribe;
  }, []);

  return { pending, history, loading, error, refresh, approve, reject };
}
