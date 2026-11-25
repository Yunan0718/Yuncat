import { useEffect, useRef, type Dispatch, type SetStateAction } from "react";

import { savePersistedTabs, toWorkspacePath, type WorkspaceTab } from "../lib/workspaceTabs";

/**
 * 工作区标签页的「恢复 + 落盘」（2026-09-15 抽自 WorkspacePage）。
 *
 * 恢复：带未保存编辑的标签用本地版本，其余回磁盘重读——序列化时已把「已保存正文」
 * 折叠成空串（见 lib/workspaceTabs.savePersistedTabs），因此本地为空即等于需要重读。
 * 落盘：标签或激活项一变就写，React 批处理天然节流。
 */
export function useWorkspaceTabPersistence(options: {
  backendReady: boolean;
  restored: { tabs: WorkspaceTab[]; activeIdx: number } | null;
  tabs: WorkspaceTab[];
  activeIdx: number;
  api: <T>(path: string, init?: RequestInit) => Promise<T>;
  setTabs: Dispatch<SetStateAction<WorkspaceTab[]>>;
}): void {
  const { backendReady, restored, tabs, activeIdx, api, setTabs } = options;
  const hydratedRef = useRef(false);

  useEffect(() => {
    if (!backendReady || hydratedRef.current) return;
    const pending = restored?.tabs ?? [];
    if (!pending.length) return;
    hydratedRef.current = true;
    (async () => {
      for (const tab of pending) {
        if (tab.content || tab.originalContent) continue;
        try {
          const rel = toWorkspacePath(tab.path);
          const res = await api<{ content: string }>(
            `/api/v1/workspace/files/read?path=${encodeURIComponent(rel)}`,
          );
          setTabs((prev) =>
            prev.map((t) =>
              t.key === tab.key
                ? { ...t, content: res.content, originalContent: res.content, loading: false }
                : t,
            ),
          );
        } catch {
          // 文件已被删除/重命名——移除标签，别留一个空壳
          setTabs((prev) => prev.filter((t) => t.key !== tab.key));
        }
      }
    })();
  }, [backendReady, api, restored, setTabs]);

  useEffect(() => {
    savePersistedTabs(tabs, activeIdx);
  }, [tabs, activeIdx]);
}
