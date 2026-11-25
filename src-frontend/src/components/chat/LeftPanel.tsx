import { Folder, Search } from "lucide-react";
import { useApi } from "@/hooks/useApi";
import { FileTree } from "@/components/workspace/FileTree";
import { SessionList } from "@/components/SessionList";

interface LeftPanelProps {
  leftPanel: "files" | "sessions";
  setLeftPanel: (v: "files" | "sessions") => void;
  activeTabPath: string | null;
  onOpenFile: (path: string, name: string) => void;
  onAddToChat?: (path: string) => void;
  showSearch: boolean;
  setShowSearch: (v: boolean | ((p: boolean) => boolean)) => void;
  /** 兼容旧调用点：模型名已在输入区显示，这里不再使用 */
  className?: string;
  style?: React.CSSProperties;
  t: (k: string, opts?: Record<string, unknown>) => string;
}

export function LeftPanel({
  leftPanel,
  setLeftPanel,
  activeTabPath,
  onOpenFile,
  onAddToChat,
  showSearch,
  setShowSearch,
  className,
  style,
  t,
}: LeftPanelProps) {
  const { api } = useApi();

  const handleOpenFolder = async () => {
    const r = await window.yuncat?.openFileDialog?.({ selectFolder: true, title: t("chat.openFolder") });
    if (r?.ok && r.path) {
      try {
        await api("/api/v1/workspace/files/root", {
          method: "POST",
          body: JSON.stringify({ path: r.path }),
        });
      } catch {
        // no-op
      }
    }
  };

  return (
    <div className={`shrink-0 flex flex-col bg-surface-1 ${className ?? ""}`} style={style}>
      {/* 顶部只放「在哪个列表之间切换」这一件事。
          原来这里还并排放着搜索按钮和当前模型名，在两百多像素宽的侧栏里必然挤成竖排文字；
          模型名已经在输入区那一行显示，不该在这里再出现一次。 */}
      <div className="h-9 flex items-center gap-1 px-2 shrink-0">
        <div className="flex items-center gap-0.5 rounded-lg bg-surface-3 p-0.5">
          <button
            type="button"
            onClick={() => setLeftPanel("files")}
            aria-pressed={leftPanel === "files"}
            className={`px-2.5 py-1 rounded-md text-[11px] transition-colors ${
              leftPanel === "files"
                ? "bg-surface-1 text-foreground font-medium shadow-sm"
                : "text-muted-foreground hover:text-foreground"
            }`}
          >
            {t("nav.workspace")}
          </button>
          <button
            type="button"
            onClick={() => setLeftPanel("sessions")}
            aria-pressed={leftPanel === "sessions"}
            className={`px-2.5 py-1 rounded-md text-[11px] transition-colors ${
              leftPanel === "sessions"
                ? "bg-surface-1 text-foreground font-medium shadow-sm"
                : "text-muted-foreground hover:text-foreground"
            }`}
          >
            {t("nav.chat")}
          </button>
        </div>
        <div className="flex-1 min-w-0" />
        <button
          type="button"
          onClick={() => setShowSearch(!showSearch)}
          aria-pressed={showSearch}
          title={t("chat.searchConversation")}
          aria-label={t("chat.searchConversation")}
          className={`h-6 w-6 inline-flex items-center justify-center rounded-md transition-colors shrink-0 ${
            showSearch ? "bg-surface-3 text-foreground" : "text-muted-foreground hover:text-foreground hover:bg-surface-3"
          }`}
        >
          <Search size={13} />
        </button>
        <button
          type="button"
          onClick={handleOpenFolder}
          title={t("chat.openFolder")}
          aria-label={t("chat.openFolder")}
          className="h-6 w-6 inline-flex items-center justify-center rounded-md text-muted-foreground hover:text-foreground hover:bg-surface-3 transition-colors shrink-0"
        >
          <Folder size={13} />
        </button>
      </div>
      <div className="flex-1 min-h-0 overflow-y-auto">
        {leftPanel === "files" ? (
          <FileTree onSelect={(p, n) => onOpenFile(p, n)} selectedPath={activeTabPath} onAddToChat={onAddToChat} />
        ) : (
          <SessionList />
        )}
      </div>
    </div>
  );
}
