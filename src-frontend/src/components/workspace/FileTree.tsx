import { useCallback, useEffect, useRef, useState } from "react";
import { useApi, ApiError } from "../../hooks/useApi";
import { useWorkspaceUpload } from "../../hooks/useWorkspaceUpload";
import { toWorkspaceRelative } from "../../lib/workspaceTabs";
import { useAppStore } from "../../store/appStore";
import { useTranslation } from "react-i18next";
import { useToast } from "../../hooks/use-toast";
import { useConfirmDialog } from "../../hooks/useConfirmDialog";
import { usePromptDialog } from "../../hooks/usePromptDialog";
import { getFileIconMeta } from "./FileTreeIcons";
import { FileCompareDialog } from "./FileCompareDialog";
import { FileTimelineDialog } from "./FileTimelineDialog";
import {
  Folder,
  FolderOpen,
  ChevronRight,
  Copy,
  ExternalLink,
  FolderOpen as FolderReveal,
  Terminal,
  MessageSquarePlus,
  Clock,
  GitCompare,
  Pin,
  PinOff,
} from "lucide-react";
interface FileEntry {
  name: string;
  path: string;
  is_dir: boolean;
  size: number;
  modified: number;
}
function fmtSize(b: number) {
  if (b < 1024) return `${b} B`;
  if (b < 1048576) return `${(b / 1024).toFixed(1)} KB`;
  return `${(b / 1048576).toFixed(1)} MB`;
}

/* ================================================================
 * ContextMenu
 * ================================================================ */
type MenuItem =
  | { type: "item"; label: string; action: () => void; danger?: boolean; icon?: React.ReactNode }
  | { type: "separator" };

function ContextMenu({ x, y, items, onClose }: { x: number; y: number; items: MenuItem[]; onClose: () => void }) {
  useEffect(() => {
    const h = () => onClose();
    document.addEventListener("click", h);
    return () => document.removeEventListener("click", h);
  }, [onClose]);
  return (
    <div
      className="fixed z-ctx bg-background border border-border rounded-lg shadow-lg py-1 min-w-[170px]"
      style={{ left: x, top: y }}
    >
      {items.map((item, idx) =>
        item.type === "separator" ? (
          // eslint-disable-next-line react/no-array-index-key
          <div key={`sep-${idx}`} className="my-1 border-t border-border" />
        ) : (
          <button
            key={item.label}
            onClick={(e) => {
              e.stopPropagation();
              item.action();
              onClose();
            }}
            className={`w-full text-left px-2.5 py-1.5 text-[11px] hover:bg-secondary transition-colors flex items-center gap-2 ${
              item.danger ? "text-destructive" : "text-foreground"
            }`}
          >
            {item.icon && <span className="text-muted-foreground">{item.icon}</span>}
            <span className="flex-1">{item.label}</span>
          </button>
        ),
      )}
    </div>
  );
}

/* ================================================================
 * DirNode — recursive tree node
 * ================================================================ */
function FileTreeIcon({ name, isDir, open }: { name: string; isDir: boolean; open?: boolean }) {
  if (isDir) {
    const Icon = open ? FolderOpen : Folder;
    return <Icon size={14} className="shrink-0 text-yellow-500/90" />;
  }
  const meta = getFileIconMeta(name);
  const Icon = meta.icon;
  return <Icon size={14} className={`shrink-0 ${meta.color}`} />;
}

