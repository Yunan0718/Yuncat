import { useCallback, type Dispatch, type SetStateAction } from "react";

import { ApiError } from "./useApi";
import { toWorkspacePath, type WorkspaceTab } from "../lib/workspaceTabs";

interface ConfirmOptions {
  title: string;
  description?: string;
  confirmText?: string;
  cancelText?: string;
  variant?: "default" | "destructive";
}

/**
 * 工作区文件保存（2026-09-15 抽自 WorkspacePage）。
 *
 * 两个数据安全点：
 *  1. 二进制文件只读——按文本保存会把乱码写回磁盘，直接拒绝；
 *  2. 乐观并发——带上打开时的 mtime，文件被 Agent/外部编辑器改过时后端回 409，
 *     由用户决定「覆盖」还是「重载」，而不是静默盖掉别人的改动。
 */
export function useWorkspaceFileSave(options: {
  api: <T>(path: string, init?: RequestInit) => Promise<T>;
  activeTab: WorkspaceTab | undefined;
  activeIdx: number;
  dirty: boolean;
  setTabs: Dispatch<SetStateAction<WorkspaceTab[]>>;
  setStatusMsg: (msg: string) => void;
  confirm: (options: ConfirmOptions) => Promise<boolean>;
  t: (key: string, opts?: Record<string, unknown>) => string;
}): (force?: boolean) => Promise<void> {
  const { api, activeTab, activeIdx, dirty, setTabs, setStatusMsg, confirm, t } = options;

  return useCallback(
    async (force = false) => {
      if (!activeTab || !dirty) return;
      if (activeTab.binary) {
        setStatusMsg(t("workspacePage.binaryReadOnly"));
        return;
      }
      const rel = toWorkspacePath(activeTab.path);
      // 最多两轮：第一轮带 expected_mtime；用户确认覆盖后第二轮不带（强制写）
      for (let attempt = 0; attempt < 2; attempt++) {
        const sendExpected = !force && activeTab.mtime !== undefined && attempt === 0;
        try {
          const res = await api<{ modified?: number }>("/api/v1/workspace/files/save", {
            method: "POST",
            body: JSON.stringify({
              path: rel,
              content: activeTab.content,
              ...(sendExpected ? { expected_mtime: activeTab.mtime } : {}),
            }),
          });
          setTabs((prev) =>
            prev.map((t, i) =>
              i === activeIdx ? { ...t, originalContent: t.content, mtime: res?.modified ?? t.mtime } : t,
            ),
          );
          setStatusMsg(t("workspacePage.saved"));
          return;
        } catch (e) {
          if (e instanceof ApiError && e.status === 409 && attempt === 0) {
            const overwrite = await confirm({
              title: t("workspacePage.saveConflictTitle"),
              description: t("workspacePage.saveConflictDesc", { name: activeTab.name }),
              confirmText: t("workspacePage.saveConflictOverwrite"),
              cancelText: t("common.cancel"),
              variant: "destructive",
            });
            if (overwrite) continue; // 第二轮：不带 expected_mtime，强制覆盖
            return;
          }
          setStatusMsg(t("workspacePage.saveFailed"));
          return;
        }
      }
    },
    [api, activeTab, activeIdx, dirty, setTabs, setStatusMsg, confirm, t],
  );
}
