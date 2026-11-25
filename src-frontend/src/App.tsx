import { useEffect, lazy, Suspense, useState, useRef, useCallback } from "react";
import { useTranslation } from "react-i18next";
import { Routes, Route, Navigate, useNavigate, useLocation } from "react-router-dom";
import { AriaLiveRegion } from "./components/AriaLiveRegion";
import { SkipLink } from "./components/common/SkipLink";
import { TooltipProvider } from "./components/ui/tooltip";
import { Sheet, SheetContent } from "./components/ui/sheet";
import { StartupScreen } from "./pages/StartupScreen";
import { Sidebar } from "./components/Sidebar";
import { TitleBar } from "./components/TitleBar";
import { CommandPalette } from "./components/CommandPalette";
import { OnboardingWizard } from "./components/OnboardingWizard";
import { CrashRecoveryDialog } from "./components/CrashRecoveryDialog";
import { UserQuestionDialog } from "./components/chat/UserQuestionDialog";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { ChatView } from "./components/chat/ChatView";
import { ToastProvider } from "./components/common/Toast";
import { emitGlobalToast } from "./components/common/toast-context";
import { useTaskStore } from "./store/taskStore";
import { useBackendReady } from "./hooks/useBackendReady";
import { useWebSocket } from "./hooks/useWebSocket";
import { useHealthCheck } from "./hooks/useHealthCheck";
import { useNotification } from "./hooks/useNotification";
import { useAppStore } from "./store/appStore";
import { isKnownRoute, ROUTE_TITLE_KEYS } from "./lib/navigation";
import { LoadingCurve } from "./components/common/LoadingCurve";
import { StatusBar } from "./components/StatusBar";
import { RightContextPanel } from "./components/RightContextPanel";
// Task 2.11: 统一快捷键读取层——替代硬编码 e.key === "k"/"n"
import { matchShortcut, isTypingElement } from "./lib/shortcuts";
import { ShortcutReferenceDialog } from "./components/settings/ShortcutReferenceDialog";
// P1: Promise-based 确认对话框——用于 Ctrl+N 清空运行中任务前的二次确认
import { useConfirmDialog } from "./hooks/useConfirmDialog";

const SettingsPage = lazy(() => import("./pages/SettingsPage").then((m) => ({ default: m.SettingsPage })));
const AgentManager = lazy(() => import("./pages/AgentManager").then((m) => ({ default: m.AgentManager })));
const WorkflowEditor = lazy(() => import("./pages/WorkflowEditor").then((m) => ({ default: m.WorkflowEditor })));
const MemoryExplorer = lazy(() => import("./pages/MemoryExplorer").then((m) => ({ default: m.MemoryExplorer })));
const Dashboard = lazy(() => import("./pages/Dashboard").then((m) => ({ default: m.Dashboard })));
const ArenaPage = lazy(() => import("./pages/ArenaPage").then((m) => ({ default: m.ArenaPage })));
const EvolutionPendingPage = lazy(() =>
  import("./pages/EvolutionPendingPage").then((m) => ({ default: m.EvolutionPendingPage })),
);
const PromptManager = lazy(() => import("./pages/PromptManager").then((m) => ({ default: m.PromptManager })));
const McpManager = lazy(() => import("./pages/McpManager").then((m) => ({ default: m.McpManager })));
const TaskCenter = lazy(() => import("./pages/TaskCenter").then((m) => ({ default: m.TaskCenter })));
const WorkspacePage = lazy(() => import("./pages/WorkspacePage").then((m) => ({ default: m.WorkspacePage })));
const SkillMarket = lazy(() => import("./components/SkillMarket").then((m) => ({ default: m.SkillMarket })));

function PageLoader() {
  return (
    <div className="flex items-center justify-center h-64">
      <LoadingCurve variant="lissajous" size={48} particleCount={12} />
    </div>
  );
}

