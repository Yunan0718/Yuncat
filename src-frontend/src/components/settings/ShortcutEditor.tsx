import { useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  SHORTCUT_DEFS,
  loadShortcutOverrides,
  saveShortcutOverrides,
  useShortcutOverrides,
  type ShortcutDef,
} from "../../lib/shortcuts";
import { ShortcutReferenceDialog } from "./ShortcutReferenceDialog";

/**
 * Phase 7: 快捷键自定义编辑器
 *
 * Task 2.11: 读写统一走 lib/shortcuts.ts——与 App.tsx 等键盘监听处共享同一注册表。
 * 原 DEFAULT_SHORTCUTS / loadOverrides / saveOverrides 已下沉到 lib/shortcuts.ts。
 *
 * - 列出所有可配置快捷键
 * - 点击录制新按键
 * - 冲突检测 + 重置
 */
export function ShortcutEditor() {
  const { t } = useTranslation();
  // Task 2.11: 用 useShortcutOverrides 订阅变化——其他标签页修改后自动同步
  const overrides = useShortcutOverrides();
  const [, forceRerender] = useState(0);
  const [recording, setRecording] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [refOpen, setRefOpen] = useState(false);
  const recordingRef = useRef<string | null>(null);

  const getKeys = (s: ShortcutDef) => overrides[s.id] || s.defaultKeys;

  const startRecord = (id: string) => {
    setRecording(id);
    recordingRef.current = id;
    setError(null);

    const handler = (e: KeyboardEvent) => {
      e.preventDefault();
      const parts: string[] = [];
      if (e.ctrlKey || e.metaKey) parts.push("Ctrl");
      if (e.shiftKey) parts.push("Shift");
      if (e.altKey) parts.push("Alt");
      const key =
        e.key === "Control" || e.key === "Shift" || e.key === "Alt" || e.key === "Meta"
          ? null
          : e.key.length === 1
            ? e.key.toUpperCase()
            : e.key;
      if (key) parts.push(key);

      if (parts.length < 2) return;

      const combo = parts.join("+");

      for (const s of SHORTCUT_DEFS) {
        if (s.id !== id && getKeys(s) === combo) {
          setError(t("shortcutEditor.conflict", { combo, label: t(s.label) }));
          document.removeEventListener("keydown", handler);
          setRecording(null);
          recordingRef.current = null;
          return;
        }
      }

      const newOverrides = { ...loadShortcutOverrides(), [id]: combo };
      saveShortcutOverrides(newOverrides);
      forceRerender((n) => n + 1);
      document.removeEventListener("keydown", handler);
      setRecording(null);
      recordingRef.current = null;
    };

    document.addEventListener("keydown", handler);
    setTimeout(() => {
      document.removeEventListener("keydown", handler);
      if (recordingRef.current === id) {
        setRecording(null);
        recordingRef.current = null;
      }
    }, 5000);
  };

  const resetAll = () => {
    saveShortcutOverrides({});
    setError(null);
  };

  return (
    <div className="space-y-4">
      <p className="text-sm text-muted-foreground">{t("shortcutEditor.description")}</p>
      {error && <div className="text-xs text-destructive bg-destructive/10 rounded px-3 py-1.5">{error}</div>}
      <div className="space-y-2">
        {SHORTCUT_DEFS.map((s) => {
          const keys = getKeys(s);
          const isRecording = recording === s.id;
          return (
            <div key={s.id} className="flex items-center justify-between border rounded px-3 py-2">
              <span className="text-sm">{t(s.label)}</span>
              <button
                onClick={() => startRecord(s.id)}
                className={`px-3 py-1 rounded text-xs font-mono border transition-colors ${isRecording ? "bg-primary-muted border-primary text-primary animate-pulse" : "hover:bg-secondary"}`}
              >
                {isRecording ? t("shortcutEditor.recording") : keys}
              </button>
            </div>
          );
        })}
      </div>
      <div className="flex items-center gap-2">
        <button
          onClick={resetAll}
          className="text-xs text-muted-foreground hover:text-destructive border rounded px-2 py-1"
        >
          {t("shortcutEditor.reset")}
        </button>
        <button
          onClick={() => setRefOpen(true)}
          className="text-xs text-primary hover:underline"
        >
          {t("shortcutEditor.openReference")}
        </button>
      </div>
      <ShortcutReferenceDialog open={refOpen} onOpenChange={setRefOpen} />
    </div>
  );
}
