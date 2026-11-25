/**
 * TerminalFindBar.tsx —— Phase 66 终端查找栏
 *
 * 对标 VSCode 终端查找栏（Ctrl+Shift+F）：
 * - 输入框实时搜索（findNext）
 * - ↑/↓ 按钮 findPrevious/findNext
 * - Escape 关闭
 * - ARIA 无障碍
 */
import { useEffect, useRef, useState } from "react";
import { ChevronUp, ChevronDown, X, Search } from "lucide-react";
import type { SearchAddon } from "@xterm/addon-search";

export interface TerminalFindBarProps {
  visible: boolean;
  onClose: () => void;
  searchAddon: Pick<SearchAddon, "findNext" | "findPrevious"> | null;
  t: (k: string, opts?: Record<string, unknown>) => string;
}

export function TerminalFindBar({ visible, onClose, searchAddon, t }: TerminalFindBarProps) {
  const [query, setQuery] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);

  // 可见时自动聚焦
  useEffect(() => {
    if (visible) {
      inputRef.current?.focus();
      inputRef.current?.select();
    } else {
      setQuery("");
    }
  }, [visible]);

  // 实时搜索
  useEffect(() => {
    if (!visible || !searchAddon) return;
    if (query) searchAddon.findNext(query);
  }, [query, visible, searchAddon]);

  if (!visible) return null;

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Escape") {
      e.preventDefault();
      onClose();
    } else if (e.key === "Enter") {
      e.preventDefault();
      if (e.shiftKey) {
        searchAddon?.findPrevious(query);
      } else {
        searchAddon?.findNext(query);
      }
    }
  };

  return (
    <div className="absolute top-1 right-2 z-dropdown flex items-center gap-1 bg-card border border-border rounded-lg shadow-lg px-2 py-1">
      <Search size={12} className="text-muted-foreground shrink-0" />
      <input
        ref={inputRef}
        type="text"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        onKeyDown={handleKeyDown}
        placeholder={t("phase66.findPlaceholder")}
        className="w-48 bg-transparent text-xs outline-none placeholder:text-muted-foreground"
        aria-label={t("phase66.findLabel")}
      />
      <button
        onClick={() => searchAddon?.findPrevious(query)}
        className="text-muted-foreground hover:text-foreground hover:bg-secondary rounded p-0.5"
        title={t("phase66.findPrev")}
        aria-label={t("phase66.findPrev")}
      >
        <ChevronUp size={14} />
      </button>
      <button
        onClick={() => searchAddon?.findNext(query)}
        className="text-muted-foreground hover:text-foreground hover:bg-secondary rounded p-0.5"
        title={t("phase66.findNext")}
        aria-label={t("phase66.findNext")}
      >
        <ChevronDown size={14} />
      </button>
      <button
        onClick={onClose}
        className="text-muted-foreground hover:text-foreground hover:bg-secondary rounded p-0.5"
        title={t("common.close")}
        aria-label={t("common.close")}
      >
        <X size={14} />
      </button>
    </div>
  );
}