export function App() {
  const { t } = useTranslation();
  useBackendReady();
  useWebSocket();
  useHealthCheck();
  useNotification();

  const backendReady = useAppStore((s) => s.backendReady);
  const theme = useAppStore((s) => s.theme);
  const uiDensity = useAppStore((s) => s.uiDensity);
  const uiFontSize = useAppStore((s) => s.uiFontSize);
  const accentHue = useAppStore((s) => s.accentHue);
  const toggleCommandPalette = useAppStore((s) => s.toggleCommandPalette);
  // Phase 69: 右侧上下文面板全局化——所有路由共享
  const rightCollapsed = useAppStore((s) => s.rightCollapsed);
  const setRightCollapsed = useAppStore((s) => s.setRightCollapsed);
  const navigate = useNavigate();
  const location = useLocation();
  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  // 2026-09-15：快捷键参考弹窗状态提升到 appStore——命令面板（Ctrl+K）与 Shift+? 共用同一入口
  const shortcutHelpOpen = useAppStore((s) => s.shortcutHelpOpen);
  const setShortcutHelpOpen = useAppStore((s) => s.setShortcutHelpOpen);
  // P1: Ctrl+N 清空运行中任务前的二次确认——防止误按丢失进行中的任务
  const { confirm, confirmDialog } = useConfirmDialog();

  // Phase 98: 会话恢复——首次挂载恢复上次路由（onboarding 未完成时全屏遮罩，恢复安全）
  const routeRestoreDone = useRef(false);
  useEffect(() => {
    if (routeRestoreDone.current) return;
    routeRestoreDone.current = true;
    const lastRoute = useAppStore.getState().lastRoute;
    // 校验路由有效——避免恢复到旧版本的不存在路由（统一来自 lib/navigation）
    if (lastRoute && lastRoute !== "/" && lastRoute !== location.pathname && isKnownRoute(lastRoute)) {
      navigate(lastRoute, { replace: true });
    }
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // Phase 98: 路由变化时保存到 localStorage（供下次启动恢复）
  useEffect(() => {
    const setLastRoute = useAppStore.getState().setLastRoute;
    setLastRoute(location.pathname);
  }, [location.pathname]);

  const accentClass =
    accentHue === 140
      ? "theme-green"
      : accentHue === 270
        ? "theme-purple"
        : accentHue === 25
          ? "theme-orange"
          : "theme-blue";

  // Phase 49-B: 路由变化时同步 document.title（标题来源统一为 navigation.ts 的 ROUTE_TITLE_KEYS）
  useEffect(() => {
    const titleKey = ROUTE_TITLE_KEYS[location.pathname] || "app.title";
    const suffix = t(titleKey);
    document.title = suffix === t("app.title") ? t("app.title") : `${t("app.title")} - ${suffix}`;
  }, [location.pathname, t]);

  // Theme sync
  useEffect(() => {
    const html = document.documentElement;
    const apply = () => {
      const t = useAppStore.getState().theme;
      if (t === "dark") {
        html.classList.add("dark");
        window.yuncat?.setTitleBarTheme?.("dark");
        return;
      }
      if (t === "light") {
        html.classList.remove("dark");
        window.yuncat?.setTitleBarTheme?.("light");
        return;
      }
      const isDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
      html.classList.toggle("dark", isDark);
      window.yuncat?.setTitleBarTheme?.(isDark ? "dark" : "light");
    };
    apply();
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const listener = () => {
      if (useAppStore.getState().theme === "system") apply();
    };
    mq.addEventListener("change", listener);
    return () => mq.removeEventListener("change", listener);
  }, [theme]);

  // Phase 49-G: accent hue sync
  useEffect(() => {
    const html = document.documentElement;
    html.classList.remove("theme-blue", "theme-green", "theme-purple", "theme-orange");
    html.classList.add(accentClass);
  }, [accentClass]);

  // Phase 49-G: density + font size sync
  useEffect(() => {
    document.documentElement.dataset.density = uiDensity;
  }, [uiDensity]);
  useEffect(() => {
    document.documentElement.style.setProperty("--ui-font-size", `${uiFontSize}px`);
  }, [uiFontSize]);

  // Tray actions：IPC 监听器只需注册一次

  // Batch 4a F7: 主进程 IPC 意图接线——全局快捷键 new-chat / yuncat:// deep-link
  // 转发为 window CustomEvent，由 ChatView 的 attachChatIntents 消费（同 yuncat-insert-input 桥接模式）
  useEffect(() => {
    const unNewChat = window.yuncat?.onNewChat?.(() => {
      window.dispatchEvent(new CustomEvent("yuncat:new-chat"));
    });
    const unDeepLink = window.yuncat?.onDeepLink?.((payload: { prompt: string; agent: string }) => {
      if (import.meta.env.DEV) console.log("[deep-link]", payload);
      window.dispatchEvent(new CustomEvent("yuncat:deep-link", { detail: payload }));
    });
    // 系统通知点击 → 导航到对应内容（link 为前端路由，如 /chat?task=xxx）
    const unNotifClick = window.yuncat?.onNotificationClick?.((link: string) => {
      if (link) navigate(link);
    });
    return () => {
      unNewChat?.();
      unDeepLink?.();
      unNotifClick?.();
    };
    // navigate 来自 useNavigate()，引用稳定；列入依赖后即使将来它变化，
    // 也只是解绑重绑三个 IPC 监听器，代价可忽略，但语义上是正确的。
  }, [navigate]);

  // P1: 新建工作流——清空运行中任务前二次确认，防止误按 Ctrl+N 丢失进行中的任务。
  // 主流做法（VSCode/Cursor）：Ctrl+N 不破坏现有状态；此处保留"新建即清空"语义，
  // 但当存在 running/paused/interrupted 任务时弹确认对话框（对标 SessionList 删除确认）。
  const startNewWorkflow = useCallback(async () => {
    const store = useTaskStore.getState();
    const hasInProgress = Object.values(store.tasks).some(
      (task) => task.status === "running" || task.status === "paused" || task.status === "interrupted",
    );
    if (hasInProgress) {
      const ok = await confirm({
        title: t("common.newWorkflowConfirmTitle"),
        description: t("common.newWorkflowConfirmDesc"),
        variant: "destructive",
        confirmText: t("chat.newChat"),
        cancelText: t("common.cancel"),
      });
      if (!ok) return;
    }
    store.clearTasks();
    const { token, port } = useAppStore.getState();
    if (token && port) {
      fetch(`http://127.0.0.1:${port}/api/v1/sessions/new`, {
        method: "POST",
        headers: { Authorization: `Bearer ${token}` },
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
        .catch(() => emitGlobalToast(t("common.newChatFailed"), "error"));
    }
    navigate("/");
  }, [confirm, navigate, t]);

  // Shortcuts
  // Task 2.11: 通过 matchShortcut 读取 ShortcutEditor 配置的 overrides——原硬编码 "k"/"n" 永不读配置
  useEffect(() => {
    const h = (e: KeyboardEvent) => {
      if (matchShortcut(e, "command_palette")) {
        e.preventDefault();
        toggleCommandPalette();
      }
      if (matchShortcut(e, "new_window")) {
        e.preventDefault();
        void window.yuncat?.newWindow?.();
        return;
      }
      if (matchShortcut(e, "toggle_sidebar") && !isTypingElement(e.target)) {
        e.preventDefault();
        useAppStore.getState().toggleNavSidebar();
      }
      if (matchShortcut(e, "toggle_bottom_panel") && !isTypingElement(e.target)) {
        e.preventDefault();
        const store = useAppStore.getState();
        store.setWorkspaceBottomPanel(store.workspaceBottomPanel ? null : "terminal");
      }
      if (matchShortcut(e, "toggle_terminal") && !isTypingElement(e.target)) {
        e.preventDefault();
        const store = useAppStore.getState();
        store.setWorkspaceBottomPanel(store.workspaceBottomPanel === "terminal" ? null : "terminal");
      }
      if (matchShortcut(e, "new_workflow")) {
        e.preventDefault();
        void startNewWorkflow();
      }
      if (!shortcutHelpOpen && matchShortcut(e, "shortcut_help") && !isTypingElement(e.target)) {
        e.preventDefault();
        setShortcutHelpOpen(true);
      }
    };
    document.addEventListener("keydown", h);
    return () => document.removeEventListener("keydown", h);
  }, [toggleCommandPalette, startNewWorkflow, shortcutHelpOpen, setShortcutHelpOpen]);

  if (!backendReady) return <StartupScreen />;

  return (
    <ErrorBoundary resetKeys={[location.pathname]}>
      <ToastProvider>
        <TooltipProvider delay={300}>
          <AriaLiveRegion />
          <SkipLink targetId="main-content" label={t("a11y.skipToContent")} />
          <div className="h-screen bg-background text-foreground flex overflow-hidden">
            <div className="hidden md:block">
              <Sidebar />
            </div>
            <Sheet open={mobileNavOpen} onOpenChange={setMobileNavOpen}>
              {/* P0-6.4: 移动端 Sheet 响应式宽度——小屏全宽，sm+ 固定 260px */}
              <SheetContent side="left" className="p-0 w-full sm:w-[260px]">
                <Sidebar mobile onClose={() => setMobileNavOpen(false)} />
              </SheetContent>
            </Sheet>
            <div className="flex-1 flex flex-col min-w-0">
              <TitleBar onOpenMobileNav={() => setMobileNavOpen(true)} />
              <main id="main-content" tabIndex={-1} className="flex-1 min-h-0 min-w-0 outline-none">
                <Routes>
                  <Route
                    path="/"
                    element={
                      <ErrorBoundary>
                        <ChatView />
                      </ErrorBoundary>
                    }
                  />
                  <Route
                    path="/chat"
                    element={
                      <ErrorBoundary>
                        <Suspense fallback={<PageLoader />}>
                          <TaskCenter />
                        </Suspense>
                      </ErrorBoundary>
                    }
                  />

                  <Route
                    path="/settings"
                    element={
                      <ErrorBoundary>
                        <Suspense fallback={<PageLoader />}>
                          <SettingsPage />
                        </Suspense>
                      </ErrorBoundary>
                    }
                  />
                  <Route
                    path="/agents"
                    element={
                      <ErrorBoundary>
                        <Suspense fallback={<PageLoader />}>
                          <AgentManager />
                        </Suspense>
                      </ErrorBoundary>
                    }
                  />
                  <Route
                    path="/workflows"
                    element={
                      <ErrorBoundary>
                        <Suspense fallback={<PageLoader />}>
                          <WorkflowEditor />
                        </Suspense>
                      </ErrorBoundary>
                    }
                  />
                  <Route
                    path="/memories"
                    element={
                      <ErrorBoundary>
                        <Suspense fallback={<PageLoader />}>
                          <MemoryExplorer />
                        </Suspense>
                      </ErrorBoundary>
                    }
                  />
                  <Route
                    path="/dashboard"
                    element={
                      <ErrorBoundary>
                        <Suspense fallback={<PageLoader />}>
                          <Dashboard />
                        </Suspense>
                      </ErrorBoundary>
                    }
                  />
                  <Route
                    path="/arena"
                    element={
                      <ErrorBoundary>
                        <Suspense fallback={<PageLoader />}>
                          <ArenaPage />
                        </Suspense>
                      </ErrorBoundary>
                    }
                  />
                  <Route
                    path="/evolution-pending"
                    element={
                      <ErrorBoundary>
                        <Suspense fallback={<PageLoader />}>
                          <EvolutionPendingPage />
                        </Suspense>
                      </ErrorBoundary>
                    }
                  />
                  <Route
                    path="/prompts"
                    element={
                      <ErrorBoundary>
                        <Suspense fallback={<PageLoader />}>
                          <PromptManager />
                        </Suspense>
                      </ErrorBoundary>
                    }
                  />
                  <Route
                    path="/mcp"
                    element={
                      <ErrorBoundary>
                        <Suspense fallback={<PageLoader />}>
                          <McpManager />
                        </Suspense>
                      </ErrorBoundary>
                    }
                  />
                  <Route
                    path="/skills"
                    element={
                      <ErrorBoundary>
                        <Suspense fallback={<PageLoader />}>
                          <SkillMarket />
                        </Suspense>
                      </ErrorBoundary>
                    }
                  />
                  <Route
                    path="/workspace"
                    element={
                      <ErrorBoundary>
                        <Suspense fallback={<PageLoader />}>
                          <WorkspacePage />
                        </Suspense>
                      </ErrorBoundary>
                    }
                  />
                  <Route path="*" element={<Navigate to="/" replace />} />
                </Routes>
              </main>
              <StatusBar />
            </div>
            {/* Phase 69: RightContextPanel 全局化——所有路由可见，从 appStore 读 rightCollapsed */}
            {!rightCollapsed && <RightContextPanel onClose={() => setRightCollapsed(true)} />}
            <CommandPalette />
            <ShortcutReferenceDialog open={shortcutHelpOpen} onOpenChange={setShortcutHelpOpen} />
            <OnboardingWizard />
            {/* Phase 92: 崩溃恢复对话框——启动时检测上次未处理的 crash，弹窗提示 */}
            <CrashRecoveryDialog />
            <UserQuestionDialog />
            {/* P1: Ctrl+N 清空运行中任务前的二次确认对话框 */}
            {confirmDialog}
          </div>
        </TooltipProvider>
      </ToastProvider>
    </ErrorBoundary>
  );
}
