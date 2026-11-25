import { useCallback, useEffect, useRef, useState } from "react";
import { ChevronDown, FolderOpen } from "lucide-react";
import { useApi } from "../hooks/useApi";
import { useWebSocket } from "../hooks/useWebSocket";
import { useToast } from "../hooks/use-toast";
import { useAppStore } from "../store/appStore";
import { useTaskStore } from "../store/taskStore";
import { FileTree } from "../components/workspace/FileTree";
import { MonacoEditor } from "../components/workspace/MonacoEditor";
import { TerminalPanel, type TerminalHandle } from "../components/workspace/TerminalPanel";
import { GitPanel } from "../components/GitPanel";
import { InlineDiff } from "../components/workspace/InlineDiff";
import Fuse from "fuse.js";
import {
  loadPersistedTabs,
  previewKind,
  supportsRenderedPreview,
  supportsTablePreview,
  isDocumentPreview,
  isLegacyOfficeFile,
  toWorkspacePath,
  type WorkspaceTab,
} from "../lib/workspaceTabs";
import { MediaPreview } from "../components/workspace/MediaPreview";
import { DocumentPreview } from "../components/workspace/DocumentPreview";
import { ProblemsPanel } from "../components/workspace/ProblemsPanel";
import { WorkspaceOverlays } from "../components/workspace/WorkspaceOverlays";
import { renderMarkdownHtml } from "../lib/markdownHtml";
import { useWorkspaceFileSave } from "../hooks/useWorkspaceFileSave";
import { useWorkspaceTabPersistence } from "../hooks/useWorkspaceTabPersistence";
import { useTranslation } from "react-i18next";
import { useConfirmDialog } from "../hooks/useConfirmDialog";

type Tab = WorkspaceTab;
let _tabCounter = 0;

const RECENT_FILES_KEY = "yuncat-workspace-recent-files";
const RECENT_FILES_MAX = 10;

