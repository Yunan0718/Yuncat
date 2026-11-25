import { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useNavigate } from "react-router-dom";
import Fuse from "fuse.js";
import { useAppStore } from "../store/appStore";
import { useTaskStore } from "../store/taskStore";
import { useApi } from "../hooks/useApi";
import { useTranslation } from "react-i18next";
import { FocusTrap } from "./FocusTrap";
import { useToast } from "../hooks/use-toast";
import { NAV_ROUTES } from "../lib/navigation";
// Task 2.11: 统一快捷键读取层——支持用户自定义 close_overlay
import { matchShortcut } from "../lib/shortcuts";

type Category = "page" | "action" | "workflow" | "runWorkflow" | "help";

interface CommandItem {
  id: string;
  label: string;
  category: Category;
  action: (() => void) | (() => Promise<void>);
  keywords?: string;
}

const CATEGORY_I18N_KEY: Record<Category, string> = {
  page: "cmd.catPage",
  action: "cmd.catAction",
  workflow: "cmd.catWorkflow",
  runWorkflow: "cmd.catRunWorkflow",
  help: "cmd.catHelp",
};

/**
 * Phase 7: 全局命令面板 (Ctrl+K)
 *
 * - fuse.js 模糊搜索
 * - 页面导航 + 工作流搜索 + 帮助
 * - Enter 执行，Esc 关闭
 */
