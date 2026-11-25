import { useCallback, useEffect, useState } from "react";

/**
 * Phase 70 / P0-7: @ 自动补全状态机——对标 Cursor @ 引用
 *
 * 抽取自 ChatView.tsx 候选 11。封装：
 * - atMenu 菜单状态（visible/query/cursorPos）
 * - sessionsForAt 会话列表（供 @session: 引用）
 * - sessions 加载 effect（backendReady 后拉取前 20 条）
 * - handleAtAutocomplete 回调（从光标位置回找最近的 @）
 *
 * 设计说明：
 * - 菜单触发规则：光标前 30 字符内有 @，且 @ 后无冒号（已输入完整 @type:value 则关闭）
 * - sessions 加载只取前 20 条，避免菜单过长（对标 Cursor 引用列表截断）
 */
export interface AtMenuState {
  visible: boolean;
  query: string;
  cursorPos: number;
}

export interface SessionForAt {
  session_id: string;
  title: string;
  msg_count: number;
  last_at: number;
}

export interface UseAtAutocompleteParams {
  api: <T>(path: string, options?: RequestInit) => Promise<T>;
  backendReady: boolean;
}

export interface UseAtAutocompleteReturn {
  atMenu: AtMenuState;
  setAtMenu: React.Dispatch<React.SetStateAction<AtMenuState>>;
  sessionsForAt: SessionForAt[];
  handleAtAutocomplete: (text: string, cursorPos: number) => void;
}

const INITIAL_AT_MENU: AtMenuState = { visible: false, query: "", cursorPos: 0 };

export function useAtAutocomplete(p: UseAtAutocompleteParams): UseAtAutocompleteReturn {
  const { api, backendReady } = p;
  const [atMenu, setAtMenu] = useState<AtMenuState>(INITIAL_AT_MENU);
  const [sessionsForAt, setSessionsForAt] = useState<SessionForAt[]>([]);

  // 加载会话列表供 @ 自动补全（前 20 条）
  useEffect(() => {
    if (!backendReady) return;
    api<SessionForAt[]>("/api/v1/sessions")
      .then((s) => setSessionsForAt(s.slice(0, 20)))
      .catch((e) => console.warn("useAtAutocomplete: sessions load failed:", e));
  }, [backendReady, api]);

  const handleAtAutocomplete = useCallback(
    (text: string, cursorPos: number) => {
      // 从光标位置回找最近的 @
      const beforeCursor = text.slice(0, cursorPos);
      const atIdx = beforeCursor.lastIndexOf("@");
      if (atIdx === -1 || cursorPos - atIdx > 30) {
        setAtMenu(INITIAL_AT_MENU);
        return;
      }
      const afterAt = beforeCursor.slice(atIdx + 1);
      // 如果已经输入了完整的 @type:value（有冒号），关闭菜单
      if (afterAt.includes(":")) {
        setAtMenu(INITIAL_AT_MENU);
        return;
      }
      setAtMenu({ visible: true, query: afterAt.toLowerCase(), cursorPos: atIdx });
    },
    [],
  );

  return { atMenu, setAtMenu, sessionsForAt, handleAtAutocomplete };
}