function loadRecentFiles(): Array<{ path: string; name: string; ts: number }> {
  try {
    const raw = localStorage.getItem(RECENT_FILES_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.slice(0, RECENT_FILES_MAX);
  } catch {
    return [];
  }
}

function saveRecentFile(path: string, name: string) {
  try {
    const list = loadRecentFiles().filter((f) => f.path !== path);
    list.unshift({ path, name, ts: Date.now() });
    localStorage.setItem(RECENT_FILES_KEY, JSON.stringify(list.slice(0, RECENT_FILES_MAX)));
  } catch {
    // ignore
  }
}


/** Phase 13: 工作区页面（Monaco 多标签 + FileTree + AI 编辑 + 终端 + Git + Ctrl+P + 搜索） */
export function WorkspacePage() {
  const { t } = useTranslation();
  const { api, apiBlob } = useApi();
  const { submitTask, cancelTask } = useWebSocket();
  const { toast } = useToast();
  const { confirm, confirmDialog } = useConfirmDialog();
  const backendReady = useAppStore((s) => s.backendReady);
  const token = useAppStore((s) => s.token);
  const capabilityLevel = useAppStore((s) => s.capabilityLevel);
  const reasoningEffort = useAppStore((s) => s.reasoningEffort);
  // Phase 49-O: 行内补全开关（默认启用，可通过设置关闭）
  const [inlineCompletion, setInlineCompletion] = useState(() => {
    try {
      return localStorage.getItem("yuncat-editor-inlineCompletion") !== "false";
    } catch {
      return true;
    }
  });

  // ---- 多标签状态 ----
  // 2026-09-15：标签页跨路由保留。原实现只活在组件内存里——从「工作区」切到
  // 「对话」再回来，所有打开的文件（含未保存编辑）全部消失，属于静默数据丢失。
  const [restored] = useState(() => loadPersistedTabs(() => ++_tabCounter));
  const [tabs, setTabs] = useState<Tab[]>(() => restored?.tabs ?? []);
  const [activeIdx, setActiveIdx] = useState(() => restored?.activeIdx ?? 0);
  const [statusMsg, setStatusMsg] = useState("");
  // Phase 98: bottomPanel 提升到 appStore——启动时恢复上次打开的底部面板
  const bottomPanel = useAppStore((s) => s.workspaceBottomPanel);
  const setBottomPanel = useAppStore((s) => s.setWorkspaceBottomPanel);
  const terminalRef = useRef<TerminalHandle>(null);
  const activeTab = tabs[activeIdx];
  const dirty = activeTab ? activeTab.content !== activeTab.originalContent : false;

  // ---- WS.3: 最近文件列表（localStorage 持久化） ----
  const [recentFiles, setRecentFiles] = useState<Array<{ path: string; name: string; ts: number }>>(() => loadRecentFiles());
  const [showRecent, setShowRecent] = useState(false);

  // ---- Ctrl+P 快速文件打开 ----
  const [showQuickOpen, setShowQuickOpen] = useState(false);
  const [quickOpenQuery, setQuickOpenQuery] = useState("");
  const [quickOpenResults, setQuickOpenResults] = useState<Array<{ path: string; name: string }>>([]);
  const [allPaths, setAllPaths] = useState<Array<{ path: string; name: string }>>([]);

  // ---- 跨文件搜索 ----
  const [showSearch, setShowSearch] = useState(false);
  const [searchQuery, setSearchQuery] = useState("");
  const [searchResults, setSearchResults] = useState<Array<{ file: string; line: number; text: string }>>([]);
  const [searching, setSearching] = useState(false);

  // 问题面板点击后要定位到的行（传给 MonacoEditor）
  const [gotoLine, setGotoLine] = useState<number | undefined>(undefined);
  const diagnosticsByFile = useAppStore((s) => s.diagnosticsByFile);
  const problemCount = Object.values(diagnosticsByFile).reduce(
    (n, items) => n + (items?.length || 0),
    0,
  );

  // Markdown / HTML：源码（Monaco）与渲染预览之间切换；切标签时回到源码
  const [renderedPreview, setRenderedPreview] = useState(false);
  useEffect(() => {
    setRenderedPreview(false);
  }, [activeTab?.path]);

  // ---- AI 编辑 ----
  const [aiInstruction, setAiInstruction] = useState("");
  const [aiRunning, setAiRunning] = useState(false);
  const [aiTaskId, setAiTaskId] = useState<string | null>(null);
  // Batch D H10: 替代 `const taskStore = useTaskStore()` —— 全订阅导致流式输出时频繁 re-render。
  // 只订阅 aiTaskId 对应的 task（需要实时监听流式 chunk）。
  const aiTask = useTaskStore((s) => (aiTaskId ? s.tasks[aiTaskId] : undefined));
  const [aiPreview, setAiPreview] = useState<string | null>(null);
  // Task 2.7: AI 编辑目标 tab path——按 path 隔离而非 activeIdx。
  // 旧实现 acceptAiEdit 用 activeIdx 应用结果，AI 期间用户切 tab 会把结果应用到错误 tab。
  // 主流做法（Cursor / VS Code inline edit）：AI 编辑绑定到发起的文件，与当前活动 tab 解耦。
  const [aiTargetPath, setAiTargetPath] = useState<string | null>(null);
  // Task 2.7: AI 编辑目标 tab——按 path 查找而非 activeIdx，AI 期间切 tab 不影响目标定位
  const aiTargetTab = tabs.find((t) => t.path === aiTargetPath);
  const aiTimerRef = useRef<number>(0);
  useEffect(
    () => () => {
      clearTimeout(aiTimerRef.current);
    },
    [],
  );

  // 持久化行内补全开关到 localStorage
  useEffect(() => {
    try {
      localStorage.setItem("yuncat-editor-inlineCompletion", String(inlineCompletion));
    } catch {
      // ignore
    }
  }, [inlineCompletion]);

  // 加载设置（内联补全开关）
  useEffect(() => {
    if (!backendReady) return;
    api<Record<string, string>>("/api/v1/settings")
      .then((s) => {
        if (s.inline_completion === "false") setInlineCompletion(false);
      })
      .catch((e) => console.warn("WorkspacePage: inline_completion load failed:", e));
  }, [backendReady, api]);

  // 加载文件列表（供 Ctrl+P 使用）
  useEffect(() => {
    if (!backendReady) return;
    api<Array<{ path: string; name: string }>>("/api/v1/workspace/files")
      .then((files) => setAllPaths(files.map((f) => ({ path: f.path, name: f.name }))))
      .catch((e) => console.warn("WorkspacePage: file paths load failed:", e));
  }, [backendReady, api]);

  // 历史标签恢复 + 落盘——实现在 hooks/useWorkspaceTabPersistence
  useWorkspaceTabPersistence({ backendReady, restored, tabs, activeIdx, api, setTabs });

  /** 从问题面板打开文件并定位到行列（对标 VS Code 点问题直接跳转） */
  const openAtLine = useCallback(
    (path: string, line: number) => {
      const name = path.replace(/\\/g, "/").split("/").pop() || path;
      setGotoLine(line);
      openFile(path, name);
    },
    // openFile 定义在下方，依赖列表里用它的引用会在首次渲染后稳定
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );

  // ---- Tab 操作 ----
  const openFile = useCallback(
    async (path: string, name: string) => {
      // WS.3: 记录到最近文件
      saveRecentFile(path, name);
      setRecentFiles(loadRecentFiles());
      // 如果已打开，切过去
      const existing = tabs.findIndex((t) => t.path === path);
      if (existing >= 0) {
        setActiveIdx(existing);
        return;
      }
      const key = ++_tabCounter;
      // 媒体文件按预览打开：跳过文本读取（否则会被二进制嗅探拦成"只读"），交给 MediaPreview
      const mediaKind = previewKind(name || path);
      if (mediaKind) {
        setTabs((prev) => [
          ...prev,
          { key, path, name, content: "", originalContent: "", loading: false, preview: mediaKind },
        ]);
        setActiveIdx(tabs.length);
        setAiPreview(null);
        setAiInstruction("");
        setAiTaskId(null);
        return;
      }
      const newTab: Tab = { key, path, name, content: "", originalContent: "", loading: true };
      setTabs((prev) => [...prev, newTab]);
      const idx = tabs.length;
      setActiveIdx(idx);
      setAiPreview(null);
      setAiInstruction("");
      setAiTaskId(null);
      try {
        const rel = toWorkspacePath(path);
        const res = await api<{ content: string; modified?: number; binary?: boolean }>(
          `/api/v1/workspace/files/read?path=${encodeURIComponent(rel)}`,
        );
        setTabs((prev) =>
          prev.map((t) =>
            t.key === key
              ? {
                  ...t,
                  content: res.content,
                  originalContent: res.content,
                  loading: false,
                  mtime: res.modified,
                  binary: res.binary === true,
                }
              : t,
          ),
        );
        if (res.binary) setStatusMsg(t("workspacePage.binaryFileHint", { name }));
      } catch {
        setStatusMsg(t("workspacePage.readFileError"));
        setTabs((prev) => prev.map((t) => (t.key === key ? { ...t, loading: false } : t)));
      }
    },
    [api, tabs, t],
  );

  // v66: 从聊天中点击文件路径 → 自动搜索并打开
  const pendingFile = useAppStore((s) => s.pendingWorkspaceFile);
  const clearPendingFile = useAppStore((s) => s.setPendingWorkspaceFile);
  useEffect(() => {
    if (!pendingFile || !backendReady) return;
    clearPendingFile(null);
    // 尝试在已加载的路径中匹配
    const match = allPaths.find((f) => f.name === pendingFile || f.path.endsWith(pendingFile));
    if (match) {
      openFile(match.path, match.name);
    }
    // 如果没匹配到，刷新文件列表后再找
    api<Array<{ path: string; name: string }>>("/api/v1/workspace/files")
      .then((files) => {
        setAllPaths(files.map((f) => ({ path: f.path, name: f.name })));
        const m = files.find((f) => f.name === pendingFile);
        if (m) openFile(m.path, m.name);
      })
      .catch((e) => console.warn("WorkspacePage: pending file load failed:", e));
  }, [pendingFile, backendReady, clearPendingFile, allPaths, api, openFile]);

  // P1 (2026-07-04): closeTab 修复——旧实现关闭标签时不取消正在运行的 AI 编辑任务，
  // 导致后台 AI 任务仍在跑 + 30s watchdog 仍挂起（资源泄漏）。
  // Task 2.7: 用 path 判断关闭的 tab 是否是 AI 编辑目标——原 idx===activeIdx 在切 tab 后失效。
  // 新实现：若关闭的是 AI 编辑目标的 tab，调 cancelTask 取消 + 清 watchdog。
  const closeTab = async (idx: number) => {
    const closingTab = tabs[idx];
    if (!closingTab) return;
    // 2026-09-15：未保存的改动必须先确认。原实现直接丢弃 + 事后弹一条 info toast，
    // 手写的代码会静默消失（关标签是最容易误触的动作之一）。
    if (closingTab.content !== closingTab.originalContent) {
      const ok = await confirm({
        title: t("workspacePage.closeUnsavedTitle", { name: closingTab.name }),
        description: t("workspacePage.closeUnsavedDesc"),
        confirmText: t("common.close"),
        cancelText: t("common.cancel"),
        variant: "destructive",
      });
      if (!ok) return;
    }
    // 若关闭的是 AI 编辑目标的 tab，取消后端任务 + 清 watchdog
    if (aiTaskId && closingTab && closingTab.path === aiTargetPath) {
      cancelTask(aiTaskId).catch(() => toast(t("common.aiEditCancelFailed"), "error"));
      clearTimeout(aiTimerRef.current);
      setAiRunning(false);
      setAiTaskId(null);
      setAiPreview(null);
      setAiTargetPath(null);
      setStatusMsg("");
    }
    setTabs((prev) => prev.filter((_, i) => i !== idx));
    setActiveIdx((a) => (a >= idx && a > 0 ? a - 1 : 0));
  };

  const updateContent = (v: string) => {
    setTabs((prev) => prev.map((t, i) => (i === activeIdx ? { ...t, content: v } : t)));
  };

  // 保存（含「文件已被外部修改」的 409 处理）——实现在 hooks/useWorkspaceFileSave
  const saveFile = useWorkspaceFileSave({
    api, activeTab, activeIdx, dirty, setTabs, setStatusMsg, confirm, t,
  });

  // 运行 —— Phase 17: 打开终端并自动输入命令
  const runFile = useCallback(() => {
    if (!activeTab) return;
    const ext = activeTab.name.split(".").pop()?.toLowerCase();
    const cmd =
      ext === "py" ? `python "${activeTab.path}"` : ext === "js" ? `node "${activeTab.path}"` : `"${activeTab.path}"`;
    setBottomPanel("terminal");
    // 延迟等终端打开后发送命令
    setTimeout(() => terminalRef.current?.sendCommand(cmd), 300);
    // setBottomPanel 是 zustand selector 取出的 action，引用恒定，列入依赖安全。
  }, [activeTab, setBottomPanel]);

  // ---- AI 编辑 ----
  // P0-4 (2026-07-04): 修复 race condition + 添加 cancel + watchdog 超时
  // 旧实现用 beforeIds 集合差集识别新 task，并发提交时误识别；
  // 新实现用唯一 marker 嵌入 prompt，subscribe 中按 marker 匹配 task。
  // 旧 rejectAiEdit 只清前端不取消后端任务；新实现调 cancelTask(api)。
  // 旧实现无超时——若 task_created 永远不到则 UI 卡死；新实现 30s watchdog。
  const submitAiEdit = useCallback(() => {
    if (!aiInstruction.trim() || !activeTab) return;
    setAiRunning(true);
    setStatusMsg(t("workspacePage.aiGenerating"));
    setAiPreview(null);
    // Task 2.7: 记录 AI 编辑目标 tab path——后续 accept/reject/closeTab 按 path 定位
    setAiTargetPath(activeTab.path);
    // P0-4: 唯一 marker 嵌入 prompt 头部——subscribe 按 marker 精确匹配
    const aiMarker = `__yuncat_ai_edit_${Date.now()}_${Math.random().toString(36).slice(2, 8)}__`;
    const prompt = `${aiMarker}\n【文件】${activeTab.path}\n【当前内容】\n${activeTab.content.slice(0, useTaskStore.getState().contextUsage.budgets?.tool_result_limit || 4000)}\n\n【用户指令】${aiInstruction}\n\n请仅输出修改后的完整文件内容，不要任何解释。`;
    try {
      submitTask(prompt, undefined, undefined, "agent", undefined, capabilityLevel, reasoningEffort);
      setAiInstruction("");
      clearTimeout(aiTimerRef.current);
      let resolved = false;
      const unsub = useTaskStore.subscribe((state) => {
        if (resolved) return;
        for (const [id, t] of Object.entries(state.tasks)) {
          if (t.prompt && t.prompt.startsWith(aiMarker) && t.status === "running") {
            resolved = true;
            setAiTaskId(id);
            unsub();
            return;
          }
        }
      });
      // P0-4: 30s watchdog——若 task_created 永不到（WS 断/后端异常）则重置 UI
      aiTimerRef.current = window.setTimeout(() => {
        if (!resolved) {
          resolved = true;
          unsub();
          setStatusMsg(t("workspacePage.aiEditFailed"));
          setAiRunning(false);
        }
      }, 30000);
    } catch {
      setStatusMsg(t("workspacePage.aiEditFailed"));
      setAiRunning(false);
    }
  }, [aiInstruction, activeTab, submitTask, t, capabilityLevel, reasoningEffort]);

  // AI 结果监听——Phase 18: 流式实时预览
  // Batch D H10: 用 aiTask selector 替代 taskStore.tasks[aiTaskId]——避免全订阅触发 re-render。
  useEffect(() => {
    if (!aiTaskId || !aiTask) return;
    const task = aiTask;
    if (task.status === "completed") {
      const result = task.chunks.join("");
      if (result.trim()) {
        setAiPreview(result);
        setStatusMsg(t("workspacePage.aiEditComplete"));
      }
      setAiRunning(false);
      setAiTaskId(null);
    } else if (task.status === "failed" || task.status === "cancelled") {
      setStatusMsg(t("workspacePage.aiEditFailed"));
      setAiRunning(false);
      setAiTaskId(null);
      // Task 2.7: 失败/取消时清 AI 目标 path——避免残留状态指向已无效的 tab
      setAiTargetPath(null);
    } else if (task.chunks.length > 0) {
      // 流式输出中——实时展示 AI 正在写的内容
      setAiPreview(task.chunks.join(""));
      const totalLen = task.chunks.reduce((sum, c) => sum + c.length, 0);
      setStatusMsg(t("workspacePage.aiStreaming", { size: Math.round(totalLen / 1024) }));
    }
  }, [aiTaskId, aiTask, t]);

  // Task 2.7: acceptAiEdit 按 aiTargetPath 定位 tab——避免 AI 期间切 tab 后结果应用到错误 tab
  const acceptAiEdit = () => {
    if (!aiPreview || !aiTargetPath) return;
    setTabs((prev) => prev.map((t) => (t.path === aiTargetPath ? { ...t, content: aiPreview } : t)));
    setAiPreview(null);
    setAiTargetPath(null);
    setStatusMsg(t("workspacePage.aiApplied"));
  };
  // P0-4: rejectAiEdit 调用 cancelTask 取消后端任务——不再仅清前端
  // P0-10: cancelTask 失败时反馈 toast——避免用户以为取消成功但后端任务仍在跑
  // Task 2.7: 清 aiTargetPath——避免残留状态
  const rejectAiEdit = useCallback(() => {
    if (aiTaskId) {
      cancelTask(aiTaskId).catch(() => toast(t("common.aiEditCancelFailed"), "error"));
    }
    setAiPreview(null);
    setAiRunning(false);
    setAiTaskId(null);
    setAiTargetPath(null);
    setStatusMsg("");
    clearTimeout(aiTimerRef.current);
  }, [aiTaskId, cancelTask, toast, t]);

  // ---- 键盘快捷键 ----
  useEffect(() => {
    const h = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key === "s") {
        e.preventDefault();
        saveFile();
      }
      if ((e.ctrlKey || e.metaKey) && e.key === "p") {
        e.preventDefault();
        setShowQuickOpen(true);
      }
      if ((e.ctrlKey || e.metaKey) && e.shiftKey && e.key === "F") {
        e.preventDefault();
        setShowSearch(true);
      }
      // WS.3: Ctrl+R 显示最近文件
      if ((e.ctrlKey || e.metaKey) && e.key === "r" && !e.shiftKey) {
        e.preventDefault();
        setRecentFiles(loadRecentFiles());
        setShowRecent(true);
      }
    };
    document.addEventListener("keydown", h);
    return () => document.removeEventListener("keydown", h);
  }, [saveFile]);

  // ---- Ctrl+P 模糊搜索 ----
  useEffect(() => {
    if (!showQuickOpen || quickOpenQuery.length < 1) {
      setQuickOpenResults(allPaths.slice(0, 20));
      return;
    }
    const fuse = new Fuse(allPaths, { keys: ["name", "path"], threshold: 0.4 });
    setQuickOpenResults(fuse.search(quickOpenQuery).map((r) => r.item));
  }, [quickOpenQuery, showQuickOpen, allPaths]);

  // ---- 跨文件搜索 ----
  const doSearch = useCallback(async () => {
    if (!searchQuery.trim()) return;
    setSearching(true);
    try {
      const res = await api<{ results: Array<{ file: string; line: number; text: string }> }>(
        `/api/v1/workspace/files/search?q=${encodeURIComponent(searchQuery)}`,
      );
      setSearchResults(res.results);
    } catch {
      setSearchResults([]);
    }
    setSearching(false);
  }, [searchQuery, api]);

  if (!backendReady) return <div className="p-8 text-muted-foreground">{t("legacy.wf_276d")}</div>;

  return (
    <div className="h-[calc(100vh-48px)] flex">
      {/* 左侧文件树 */}
      <div className="w-56 shrink-0 border-r">
        <FileTree onSelect={openFile} selectedPath={activeTab?.path ?? null} />
      </div>

      {/* 右侧主区域 */}
      <div className="flex-1 min-w-0 flex flex-col">
        {statusMsg && <div className="px-3 py-1 text-xs bg-secondary border-b font-mono">{statusMsg}</div>}

        {/* 标签栏 */}
        <div className="h-8 border-b flex items-center bg-secondary shrink-0 overflow-x-auto" role="tablist">
          {tabs.map((tab, i) => (
            <div
              key={tab.key}
              role="tab"
              tabIndex={0}
              aria-selected={i === activeIdx}
              onClick={() => setActiveIdx(i)}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  setActiveIdx(i);
                }
              }}
              className={`flex items-center gap-1 px-2 py-0.5 text-xs cursor-pointer border-r shrink-0 max-w-[180px] transition-colors ${
                i === activeIdx
                  ? "bg-background border-b-2 border-b-accent font-semibold"
                  : "hover:bg-background text-muted-foreground"
              }`}
            >
              <span className="truncate">{tab.name}</span>
              {tab.content !== tab.originalContent && <span className="text-warning text-[10px]">●</span>}
              <button
                type="button"
                aria-label={`${t("common.close")} ${tab.name}`}
                onClick={(e) => {
                  e.stopPropagation();
                  closeTab(i);
                }}
                className="text-muted-foreground hover:text-destructive ml-0.5"
              >
                ×
              </button>
            </div>
          ))}
          {tabs.length === 0 && (
            <span className="text-xs text-muted-foreground px-3">{t("workspacePage.noOpenFile")}</span>
          )}
        </div>

        {/* AI 编辑栏 */}
        {activeTab && (
          <div className="h-8 border-b px-2 flex items-center gap-2 shrink-0">
            <span className="text-[10px] text-muted-foreground font-semibold">{t("workspacePage.aiLabel")}</span>
            <input
              value={aiInstruction}
              onChange={(e) => setAiInstruction(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && submitAiEdit()}
              placeholder={t("workspacePage.aiPlaceholder")}
              className="flex-1 px-2 py-0.5 text-xs border rounded bg-background"
              disabled={aiRunning}
            />
            <button
              onClick={submitAiEdit}
              disabled={!aiInstruction.trim() || aiRunning}
              className="px-2 py-0.5 text-[10px] bg-primary text-primary-foreground rounded font-semibold disabled:opacity-50"
            >
              {aiRunning ? t("workspacePage.aiSubmitting") : t("workspacePage.aiSubmit")}
            </button>
            {(supportsRenderedPreview(activeTab.name) || supportsTablePreview(activeTab.name)) && (
              <button
                type="button"
                data-testid="toggle-rendered-preview"
                onClick={() => setRenderedPreview((v) => !v)}
                className="px-2 py-0.5 text-[10px] border border-input rounded hover:bg-secondary"
              >
                {renderedPreview ? t("workspacePage.showSource") : t("workspacePage.showPreview")}
              </button>
            )}
          </div>
        )}

        {/* 编辑器区 */}
        <div className="flex-1 min-h-0">
          {activeTab ? (
            activeTab.loading ? (
              <div className="flex items-center justify-center text-sm text-muted-foreground h-full">
                {t("legacy.git_26b5")}
              </div>
            ) : activeTab.preview ? (
              isDocumentPreview(activeTab.preview) ? (
                <DocumentPreview
                  apiBlob={apiBlob}
                  path={activeTab.path}
                  name={activeTab.name}
                  kind={activeTab.preview}
                />
              ) : (
                <MediaPreview
                  apiBlob={apiBlob}
                  path={activeTab.path}
                  name={activeTab.name}
                  kind={activeTab.preview}
                />
              )
            ) : activeTab.binary ? (
              <div className="flex flex-col items-center justify-center text-center space-y-2 h-full px-6">
                <FolderOpen size={40} strokeWidth={1.2} className="text-muted-foreground/40" />
                <p className="text-sm text-muted-foreground max-w-md">
                  {t("workspacePage.binaryFileHint", { name: activeTab.name })}
                </p>
                {isLegacyOfficeFile(activeTab.name) && (
                  <p className="text-[11px] text-warning">{t("documentPreview.legacyOffice")}</p>
                )}
                <p className="text-[11px] text-muted-foreground/80">
                  {t("mediaPreview.openExternalHint")}
                </p>
              </div>
            ) : renderedPreview && supportsRenderedPreview(activeTab.name) ? (
              activeTab.name.toLowerCase().endsWith(".html") ||
              activeTab.name.toLowerCase().endsWith(".htm") ? (
                <MediaPreview apiBlob={apiBlob} path={activeTab.path} name={activeTab.name} kind="html" />
              ) : (
                <div
                  className="markdown-body h-full overflow-auto px-4 py-3 text-sm"
                  dangerouslySetInnerHTML={{ __html: renderMarkdownHtml(activeTab.content) }}
                />
              )
            ) : renderedPreview && supportsTablePreview(activeTab.name) ? (
              <DocumentPreview
                name={activeTab.name}
                path={activeTab.path}
                kind="spreadsheet"
                text={activeTab.content}
              />
            ) : (
              <MonacoEditor
                filePath={activeTab.path}
                content={activeTab.content}
                onChange={updateContent}
                onSave={saveFile}
                onRun={runFile}
                dirty={dirty}
                token={token || ""}
                enableInlineCompletion={inlineCompletion}
                gotoLine={gotoLine}
              />
            )
          ) : (
            <div className="flex flex-col items-center justify-center text-center space-y-3 h-full">
              <FolderOpen size={48} strokeWidth={1.2} className="text-muted-foreground/40" />
              <h2 className="text-lg font-semibold">{t("workspacePage.emptyTitle")}</h2>
              <p className="text-sm text-muted-foreground max-w-md">
                {t("workspacePage.emptyHint", { ctrlP: "Ctrl+P", ctrlShiftF: "Ctrl+Shift+F" })}
              </p>
            </div>
          )}
        </div>

        {/* Phase 18: AI 流式输出面板——编辑过程中实时预览
            Task 2.7: preview 绑定到 aiTargetTab（AI 发起时的 tab）而非 activeTab——
            避免 AI 期间切 tab 后 preview 显示在错误 tab 上 */}
        {aiRunning && aiPreview && aiTargetTab && (
          <div className="shrink-0 mx-2 mb-1 border rounded-lg diff-block max-h-[40vh] flex flex-col">
            <div className="flex items-center px-3 py-1.5 diff-block-header rounded-t-lg">
              <span className="text-[11px] diff-meta font-mono">
                {t("workspacePage.aiGeneratingPanel")}{aiTargetTab.name !== activeTab?.name ? ` · ${aiTargetTab.name}` : ""}
              </span>
              <div className="flex-1" />
              <button onClick={rejectAiEdit} className="text-[10px] text-destructive hover:text-destructive/80">
                {t("workspacePage.cancel")}
              </button>
            </div>
            <div className="overflow-y-auto p-3 flex-1">
              <pre className="text-[13px] text-foreground font-mono whitespace-pre-wrap leading-relaxed">
                {aiPreview}
              </pre>
            </div>
          </div>
        )}

        {/* AI Diff 面板——生成完成后展示对比
            Task 2.7: diff 对比绑定到 aiTargetTab.content——避免切 tab 后对比错误文件 */}
        {!aiRunning && aiPreview && aiTargetTab && (
          <div className="shrink-0 mx-2 mb-1">
            <InlineDiff
              oldContent={aiTargetTab.content}
              newContent={aiPreview}
              onAccept={acceptAiEdit}
              onReject={rejectAiEdit}
              title={t("workspacePage.aiSuggestionTitle", { name: aiTargetTab.name })}
            />
          </div>
        )}

        {/* 底部面板：终端 / Git */}
        <div className="border-t shrink-0 bg-secondary">
          <div className="h-7 flex items-center px-3 gap-1">
            <button
              onClick={() => setBottomPanel(bottomPanel === "terminal" ? null : "terminal")}
              className={`text-[10px] px-2 py-0.5 rounded transition-colors ${
                bottomPanel === "terminal"
                  ? "bg-background font-semibold"
                  : "text-muted-foreground hover:text-foreground"
              }`}
            >
              {t("legacy.term_4722")}
            </button>
            <button
              onClick={() => setBottomPanel(bottomPanel === "git" ? null : "git")}
              className={`text-[10px] px-2 py-0.5 rounded transition-colors ${
                bottomPanel === "git" ? "bg-background font-semibold" : "text-muted-foreground hover:text-foreground"
              }`}
            >
              {t("workspacePage.git")}
            </button>
            <button
              data-testid="problems-tab"
              onClick={() => setBottomPanel(bottomPanel === "problems" ? null : "problems")}
              className={`text-[10px] px-2 py-0.5 rounded transition-colors inline-flex items-center gap-1 ${
                bottomPanel === "problems"
                  ? "bg-background font-semibold"
                  : "text-muted-foreground hover:text-foreground"
              }`}
            >
              {t("workspacePage.problemsTab")}
              {problemCount > 0 && (
                <span className="px-1 rounded bg-destructive/15 text-destructive">{problemCount}</span>
              )}
            </button>
            <div className="flex-1" />
            {bottomPanel && (
              <button
                onClick={() => setBottomPanel(null)}
                className="text-muted-foreground hover:text-foreground text-[10px]"
              >
                <ChevronDown size={12} />
              </button>
            )}
          </div>
        </div>
        {bottomPanel === "terminal" && (
          <TerminalPanel ref={terminalRef} collapsed={false} onToggle={() => setBottomPanel(null)} />
        )}
        {bottomPanel === "git" && <GitPanel />}
        {bottomPanel === "problems" && <ProblemsPanel onOpen={openAtLine} />}
      </div>

      <WorkspaceOverlays
        showQuickOpen={showQuickOpen}
        setShowQuickOpen={setShowQuickOpen}
        quickOpenQuery={quickOpenQuery}
        setQuickOpenQuery={setQuickOpenQuery}
        quickOpenResults={quickOpenResults}
        openFile={openFile}
        showRecent={showRecent}
        setShowRecent={setShowRecent}
        recentFiles={recentFiles}
        showSearch={showSearch}
        setShowSearch={setShowSearch}
        searchQuery={searchQuery}
        setSearchQuery={setSearchQuery}
        searching={searching}
        searchResults={searchResults}
        doSearch={doSearch}
        t={t}
      />
      {confirmDialog}
    </div>
  );
}