export function CommandPalette() {
  const { t } = useTranslation();
  const { toast } = useToast();
  const open = useAppStore((s) => s.commandPaletteOpen);
  const setOpen = useAppStore((s) => s.setCommandPaletteOpen);
  const navigate = useNavigate();
  const { api } = useApi();
  const backendReady = useAppStore((s) => s.backendReady);
  const inputRef = useRef<HTMLInputElement>(null);
  const [query, setQuery] = useState("");
  const [selectedIdx, setSelectedIdx] = useState(0);
  const [workflows, setWorkflows] = useState<Array<{ id: string; name: string }>>([]);

  // 加载工作流列表
  useEffect(() => {
    if (!backendReady || !open) return;
    api<Array<{ id: string; name: string }>>("/api/v1/workflows")
      .then(setWorkflows)
      .catch((e) => console.warn("CommandPalette: workflows load failed:", e));
  }, [backendReady, open, api]);

  const workflowItems: CommandItem[] = workflows.map((w) => ({
    id: `wf-${w.id}`,
    label: w.name,
    category: "workflow" as const,
    action: () => navigate("/workflows"),
    keywords: `${t("nav.workflows")} ${w.name}`,
  }));

  // Phase 17: 每个工作流增加"运行"命令——直接执行工作流
  const runWorkflowItems: CommandItem[] = workflows.map((w) => ({
    id: `run-wf-${w.id}`,
    label: t("cmd.runWorkflowLabel", { name: w.name }),
    category: "runWorkflow" as const,
    action: async () => {
      const tok = useAppStore.getState().token;
      const pt = useAppStore.getState().port;
      if (!tok || !pt) return;
      try {
        await fetch(`http://127.0.0.1:${pt}/api/v1/workflows/${w.id}/run`, {
          method: "POST",
          headers: { Authorization: `Bearer ${tok}`, "Content-Type": "application/json" },
          body: JSON.stringify({ initial_input: "" }),
        });
        navigate(`/workflows?run=${w.id}`);
      } catch {
        // P1: 用户主动触发工作流失败必须反馈
        toast(t("cmd.runWorkflowFailed"), "error");
      }
    },
    keywords: `${t("cmd.catRunWorkflow")} ${w.name}`,
  }));

  // 构建命令列表（staticItems 移入 useMemo，避免依赖数组每次渲染都变化）
  const allItems = useMemo(() => {
    const staticItems: CommandItem[] = [
      // Phase 17: 常用操作优先
      {
        id: "new-chat",
        label: t("chat.newChat"),
        category: "action",
        action: () => {
          const store = useTaskStore.getState();
          store.clearTasks();
          const tok = useAppStore.getState().token;
          const pt = useAppStore.getState().port;
          if (tok && pt) {
            fetch(`http://127.0.0.1:${pt}/api/v1/sessions/new`, {
              method: "POST",
              headers: { Authorization: `Bearer ${tok}` },
            })
              .then((r) => r.json())
              .then((d: { session_id: string }) => {
                store.setCurrentSession(d.session_id);
                store.addSession({
                  session_id: d.session_id,
                  msg_count: 0,
                  last_at: Math.floor(Date.now() / 1000),
                  agents: [],
                  title: t("chat.newChat"),
                  preview: "",
                });
              })
              .catch(() => toast(t("common.newChatFailed"), "error"));
          }
          navigate("/");
        },
        keywords: t("cmd.newChatKeyword"),
      },
      {
        id: "open-supervisor",
        label: t("cmd.openSupervisor"),
        category: "action",
        action: () => useAppStore.getState().setSupervisorDrawerOpen(true),
        keywords: t("cmd.supervisorKeyword"),
      },
      {
        id: "toggle-bottom-panel",
        label: t("cmd.toggleBottomPanel"),
        category: "action",
        action: () => {
          const store = useAppStore.getState();
          store.setWorkspaceBottomPanel(store.workspaceBottomPanel ? null : "terminal");
        },
        keywords: t("cmd.panelKeyword"),
      },
      {
        id: "toggle-sidebar",
        label: t("chat.toggleSidebar"),
        category: "action",
        action: () => useAppStore.getState().toggleNavSidebar(),
        keywords: t("cmd.panelKeyword"),
      },
      {
        id: "new-window",
        label: t("cmd.newWindow"),
        category: "action",
        action: () => { void window.yuncat?.newWindow?.(); },
        keywords: t("cmd.newWindowKeyword"),
      },
      {
        id: "open-file",
        label: t("cmd.openFile"),
        category: "action",
        action: () => navigate("/workspace"),
        keywords: t("cmd.fileKeyword"),
      },
      // 页面导航全部从统一的 lib/navigation 派生，避免 ⌘K 与侧栏/设置入口漂移
      ...NAV_ROUTES.map((r) => ({
        id: `nav-${r.path}`,
        label: t(r.labelKey),
        category: "page" as const,
        action: () => navigate(r.path),
        keywords: t(r.labelKey),
      })),
      { id: "nav-settings", label: t("nav.settings"), category: "page", action: () => navigate("/settings") },
      {
        id: "help-shortcuts",
        label: t("cmd.shortcuts"),
        category: "help",
        action: () => {
          // 2026-09-15：原实现用 innerHTML 拼了一个 z-index:9999 的浮层 + 内联 onclick——
          // ①绕过 z-index 命名层级；②在 CSP（script-src 'self'，无 unsafe-inline）下
          // 内联事件被拦，关闭按钮与点击遮罩全部失效，用户会被卡在遮罩里。
          // 改为复用已有的可访问对话框原语（ShortcutReferenceDialog）。
          const store = useAppStore.getState();
          store.setCommandPaletteOpen(false);
          store.setShortcutHelpOpen(true);
        },
        keywords: t("cmd.shortcuts"),
      },
    ];
    return [...staticItems, ...workflowItems, ...runWorkflowItems];
  }, [workflowItems, runWorkflowItems, t, navigate, toast]);

  // fuse.js 模糊搜索
  const fuse = useMemo(
    () => new Fuse(allItems, { keys: ["label", "keywords", "category"], threshold: 0.4 }),
    [allItems],
  );
  const results = query ? fuse.search(query).map((r) => r.item) : allItems;

  // 分组
  const grouped = results.reduce(
    (acc, item) => {
      (acc[item.category] ??= []).push(item);
      return acc;
    },
    {} as Record<Category, CommandItem[]>,
  );

  // 自动聚焦 + 重置
  useEffect(() => {
    if (open) {
      setQuery("");
      setSelectedIdx(0);
      const t = setTimeout(() => inputRef.current?.focus(), 50);
      return () => clearTimeout(t);
    }
  }, [open]);

  const execute = async (item: CommandItem) => {
    await item.action();
    setOpen(false);
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    // Task 2.11: close_overlay 用统一注册表——支持用户自定义关闭组合键
    if (e.key === "Escape" || matchShortcut(e, "close_overlay")) {
      setOpen(false);
      return;
    }
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setSelectedIdx((i) => Math.min(i + 1, results.length - 1));
      return;
    }
    if (e.key === "ArrowUp") {
      e.preventDefault();
      setSelectedIdx((i) => Math.max(i - 1, 0));
      return;
    }
    if (e.key === "Enter" && results[selectedIdx]) {
      execute(results[selectedIdx]);
    }
  };

  if (!open) return null;

  const categoryOrder: Category[] = ["action", "page", "workflow", "runWorkflow", "help"];

  return createPortal(
    <div
      className="fixed inset-0 bg-black/50 z-modal-top flex items-start justify-center pt-[20vh]"
      onClick={() => setOpen(false)}
      role="dialog"
      aria-modal="true"
      aria-label={t("cmd.palette")}
    >
      <FocusTrap active={open} initialFocusRef={inputRef}>
        <div
          className="bg-background border rounded shadow-sm w-[500px] max-h-[400px] flex flex-col"
          onClick={(e) => e.stopPropagation()}
        >
          <input
            ref={inputRef}
            value={query}
            onChange={(e) => {
              setQuery(e.target.value);
              setSelectedIdx(0);
            }}
            onKeyDown={handleKeyDown}
            placeholder={t("cmd.searchPlaceholder")}
            aria-label={t("cmd.searchPlaceholder")}
            className="px-4 py-3 text-sm border-b bg-transparent outline-none"
          />
          <div className="flex-1 overflow-y-auto p-1">
            {categoryOrder.map((cat) => {
              const items = grouped[cat];
              if (!items || items.length === 0) return null;
              return (
                <div key={cat}>
                  <div className="text-[10px] font-semibold text-muted-foreground px-3 py-1 uppercase">
                    {t(CATEGORY_I18N_KEY[cat])}
                  </div>
                  {items.map((item) => {
                    const idx = results.indexOf(item);
                    const isSelected = idx === selectedIdx;
                    return (
                      <button
                        key={item.id}
                        onClick={() => execute(item)}
                        className={`w-full text-left px-3 py-1.5 text-sm rounded transition-colors ${isSelected ? "bg-primary-muted text-primary font-semibold" : "hover:bg-secondary"}`}
                      >
                        {item.label}
                      </button>
                    );
                  })}
                </div>
              );
            })}
            {results.length === 0 && (
              <p className="text-sm text-muted-foreground text-center py-4">{t("cmd.noMatch")}</p>
            )}
          </div>
          <div className="border-t px-3 py-1.5 flex gap-4 text-[10px] text-muted-foreground">
            <span>{t("cmd.navHint")}</span>
            <span>{t("cmd.enterHint")}</span>
            <span>{t("cmd.escHint")}</span>
          </div>
        </div>
      </FocusTrap>
    </div>,
    document.getElementById("overlay-root")!,
  );
}