function DirNode({
  entry,
  depth,
  selectedPath,
  onSelect,
  onRefresh,
  api,
  gitFiles,
  selectedFiles,
  onToggleSelect,
  onAddToChat,
  onOpenTerminal,
  onShowTimeline,
  compareBase,
  onSetCompareBase,
  onCompareWith,
  workspaceRoot,
  t,
  readOnly = false,
}: {
  entry: FileEntry;
  depth: number;
  selectedPath: string | null;
  onSelect: (p: string, n: string) => void;
  onRefresh: () => void;
  api: <T>(u: string, i?: RequestInit) => Promise<T>;
  gitFiles: Record<string, string>;
  selectedFiles?: Set<string>;
  onToggleSelect?: (path: string) => void;
  onAddToChat?: (path: string) => void;
  onOpenTerminal?: (path: string) => void;
  onShowTimeline?: (path: string) => void;
  compareBase: string | null;
  onSetCompareBase: (path: string | null) => void;
  onCompareWith: (path: string) => void;
  workspaceRoot: string;
  t: (key: string, options?: Record<string, unknown>) => string;
  readOnly?: boolean;
}) {
  const { toast } = useToast();
  const { prompt: promptDialogFn, promptDialog } = usePromptDialog();
  const [expanded, setExpanded] = useState(false);
  const [children, setChildren] = useState<FileEntry[] | null>(null);
  const [menuPos, setMenuPos] = useState<{ x: number; y: number } | null>(null);
  const [isDropTarget, setIsDropTarget] = useState(false);

  const relPath = toWorkspaceRelative(entry.path, workspaceRoot);  // 2026-09-15: 路径语义见 lib/workspaceTabs

  const toggle = async (e?: React.MouseEvent) => {
    if (e?.ctrlKey && onToggleSelect && !entry.is_dir) {
      onToggleSelect(entry.path);
      return;
    }
    if (!entry.is_dir) {
      onSelect(entry.path, entry.name);
      return;
    }
    if (!expanded && children === null) {
      try {
        const files = await api<FileEntry[]>(`/api/v1/workspace/files?dir=${encodeURIComponent(relPath)}`);
        setChildren(files);
      } catch {
        setChildren([]);
        toast(t("fileTree.loadFailed"), "error");
      }
    }
    setExpanded(!expanded);
  };

  const handleCtx = (e: React.MouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
    setMenuPos({ x: e.clientX, y: e.clientY });
  };

  // Task 7.25: 拖拽移动文件——对标 VS Code 文件树原生 HTML5 DnD
  const handleDragStart = (e: React.DragEvent) => {
    if (readOnly) return;
    e.dataTransfer.setData("application/x-yuncat-move", relPath);
    e.dataTransfer.effectAllowed = "move";
  };
  const handleNodeDragOver = (e: React.DragEvent) => {
    if (readOnly) return;
    if (!e.dataTransfer.types.includes("application/x-yuncat-move")) return;
    e.preventDefault();
    e.stopPropagation();
    e.dataTransfer.dropEffect = "move";
    setIsDropTarget(true);
  };
  const handleNodeDragLeave = (e: React.DragEvent) => {
    const related = e.relatedTarget as Node | null;
    if (!related || !e.currentTarget.contains(related)) {
      setIsDropTarget(false);
    }
  };
  const handleNodeDrop = async (e: React.DragEvent) => {
    const sourceRelPath = e.dataTransfer.getData("application/x-yuncat-move");
    if (!sourceRelPath) return;
    e.preventDefault();
    e.stopPropagation();
    setIsDropTarget(false);
    // 放置在目录上 → 移入该目录；放置在文件上 → 移到其父目录（对标 VS Code）
    const targetDir = entry.is_dir ? relPath : relPath.split("/").slice(0, -1).join("/");
    // 防止将目录移入自身或其子孙
    if (sourceRelPath === targetDir || (entry.is_dir && targetDir.startsWith(sourceRelPath + "/"))) {
      toast(t("fileTree.cannotMoveIntoSelf"), "error");
      return;
    }
    const fileName = sourceRelPath.split("/").pop() || "";
    const newRelPath = targetDir ? `${targetDir}/${fileName}` : fileName;
    if (sourceRelPath === newRelPath) return; // 同位置 no-op
    try {
      await api("/api/v1/workspace/files/rename", {
        method: "POST",
        body: JSON.stringify({ old_path: sourceRelPath, new_path: newRelPath }),
      });
      onRefresh();
    } catch (err) {
      const msg = err instanceof ApiError ? err.message : t("fileTree.moveFailed");
      toast(msg, "error");
    }
  };

  const deleteFile = async () => {
    try {
      await api(`/api/v1/workspace/files?path=${encodeURIComponent(relPath)}`, { method: "DELETE" });
      onRefresh();
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : t("fileTree.deleteFailed");
      toast(msg, "error");
    }
  };
  const renameFile = async () => {
    const nn = await promptDialogFn({
      title: t("fileTree.rename"),
      defaultValue: entry.name,
      placeholder: t("fileTree.renamePrompt"),
    });
    if (!nn || nn === entry.name) return;
    try {
      const dir = relPath.split("/").slice(0, -1).join("/");
      const nr = dir ? `${dir}/${nn}` : nn;
      await api("/api/v1/workspace/files/rename", {
        method: "POST",
        body: JSON.stringify({ old_path: relPath, new_path: nr }),
      });
      onRefresh();
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : t("fileTree.renameFailed");
      toast(msg, "error");
    }
  };

  const copyToClipboard = (text: string) => {
    navigator.clipboard.writeText(text).catch(() => toast(t("fileTree.copyFailed"), "error"));
  };

  const revealInFolder = () => {
    if (!entry.is_dir) {
      window.yuncat?.showItemInFolder?.(entry.path).catch(() => toast(t("fileTree.revealInFolderFailed"), "error"));
    }
  };
  const openExternally = () => {
    window.yuncat?.openPath?.(entry.path).catch(() => toast(t("fileTree.openExternalFailed"), "error"));
  };
  const copyPath = () => copyToClipboard(entry.path);
  const copyRelativePath = () => {
    const rel = entry.path.replace(workspaceRoot.replace(/\\/g, "/"), "").replace(/^\//, "");
    copyToClipboard(rel || entry.name);
  };
  const copyContent = async () => {
    try {
      const r = await api<{ content: string }>(`/api/v1/workspace/files/read?path=${encodeURIComponent(relPath)}`);
      copyToClipboard(r.content);
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : t("fileTree.copyFailed");
      toast(msg, "error");
    }
  };

  const isSelected = selectedPath === entry.path;
  const isMultiSelected = selectedFiles?.has(entry.path);
  const isCompareBase = compareBase === entry.path;

  const menuItems: MenuItem[] = [
    ...(entry.is_dir
      ? [
          {
            type: "item" as const,
            label: expanded ? t("fileTree.collapse") : t("fileTree.expand"),
            action: () => setExpanded(!expanded),
            icon: <ChevronRight size={13} className={expanded ? "rotate-90" : ""} />,
          },
        ]
      : []),
    ...(entry.is_dir
      ? []
      : [
          {
            type: "item" as const,
            label: t("fileTree.openExternal"),
            action: openExternally,
            icon: <ExternalLink size={13} />,
          },
          { type: "separator" as const },
        ]),
    {
      type: "item" as const,
      label: t("fileTree.revealInFolder"),
      action: revealInFolder,
      icon: <FolderReveal size={13} />,
    },
    {
      type: "item" as const,
      label: t("fileTree.openInTerminal"),
      action: () =>
        onOpenTerminal?.(entry.is_dir ? entry.path : entry.path.replace(/\\/g, "/").split("/").slice(0, -1).join("/")),
      icon: <Terminal size={13} />,
    },
    { type: "separator" as const },
    { type: "item" as const, label: t("fileTree.copyPath"), action: copyPath, icon: <Copy size={13} /> },
    {
      type: "item" as const,
      label: t("fileTree.copyRelativePath"),
      action: copyRelativePath,
      icon: <Copy size={13} />,
    },
    ...(!entry.is_dir
      ? [{ type: "item" as const, label: t("fileTree.copyContent"), action: copyContent, icon: <Copy size={13} /> }]
      : []),
    { type: "separator" as const },
    {
      type: "item" as const,
      label: t("fileTree.addToChat"),
      action: () => onAddToChat?.(relPath),
      icon: <MessageSquarePlus size={13} />,
    },
    ...(!entry.is_dir
      ? [
          { type: "separator" as const },
          {
            type: "item" as const,
            label: isCompareBase ? t("fileTree.clearCompareBase") : t("fileTree.setCompareBase"),
            action: () => onSetCompareBase(isCompareBase ? null : entry.path),
            icon: isCompareBase ? <PinOff size={13} /> : <Pin size={13} />,
          },
          ...(compareBase && compareBase !== entry.path
            ? [
                {
                  type: "item" as const,
                  label: t("fileTree.compareWithSelected"),
                  action: () => onCompareWith(entry.path),
                  icon: <GitCompare size={13} />,
                },
              ]
            : []),
          {
            type: "item" as const,
            label: t("fileTree.timeline"),
            action: () => onShowTimeline?.(relPath),
            icon: <Clock size={13} />,
          },
        ]
      : []),
    { type: "separator" as const },
    {
      type: "item" as const,
      label: t("fileTree.viewHistory"),
      action: async () => {
        try {
          const r = await api<{ entries: Array<{ id: string; label: string; mtime: number }> }>(
            `/api/v1/tools/checkpoint_list?path=${encodeURIComponent(relPath)}`,
          );
          if (r.entries?.length) {
            const list = r.entries
              .map((e) => `${e.id.slice(0, 8)}  ${e.label} (${new Date(e.mtime * 1000).toLocaleString()})`)
              .join("\n");
            const cid = await promptDialogFn({
              title: t("fileTree.restoreCheckpoint"),
              description: t("fileTree.historyPrompt", { list }),
              placeholder: t("fileTree.checkpointIdPlaceholder"),
            });
            if (cid) {
              await api("/api/v1/tools/checkpoint_restore", {
                method: "POST",
                body: JSON.stringify({ path: relPath, checkpoint_id: cid }),
              });
              onRefresh();
            }
          } else {
            toast(t("fileTree.noHistory"), "info");
          }
        } catch (e) {
          const msg = e instanceof ApiError ? e.message : t("fileTree.historyError");
          toast(msg, "error");
        }
      },
    },
    { type: "item" as const, label: t("fileTree.rename"), action: renameFile, icon: <ExternalLink size={13} /> },
    { type: "item" as const, label: t("fileTree.delete"), action: deleteFile, danger: true },
  ];

  return (
    <div className={`${isMultiSelected ? "bg-primary/10 rounded" : ""}`}>
      <button
        onClick={(e) => toggle(e)}
        onContextMenu={handleCtx}
        draggable={!readOnly}
        onDragStart={handleDragStart}
        onDragOver={handleNodeDragOver}
        onDragLeave={handleNodeDragLeave}
        onDrop={handleNodeDrop}
        className={`w-full text-left px-2 py-0.5 text-[11px] rounded flex items-center gap-1 transition-colors ${
          isDropTarget
            ? "bg-primary/20 ring-1 ring-primary"
            : isSelected
              ? "bg-primary-muted text-primary font-medium"
              : "hover:bg-secondary text-muted-foreground"
        }`}
        style={{ paddingLeft: `${6 + depth * 12}px` }}
        title={entry.name}
      >
        {entry.is_dir ? (
          <ChevronRight size={10} className={`shrink-0 transition-transform ${expanded ? "rotate-90" : ""}`} />
        ) : (
          <span className="w-[10px]" />
        )}
        <FileTreeIcon name={entry.name} isDir={entry.is_dir} open={expanded} />
        <span
          className={`truncate ${
            !entry.is_dir && gitFiles[entry.path]
              ? gitFiles[entry.path] === "M" || gitFiles[entry.path] === " M"
                ? "text-warning font-medium"
                : gitFiles[entry.path] === "A" || gitFiles[entry.path] === " A"
                  ? "text-success font-medium"
                  : gitFiles[entry.path].includes("D")
                    ? "text-destructive line-through"
                    : ""
              : ""
          }`}
        >
          {entry.name}
        </span>
        {isCompareBase && <Pin size={10} className="text-primary ml-0.5" />}
        {!entry.is_dir && (
          <span className="text-[9px] text-muted-foreground ml-auto shrink-0">{fmtSize(entry.size)}</span>
        )}
      </button>
      {expanded && children && children.length > 0 && (
        <div data-dir={relPath}>
          {children.map((c) => (
            <DirNode
              key={c.path}
              entry={c}
              depth={depth + 1}
              selectedPath={selectedPath}
              onSelect={onSelect}
              onRefresh={onRefresh}
              api={api}
              gitFiles={gitFiles}
              selectedFiles={selectedFiles}
              onToggleSelect={onToggleSelect}
              onAddToChat={onAddToChat}
              onOpenTerminal={onOpenTerminal}
              onShowTimeline={onShowTimeline}
              compareBase={compareBase}
              onSetCompareBase={onSetCompareBase}
              onCompareWith={onCompareWith}
              workspaceRoot={workspaceRoot}
              t={t}
            />
          ))}
        </div>
      )}
      {expanded && children && children.length === 0 && (
        <p className="text-[10px] text-muted-foreground px-2 py-0.5" style={{ paddingLeft: `${20 + depth * 12}px` }}>
          {t("fileTree.empty")}
        </p>
      )}
      {menuPos && <ContextMenu x={menuPos.x} y={menuPos.y} onClose={() => setMenuPos(null)} items={menuItems} />}
      {promptDialog}
    </div>
  );
}

/* ================================================================
 * FileTree — root component
 * ================================================================ */
export function FileTree({
  onSelect,
  selectedPath,
  onAddToChat,
}: {
  onSelect: (path: string, name: string) => void;
  selectedPath: string | null;
  onAddToChat?: (path: string) => void;
}) {
  const { api } = useApi();
  const { t } = useTranslation();
  const { toast } = useToast();
  const { confirm, confirmDialog } = useConfirmDialog();
  const { prompt: promptDialogFn, promptDialog } = usePromptDialog();
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const [sharedFiles, setSharedFiles] = useState<FileEntry[]>([]);
  const [agentFiles, setAgentFiles] = useState<FileEntry[]>([]);
  const [error, setError] = useState("");
  const [refreshKey, setRefreshKey] = useState(0);
  const [gitFiles, setGitFiles] = useState<Record<string, string>>({});
  const [breadcrumb] = useState<string[]>([]);
  const [selectedFiles, setSelectedFiles] = useState<Set<string>>(new Set());
  const [dragOver, setDragOver] = useState(false);
  const [dropDir, setDropDir] = useState("shared");
  const [workspaceRoot, setWorkspaceRoot] = useState("");
  const [compareBase, setCompareBase] = useState<string | null>(null);
  const [compareDialog, setCompareDialog] = useState<{
    left: string;
    right: string;
    leftContent: string;
    rightContent: string;
  } | null>(null);
  const [timelineFile, setTimelineFile] = useState<string | null>(null);
  const [timelineData, setTimelineData] = useState<{
    blame: Array<{ author: string; count: number }>;
    commits: Array<{ hash: string; message: string; time: string; author: string }>;
  }>({ blame: [], commits: [] });

  useEffect(() => {
    api<Record<string, string>>("/api/v1/settings").then((s) => setWorkspaceRoot(s.workspace || ""))
      .catch((e) => console.warn("FileTree: workspace root load failed:", e));
  }, [api]);

  const load = useCallback(() => {
    Promise.all([
      api<FileEntry[]>("/api/v1/workspace/files").catch(() => [] as FileEntry[]),
      api<FileEntry[]>("/api/v1/workspace/files?dir=agents").catch(() => [] as FileEntry[]),
      api<{ files: Record<string, string> }>("/api/v1/workspace/files/git-status")
        .then((r) => setGitFiles(r.files || {}))
        .catch((e) => console.warn("FileTree: git status load failed:", e)),
    ])
      .then(([s, a]) => {
        setSharedFiles(s);
        setAgentFiles(a);
        setError("");
      })
      .catch(() => setError(t("common.loading")));
  }, [api, t]);

  useEffect(() => {
    load();
  }, [load, refreshKey]);
  const refresh = () => setRefreshKey((k) => k + 1);
  // Phase 47: watchdog file_changed WS→auto-refresh FileTree(对标 VSCode fs.watch)
  const wc = useAppStore((s) => s.fileWatchCounter);
  useEffect(() => {
    if (wc > 0) refresh();
  }, [wc]);

  const createItem = async (isDir: boolean) => {
    const name = await promptDialogFn({
      title: isDir ? t("fileTree.newFolder") : t("fileTree.newFile"),
      placeholder: isDir ? t("fileTree.dirNamePrompt") : t("fileTree.fileNamePrompt"),
    });
    if (!name) return;
    try {
      if (isDir)
        await api("/api/v1/workspace/files/mkdir", {
          method: "POST",
          body: JSON.stringify({ path: `shared/${name}` }),
        });
      else
        await api("/api/v1/workspace/files/save", {
          method: "POST",
          body: JSON.stringify({ path: `shared/${name}`, content: "" }),
        });
      refresh();
    } catch (e) {
      // P0-9 (2026-07-04): 旧实现仅 console.error——用户看不到失败原因。
      // 新实现：toast 红色提示 + 区分 ApiError 友好消息 vs 未知错误。
      const msg = e instanceof ApiError ? e.message : t("fileTree.createFailed");
      toast(msg, "error");
    }
  };

  // 上传实现见 hooks/useWorkspaceUpload（逐文件、原始字节、上限预检）
  const { handleUpload } = useWorkspaceUpload({ api, refresh, t, toast, formatSize: fmtSize });

  const handleDragOver = (e: React.DragEvent) => {
    // 仅处理 OS 文件拖入上传——内部文件移动由 DirNode 自行处理
    if (!e.dataTransfer.types.includes("Files")) return;
    e.preventDefault();
    setDragOver(true);
    const target = e.currentTarget as HTMLElement;
    const dirAttr = target.closest("[data-dir]")?.getAttribute("data-dir");
    if (dirAttr) setDropDir(dirAttr);
  };
  const handleDragLeave = (e: React.DragEvent) => {
    const target = e.currentTarget as HTMLElement;
    if (e.relatedTarget && !target.contains(e.relatedTarget as Node)) {
      setDragOver(false);
    }
  };
  const handleDrop = async (e: React.DragEvent) => {
    e.preventDefault();
    setDragOver(false);
    const files = e.dataTransfer.files;
    // 内部文件移动（无 OS 文件）由 DirNode 处理——此处仅处理上传
    if (!files || files.length === 0) return;
    await handleUpload(files, dropDir);
  };

  const deleteSelected = async () => {
    if (selectedFiles.size === 0) return;
    const ok = await confirm({
      title: t("common.confirm"),
      description: t("fileTree.batchDeleteConfirm", { count: selectedFiles.size }),
      variant: "destructive",
    });
    if (!ok) return;
    try {
      await api("/api/v1/workspace/files/batch", {
        method: "DELETE",
        body: JSON.stringify({ paths: [...selectedFiles] }),
      });
      setSelectedFiles(new Set());
      refresh();
    } catch {
      toast(t("fileTree.batchDeleteFailed"), "error");
    }
  };

  const toggleSelect = (path: string) => {
    const next = new Set(selectedFiles);
    if (next.has(path)) next.delete(path);
    else next.add(path);
    setSelectedFiles(next);
  };

  const openInTerminal = (dirPath: string) => {
    const rel = dirPath.replace(/\\/g, "/").split("/shared/").pop() || "";
    if (rel) {
      useAppStore.getState().sendTerminalCommand(`cd "shared/${rel}"`);
    }
  };

  const handleCompareWith = async (rightPath: string) => {
    if (!compareBase) return;
    try {
      // 2026-09-15 修复：compare 读取同样走工作区相对路径（原先丢 shared/ 前缀 → 404）
      const [leftRel, rightRel] = [compareBase, rightPath].map((p) => toWorkspaceRelative(p, workspaceRoot));
      const [l, r] = await Promise.all([
        api<{ content: string }>(`/api/v1/workspace/files/read?path=${encodeURIComponent(leftRel)}`),
        api<{ content: string }>(`/api/v1/workspace/files/read?path=${encodeURIComponent(rightRel)}`),
      ]);
      setCompareDialog({
        left: compareBase,
        right: rightPath,
        leftContent: l.content,
        rightContent: r.content,
      });
      setCompareBase(null);
    } catch {
      // ignore
    }
  };

  const handleShowTimeline = async (relPath: string) => {
    setTimelineFile(relPath);
    setTimelineData({ blame: [], commits: [] });
    try {
      const r = await api<{
        blame?: Array<{ author: string; count: number }>;
        commits?: Array<{ hash: string; message: string; time: string; author: string }>;
      }>(`/api/v1/git/file-timeline?path=${encodeURIComponent(relPath)}`);
      setTimelineData({
        blame: r.blame || [],
        commits: r.commits || [],
      });
    } catch {
      // ignore
    }
  };

  return (
    <div
      className="h-full flex flex-col bg-background"
      onDragOver={handleDragOver}
      onDragLeave={handleDragLeave}
      onDrop={handleDrop}
    >
      {dragOver && (
        <div className="absolute inset-0 bg-primary/10 border-2 border-dashed border-primary z-10 flex items-center justify-center rounded-lg pointer-events-none">
          <span className="text-sm font-semibold text-primary">{t("fileTree.dropToUpload", { dir: dropDir })}</span>
        </div>
      )}
      {/* Header */}
      <div className="px-3 py-1.5 flex items-center justify-between shrink-0 text-[11px] text-muted-foreground">
        <span className="text-[10px] font-semibold text-muted-foreground uppercase tracking-wider">
          {t("chat.files")}
        </span>
        <div className="flex gap-0.5">
          <input
            ref={fileInputRef}
            type="file"
            multiple
            onChange={(e) => {
              handleUpload(e.target.files);
              e.target.value = "";
            }}
            className="hidden"
          />
          <button
            onClick={() => fileInputRef.current?.click()}
            className="w-5 h-5 flex items-center justify-center hover:bg-surface-3 rounded text-muted-foreground"
            title={t("fileTree.uploadFile")}
          >
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
              <path d="M12 3v12M8 7l4-4 4 4" />
              <path d="M4 14v4a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-4" />
            </svg>
          </button>
          <button
            onClick={() => createItem(false)}
            className="w-5 h-5 flex items-center justify-center hover:bg-surface-3 rounded text-muted-foreground"
            title={t("fileTree.newFile")}
          >
            +
          </button>
          <button
            onClick={() => createItem(true)}
            className="w-5 h-5 flex items-center justify-center hover:bg-surface-3 rounded text-muted-foreground"
            title={t("fileTree.newFolder")}
            aria-label={t("fileTree.newFolder")}
          >
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
              <path d="M12 5v14M5 12h14" />
              <path d="M2 6a2 2 0 0 1 2-2h5l2 2h9a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V6z" opacity="0.3" />
            </svg>
          </button>
          <button
            onClick={refresh}
            className="w-5 h-5 flex items-center justify-center hover:bg-surface-3 rounded text-muted-foreground"
            title={t("common.refresh")}
            aria-label={t("common.refresh")}
          >
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
              <path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8" />
              <path d="M21 3v5h-5" />
              <path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16" />
              <path d="M3 21v-5h5" />
            </svg>
          </button>
        </div>
      </div>
      {/* Selection toolbar */}
      {selectedFiles.size > 0 && (
        <div className="px-2 py-1 bg-primary/5 flex items-center gap-2 text-[10px] shrink-0">
          <span>{t("fileTree.nSelected", { count: selectedFiles.size })}</span>
          <button onClick={deleteSelected} className="text-destructive hover:text-destructive/80 font-medium">
            {t("fileTree.delete")}
          </button>
          <button onClick={() => setSelectedFiles(new Set())} className="text-muted-foreground">
            {t("common.cancel")}
          </button>
        </div>
      )}

      {breadcrumb.length > 0 && (
        <div className="px-2 py-1 text-[10px] text-muted-foreground flex items-center gap-0.5 overflow-x-auto shrink-0">
          {breadcrumb.map((b, i) => (
            // eslint-disable-next-line react/no-array-index-key
            <span key={b + ":" + i} className="flex items-center gap-0.5">
              {i > 0 && <span>/</span>}
              <span className="hover:text-primary cursor-pointer truncate max-w-[80px]">{b}</span>
            </span>
          ))}
        </div>
      )}

      {/* Tree */}
      <div className="flex-1 overflow-y-auto py-1">
        {error && <p className="text-[10px] text-destructive px-2 py-1">{error}</p>}
        <div className="text-[9px] font-semibold text-muted-foreground px-2 py-0.5">{t("fileTree.sectionShared")}</div>
        {sharedFiles.length === 0 ? (
          <p className="text-[10px] text-muted-foreground px-2 py-0.5">{t("fileTree.empty")}</p>
        ) : (
          sharedFiles.map((f) => (
            <DirNode
              key={f.path}
              entry={f}
              depth={0}
              selectedPath={selectedPath}
              onSelect={onSelect}
              onRefresh={refresh}
              api={api}
              gitFiles={gitFiles}
              selectedFiles={selectedFiles}
              onToggleSelect={toggleSelect}
              onAddToChat={onAddToChat}
              onOpenTerminal={openInTerminal}
              onShowTimeline={handleShowTimeline}
              compareBase={compareBase}
              onSetCompareBase={setCompareBase}
              onCompareWith={handleCompareWith}
              workspaceRoot={workspaceRoot}
              t={t}
            />
          ))
        )}
        <div className="border-t border-border my-1" />
        <div className="text-[9px] font-semibold text-muted-foreground px-2 py-0.5 flex items-center gap-1">
          {t("fileTree.sectionAgents")}{" "}
          <span className="text-[7px] bg-warning/10 text-warning px-1 rounded">{t("fileTree.readOnly")}</span>
        </div>
        {agentFiles.length === 0 ? (
          <p className="text-[10px] text-muted-foreground px-2 py-0.5">{t("fileTree.empty")}</p>
        ) : (
          agentFiles.map((f) => (
            <DirNode
              key={f.path}
              entry={f}
              readOnly
              depth={0}
              selectedPath={selectedPath}
              onSelect={onSelect}
              onRefresh={refresh}
              api={api}
              gitFiles={gitFiles}
              selectedFiles={selectedFiles}
              onToggleSelect={toggleSelect}
              onAddToChat={onAddToChat}
              onOpenTerminal={openInTerminal}
              onShowTimeline={handleShowTimeline}
              compareBase={compareBase}
              onSetCompareBase={setCompareBase}
              onCompareWith={handleCompareWith}
              workspaceRoot={workspaceRoot}
              t={t}
            />
          ))
        )}
      </div>
      <FileCompareDialog
        open={!!compareDialog}
        onClose={() => setCompareDialog(null)}
        left={compareDialog?.left || ""}
        right={compareDialog?.right || ""}
        leftContent={compareDialog?.leftContent || ""}
        rightContent={compareDialog?.rightContent || ""}
      />
      <FileTimelineDialog
        open={!!timelineFile}
        onClose={() => setTimelineFile(null)}
        filePath={timelineFile}
        data={timelineData}
      />
      {confirmDialog}
      {promptDialog}
    </div>
  );
}
