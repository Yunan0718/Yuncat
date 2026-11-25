/**
 * Task 2.11: 统一快捷键注册表 + 读取层——对标 VS Code keybindings / Linear Keyboard Shortcuts。
 *
 * 原 ShortcutEditor 写 localStorage["yuncat-shortcuts"]，但 App.tsx 等键盘监听处硬编码
 * e.key === "k"/"n"，完全不读 overrides——编辑器是欺骗性 UI。
 *
 * 主流做法（VS Code）：单一快捷键注册表 + resolve(event) 匹配函数，所有监听处通过
 * matchShortcut(event, id) 判定，配置变化后自动生效。
 */

import { useEffect, useState } from "react";

export interface ShortcutDef {
  id: string;
  /** i18n key——ShortcutEditor 渲染时 t(label) */
  label: string;
  /** 默认组合，格式 "Ctrl+K" / "Enter" / "Escape" / "Ctrl+Shift+P" */
  defaultKeys: string;
}

export const SHORTCUT_DEFS: ShortcutDef[] = [
  // 命令面板：Ctrl+Shift+P（与 VS Code / Cursor 一致）——之前默认留空，
  // 结果键盘根本打不开面板，只能去点标题栏菜单
  { id: "command_palette", label: "shortcutEditor.commandPalette", defaultKeys: "Ctrl+Shift+P" },
  // Enter 默认发送（对标 ChatGPT/Claude）；换行用 Shift+Enter。用户可在快捷键设置中改回 Ctrl+Enter 或禁用。
  { id: "send_message", label: "shortcutEditor.sendMessage", defaultKeys: "Enter" },
  // 多窗口：默认 Ctrl+Shift+N（VS Code / Cursor / Chrome 的「新建窗口」约定）
  { id: "new_window", label: "shortcutEditor.newWindow", defaultKeys: "Ctrl+Shift+N" },
  // 面板开关：键位与 VS Code 一致（Ctrl+B 侧栏 / Ctrl+J 底部面板 / Ctrl+` 终端）
  { id: "toggle_sidebar", label: "shortcutEditor.toggleSidebar", defaultKeys: "Ctrl+B" },
  { id: "toggle_bottom_panel", label: "shortcutEditor.toggleBottomPanel", defaultKeys: "Ctrl+J" },
  { id: "toggle_terminal", label: "shortcutEditor.toggleTerminal", defaultKeys: "Ctrl+`" },
  { id: "new_workflow", label: "shortcutEditor.newWorkflow", defaultKeys: "" },
  { id: "close_overlay", label: "shortcutEditor.closeOverlay", defaultKeys: "" },
  { id: "undo", label: "shortcutEditor.undo", defaultKeys: "" },
  { id: "redo", label: "shortcutEditor.redo", defaultKeys: "" },
  { id: "shortcut_help", label: "shortcutEditor.shortcutHelp", defaultKeys: "" },
];

const STORAGE_KEY = "yuncat-shortcuts";
const CHANGE_EVENT = "yuncat-shortcuts-changed";

export function loadShortcutOverrides(): Record<string, string> {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw ? JSON.parse(raw) : {};
  } catch {
    return {};
  }
}

export function saveShortcutOverrides(overrides: Record<string, string>): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(overrides));
    // 通知同窗口其他组件 overrides 变化——订阅的组件可重新读取
    window.dispatchEvent(new CustomEvent(CHANGE_EVENT));
  } catch {
    /* ignore */
  }
}

export function getShortcutKeys(id: string): string {
  const def = SHORTCUT_DEFS.find((s) => s.id === id);
  if (!def) return "";
  const overrides = loadShortcutOverrides();
  return overrides[id] || def.defaultKeys;
}

/**
 * 解析 combo 字符串为 { ctrl, shift, alt, mainKey }。
 * combo 格式："Ctrl+Shift+P" / "Enter" / "Escape" / "Ctrl+K"
 * "Ctrl" 在 macOS 上匹配 metaKey（Cmd），其它平台匹配 ctrlKey——对标 VS Code 跨平台行为。
 */
function parseCombo(combo: string): { ctrl: boolean; shift: boolean; alt: boolean; mainKey: string } {
  const parts = combo.split("+");
  const ctrl = parts.includes("Ctrl");
  const shift = parts.includes("Shift");
  const alt = parts.includes("Alt");
  const mainKey = (parts[parts.length - 1] || "").toLowerCase();
  return { ctrl, shift, alt, mainKey };
}

const SPECIAL_KEY_MAP: Record<string, string> = {
  enter: "Enter",
  escape: "Escape",
  tab: "Tab",
  backspace: "Backspace",
  delete: "Delete",
  space: " ",
  arrowup: "ArrowUp",
  arrowdown: "ArrowDown",
  arrowleft: "ArrowLeft",
  arrowright: "ArrowRight",
};

/** matchShortcut 接受的最低事件形状——兼容 DOM KeyboardEvent 与 React.KeyboardEvent */
export type ShortcutEvent = Pick<KeyboardEvent, "key" | "ctrlKey" | "shiftKey" | "altKey" | "metaKey">;

/**
 * 判断 KeyboardEvent 是否匹配某快捷键 id。
 * 读取当前 overrides（每次调用都读，保证配置变化立即生效）。
 * 兼容 React.KeyboardEvent（SyntheticEvent）——类型用 ShortcutEvent 最低形状。
 */
export function matchShortcut(e: ShortcutEvent, id: string): boolean {
  const keys = getShortcutKeys(id);
  if (!keys) return false;
  const { ctrl, shift, alt, mainKey } = parseCombo(keys);
  if (!mainKey) return false;

  const ctrlPressed = e.ctrlKey || e.metaKey;
  if (ctrl !== ctrlPressed) return false;
  if (shift !== e.shiftKey) return false;
  if (alt !== e.altKey) return false;

  const expected = SPECIAL_KEY_MAP[mainKey] || mainKey;
  return e.key === expected || e.key.toLowerCase() === expected.toLowerCase();
}

/**
 * Hook: 订阅快捷键 overrides 变化，触发组件重渲染。
 * 用于 ShortcutEditor 等需要实时反映 overrides 的组件。
 */
export function useShortcutOverrides(): Record<string, string> {
  const [overrides, setOverrides] = useState<Record<string, string>>(loadShortcutOverrides);
  useEffect(() => {
    const handler = () => setOverrides(loadShortcutOverrides());
    const storageHandler = (e: StorageEvent) => {
      if (e.key === STORAGE_KEY) handler();
    };
    window.addEventListener(CHANGE_EVENT, handler);
    window.addEventListener("storage", storageHandler);
    return () => {
      window.removeEventListener(CHANGE_EVENT, handler);
      window.removeEventListener("storage", storageHandler);
    };
  }, []);
  return overrides;
}

/** 判断键盘事件目标是否处于输入状态——参考页等全局快捷键应让路给原生输入。 */
export function isTypingElement(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  const tag = target.tagName;
  const editable =
    target.isContentEditable ||
    target.contentEditable === "true" ||
    target.getAttribute("contenteditable") === "true";
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || editable;
}
