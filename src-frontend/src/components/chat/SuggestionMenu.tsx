/**
 * SuggestionMenu<T> — Phase 64 通用下拉建议菜单
 *
 * 对标 VSCode QuickInput / Discord slash command picker：
 * - 键盘上下循环选择（ArrowUp/ArrowDown）
 * - Enter 选中、Escape 关闭
 * - 与 InputArea @ autocomplete 风格一致（轻量手写，不引入 cmdk 重依赖）
 *
 * 用法：
 *   <SuggestionMenu
 *     items={commands}
 *     visible={slashMenu.visible}
 *     onSelect={(c) => insertCommand(c.slash_command)}
 *     onClose={() => setSlashMenu({ visible: false })}
 *     getKey={(c) => c.slash_command}
 *     getLabel={(c) => c.slash_command}
 *     getDescription={(c) => c.description}
 *   />
 */
import { useEffect, useRef, useState, type ReactNode } from "react";
import i18next from "i18next";

export interface SuggestionMenuProps<T> {
  items: T[];
  visible: boolean;
  onSelect: (item: T) => void;
  onClose: () => void;
  getKey: (item: T) => string;
  getLabel: (item: T) => string;
  getDescription?: (item: T) => string;
  renderBadge?: (item: T) => ReactNode;
  emptyText?: string;
  /** 渲染在列表顶部的分组标题（如 "Commands"） */
  header?: string;
  className?: string;
}

/** 菜单默认最大高度；上方空间不够时会被压短 */
const DEFAULT_MAX_HEIGHT = 256;

export function SuggestionMenu<T>({
  items,
  visible,
  onSelect,
  onClose,
  getKey,
  getLabel,
  getDescription,
  renderBadge,
  emptyText = i18next.t("suggestionMenu.noMatch"),
  header,
  className = "",
}: SuggestionMenuProps<T>) {
  const [activeIdx, setActiveIdx] = useState(0);
  const [maxHeight, setMaxHeight] = useState(DEFAULT_MAX_HEIGHT);
  const listRef = useRef<HTMLDivElement>(null);

  // items 变化时重置高亮
  useEffect(() => {
    setActiveIdx(0);
  }, [items]);

  // 滚动到可见
  useEffect(() => {
    if (!visible || !listRef.current) return;
    const el = listRef.current.querySelector<HTMLElement>(`[data-idx="${activeIdx}"]`);
    el?.scrollIntoView({ block: "nearest" });
  }, [activeIdx, visible]);

  // 2026-09-16: 菜单高度取“输入框上方剩下的空间”——对标 harness 的 useAnchoredMaxHeight，
  // 否则窗口矮时菜单会顶出屏幕之外
  useEffect(() => {
    if (!visible) return;
    const update = () => {
      const anchorTop = listRef.current?.parentElement?.getBoundingClientRect().top;
      const available = typeof anchorTop === "number" ? anchorTop - 24 : DEFAULT_MAX_HEIGHT;
      setMaxHeight(Math.max(120, Math.min(DEFAULT_MAX_HEIGHT, available)));
    };
    update();
    window.addEventListener("resize", update);
    return () => window.removeEventListener("resize", update);
  }, [visible, items]);

  // 键盘事件——挂载到 document，但只在 visible 时响应
  useEffect(() => {
    if (!visible) return;
    const h = (e: KeyboardEvent) => {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setActiveIdx((i) => (items.length === 0 ? 0 : (i + 1) % items.length));
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        setActiveIdx((i) => (items.length === 0 ? 0 : (i - 1 + items.length) % items.length));
      } else if (e.key === "Enter") {
        if (items.length > 0) {
          e.preventDefault();
          e.stopPropagation();
          onSelect(items[activeIdx]!);
        }
      } else if (e.key === "Escape") {
        e.preventDefault();
        onClose();
      }
    };
    document.addEventListener("keydown", h, true); // capture 优先于 textarea
    return () => document.removeEventListener("keydown", h, true);
  }, [visible, items, activeIdx, onSelect, onClose]);

  if (!visible) return null;

  return (
    <div
      className={`absolute bottom-full left-0 mb-1 w-80 bg-card border rounded-lg shadow-lg z-dropdown overflow-y-auto ${className}`}
      style={{ maxHeight }}
      role="listbox"
      aria-label={header || "suggestions"}
    >
      {header && (
        <div className="px-2 py-1 text-[9px] text-muted-foreground bg-muted/50 sticky top-0">{header}</div>
      )}
      {items.length === 0 ? (
        <div className="px-3 py-2 text-[11px] text-muted-foreground">{emptyText}</div>
      ) : (
        items.map((item, i) => (
          <button
            key={getKey(item)}
            data-idx={i}
            onMouseEnter={() => setActiveIdx(i)}
            onClick={(e) => {
              e.preventDefault();
              onSelect(item);
            }}
            className={`w-full text-left px-3 py-1.5 text-[11px] flex items-center gap-2 ${
              i === activeIdx ? "bg-secondary" : "hover:bg-secondary/50"
            }`}
            role="option"
            aria-selected={i === activeIdx}
          >
            <span className="font-mono text-primary shrink-0">{getLabel(item)}</span>
            {getDescription?.(item) && (
              <span className="text-muted-foreground truncate flex-1">{getDescription(item)}</span>
            )}
            {renderBadge?.(item)}
          </button>
        ))
      )}
    </div>
  );
}
