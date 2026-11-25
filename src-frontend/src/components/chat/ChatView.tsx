import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useLocation, useNavigate } from "react-router-dom";
import { useToast } from "../../hooks/use-toast";
import { useApi, isAbortedError } from "../../hooks/useApi";
import { useWebSocket } from "../../hooks/useWebSocket";
import { GoalClarificationDialog } from "../GoalClarificationDialog";
import { useAppStore } from "../../store/appStore";
import { useTaskStore } from "../../store/taskStore";
import { BottomPanel } from "../BottomPanel";
import {
  parseReferences,
  type ContextRef,
} from "../context-pills";
import { ContextPills } from "../ContextPills";
import { useIsMobile } from "../../hooks/useMediaQuery";
import { useResizablePanel } from "../../hooks/useResizablePanel";
import { switchSessionFromApi } from "../../utils/sessionRestore";
import { Sheet, SheetContent } from "@/components/ui/sheet";
import SpecView from "../SpecView";
import { useSpecStore } from "../../store/specStore";
import { DeliberationView } from "../../pages/DeliberationView";
import { useDeliberationStore } from "../../store/deliberationStore";
import { SearchPanel } from "../SearchPanel";
import { ChatToolbar } from "./ChatToolbar";
import { MessageList, type MessageListHandle } from "./MessageList";
import { InputArea } from "./InputArea";
import { useSecurityControl } from "../../hooks/useSecurityControl";
import { LeftPanel } from "./LeftPanel";
import { LoadingCurve } from "../common/LoadingCurve";
import { ToolConfirmDialog } from "../ToolConfirmDialog";
import { ToolConfirmBatchDialog } from "../ToolConfirmBatchDialog";
import { MessageContextMenu } from "./MessageContextMenu";
import { BridgeApprovalDialog } from "./BridgeApprovalDialog";
import { useTaskActions } from "./useTaskActions";
import { useSlashCommands } from "./useSlashCommands";
import { useAttachments } from "./useAttachments";
import { useSendTask } from "./useSendTask";
import { useSessionDraft } from "../../hooks/useSessionDraft";
import { useAtAutocomplete } from "./useAtAutocomplete";
import type { ChatMode, CtxMenuState } from "./types";
import type { VersionNode, VersionEdge } from "../MessageVersionTree";

// Phase 70: 导出纯函数供单元测试（不影响运行时行为）——对齐 Phase 66 TerminalPanel 模式
export function loadLeftCollapsed(): boolean {
  try {
    const stored = localStorage.getItem("yuncat-left-collapsed");
    return stored === null ? false : stored === "true";
  } catch {
    return false;
  }
}

/** Batch 4a F7: App 层 IPC→CustomEvent 桥接（yuncat:new-chat / yuncat:deep-link）的消费端。
 * App.tsx 订阅 onNewChat/onDeepLink 后 dispatch 对应 CustomEvent，此处注册监听并转发。
 * 导出纯注册函数供单元测试；返回解绑函数。 */
export function attachChatIntents(opts: {
  onNewChat: () => void;
  onDeepLink: (payload: { prompt?: string; agent?: string }) => void;
  target?: Pick<EventTarget, "addEventListener" | "removeEventListener">;
}): () => void {
  const target = opts.target ?? window;
  const handleNew = () => opts.onNewChat();
  const handleDeep = (e: Event) => {
    const detail = (e as CustomEvent<{ prompt?: string; agent?: string }>).detail;
    if (detail) opts.onDeepLink(detail);
  };
  target.addEventListener("yuncat:new-chat", handleNew);
  target.addEventListener("yuncat:deep-link", handleDeep);
  return () => {
    target.removeEventListener("yuncat:new-chat", handleNew);
    target.removeEventListener("yuncat:deep-link", handleDeep);
  };
}

export function ChatView() {
  const { t, i18n } = useTranslation();
  const { toast } = useToast();
  const { api } = useApi();
  const location = useLocation();
  const navigate = useNavigate();
  const { submitTask, steerTask, cancelTask, confirmTool, confirmToolBatch, sendGoalClarificationResponse, clearClarification } = useWebSocket();
  const backendReady = useAppStore((s) => s.backendReady);
  const backendDead = useAppStore((s) => s.backendDead);
  const wsPort = useAppStore((s) => s.port);
  const wsToken = useAppStore((s) => s.token) || "";
  // Batch D H8: 替代 `const store = useTaskStore()` —— 全订阅导致流式输出时频繁 re-render。
  // 拆分为窄选择器：action 函数（resolveConfirmDialog/setBridgeApproval/switchVersion）
  // 在 zustand 中为稳定引用，不会触发 re-render。
  const confirmDialog = useTaskStore((s) => s.confirmDialog);
  const bridgeApproval = useTaskStore((s) => s.bridgeApproval);
  const resolveConfirmDialog = useTaskStore((s) => s.resolveConfirmDialog);
  const setBridgeApproval = useTaskStore((s) => s.setBridgeApproval);
  const longTaskSuggestion = useTaskStore((s) => s.longTaskSuggestion);
  const setLongTaskSuggestion = useTaskStore((s) => s.setLongTaskSuggestion);
  // Phase 82: 长任务 Supervisor 驾驶舱入口——有运行/暂停的长任务时显示工具栏按钮
  const hasLongTask = useTaskStore((s) =>
    Object.values(s.tasks).some(
      (tk) => tk.isLongTask && (tk.status === "running" || tk.status === "paused"),
    ),
  );
  const setSupervisorActiveTask = useTaskStore((s) => s.setSupervisorActiveTask);
  const setSupervisorDrawerOpen = useAppStore((s) => s.setSupervisorDrawerOpen);
  const openSupervisor = useCallback(() => {
    // 2026-09-16: 以前没有跑着的长任务就直接 return，点了没反应；
    // 现在优先定位正在跑的，否则取最近一次长任务，再没有就打开空的驾驶舱
    const entries = Object.entries(useTaskStore.getState().tasks).filter(([, tk]) => tk.isLongTask);
    const live = (tk: { status?: string }) => (tk.status === "running" || tk.status === "paused" ? 1 : 0);
    entries.sort(
      (a, b) => live(b[1]) - live(a[1]) || (b[1].startedAt ?? 0) - (a[1].startedAt ?? 0),
    );
    if (entries.length > 0) setSupervisorActiveTask(entries[0][0]);
    setSupervisorDrawerOpen(true);
  }, [setSupervisorActiveTask, setSupervisorDrawerOpen]);
  const switchVersion = useTaskStore((s) => s.switchVersion);
  const setTaskParent = useTaskStore((s) => s.setTaskParent);
  const addTaskVersion = useTaskStore((s) => s.addTaskVersion);
  // 稳定 store 包装——供 useTaskActions + useSendTask 使用。tasks 通过 getter 实时读取，
  // 对象本身用 useMemo 保持引用稳定（不触发下游 hook 的 memo 重算）。
  const stableStore = useMemo(() => ({
    get tasks() { return useTaskStore.getState().tasks; },
    switchVersion,
    setTaskParent,
    addTaskVersion,
  }), [switchVersion, setTaskParent, addTaskVersion]);
  const currentSessionId = useTaskStore((s) => s.currentSessionId);
  const setCurrentSession = useTaskStore((s) => s.setCurrentSession);
  const setTaskSuperseded = useTaskStore((s) => s.setTaskSuperseded);
  const bumpSessionList = useTaskStore((s) => s.bumpSessionList);
  const pendingFile = useAppStore((s) => s.pendingWorkspaceFile);
  const setPendingWorkspaceFile = useAppStore((s) => s.setPendingWorkspaceFile);
  const pendingDelibPrompt = useAppStore((s) => s.pendingDeliberationPrompt);
  // Batch 4a F6: 原误绑 setPendingWorkspaceFile——自动讨论 prompt 消费后不清除
  // （同值不触发 effect 静默失效），且误清 pendingWorkspaceFile。改绑正确 action。
  const clearPendingDelibPrompt = useAppStore((s) => s.setPendingDeliberationPrompt);
  const capabilityLevel = useAppStore((s) => s.capabilityLevel);

  /* ── Editor ── */
  const [_statusMsg, setStatusMsg] = useState("");
  const [leftPanel, setLeftPanel] = useState<"files" | "sessions">("files");
  // Phase 70: leftCollapsed 从 localStorage 读取，默认展开（false）
  const [leftCollapsed, setLeftCollapsed] = useState<boolean>(() => loadLeftCollapsed());
  // v66: 编辑器已迁至 /workspace——聊天中打开文件改为跳转工作区（WorkspacePage 消费 pendingWorkspaceFile）
  const openInWorkspace = useCallback(
    (path: string) => {
      setPendingWorkspaceFile(path);
    },
    [setPendingWorkspaceFile],
  );

  // v66: 点击文件路径（ResultRenderer / LeftPanel / SearchPanel / diff 应用）→ 跳转工作区打开
  useEffect(() => {
    if (!pendingFile || !backendReady) return;
    navigate("/workspace");
  }, [pendingFile, backendReady, navigate]);
  // Phase 70: leftCollapsed 变化时持久化到 localStorage
  useEffect(() => {
    try {
      localStorage.setItem("yuncat-left-collapsed", String(leftCollapsed));
    } catch {
      /* ignore */
    }
  }, [leftCollapsed]);
  // Phase 70: 首次进入显示引导 toast（localStorage flag 标记已访问）
  useEffect(() => {
    try {
      if (localStorage.getItem("yuncat-chat-view-visited") === "true") return;
      localStorage.setItem("yuncat-chat-view-visited", "true");
      toast(t("phase70.welcomeToast"), "info");
    } catch {
      /* ignore */
    }
  }, [t, toast]);

  // Phase D H1.1: 通过 Sidebar 导航进入讨论模式
  useEffect(() => {
    if ((location.state as { openDeliberation?: boolean } | null)?.openDeliberation) {
      setMode("deliberation");
      navigate(location.pathname, { replace: true, state: {} });
    }
  }, [location.state, location.pathname, navigate]);

  // Phase 69: rightCollapsed 提升到 appStore——所有路由共享
  const rightCollapsed = useAppStore((s) => s.rightCollapsed);
  const toggleRightPanel = useAppStore((s) => s.toggleRightPanel);
  const [mobileLeftOpen, setMobileLeftOpen] = useState(false);

  /* ── Terminal ── */
  const [termCollapsed, setTermCollapsed] = useState(true);
  // P0-6.5: 终端面板可拖拽调整高度——用 useResizablePanel 替换手写拖拽代码
  // 持久化到 localStorage，重启后保留用户偏好（对标 VS Code Panel 高度持久化）
  const { size: termHeight, dragHandleProps: termDragProps } = useResizablePanel({
    storageKey: "yuncat.terminal.height",
    initialSize: Math.max(180, Math.floor(window.innerHeight * 0.25)),
    minSize: 100,
    maxSize: Math.floor(window.innerHeight * 0.6),
    side: "bottom",
  });

  /* ── Chat ── */
  const isMobile = useIsMobile(); // P0-6.3: 替代手写 innerWidth < 768 判断
  // P0-6.5: LeftPanel 可拖拽调整宽度——对标 VS Code Sidebar（170-512px）+ 持久化
  const { size: leftPanelWidth, dragHandleProps: leftPanelDragProps } = useResizablePanel({
    storageKey: "yuncat.leftPanel.width",
    initialSize: 220,
    minSize: 170,
    maxSize: 512,
    side: "left",
  });
  // P3-2: 草稿按会话持久化——实现与「切会话串草稿」的修复见 hooks/useSessionDraft
  const [input, setInput] = useSessionDraft(currentSessionId);
  // P0-5.1: 实时解析输入中的 @引用——在输入框下方显示 ContextPills（可删除/预览）
  const [inputRefs, setInputRefs] = useState<ContextRef[]>([]);
  useEffect(() => {
    setInputRefs(parseReferences(input));
  }, [input]);
  // P0-5.1: 删除引用时从 input 文本中移除对应 @标记
  const handleRemoveRef = useCallback(
    (ref: ContextRef) => {
      const newInput =
        input.slice(0, ref.startIndex) + input.slice(ref.startIndex + ref.raw.length);
      setInput(newInput.replace(/\s{2,}/g, " ").trimStart());
    },
    [input, setInput],
  );
  const handleAddToChat = useCallback(
    (path: string) => {
      const insert = `@file:${path} `;
      setInput((prev) => (prev.endsWith(" ") || prev === "" ? prev + insert : prev + " " + insert));
    },
    [setInput],
  );
  const [agents, setAgents] = useState<
    Array<{
      id: string;
      name: string;
      status?: string;
      kind?: string;
      external?: boolean;
      installed?: boolean;
      enabled?: boolean;
      detected?: boolean;
    }>
  >([]);
  const [selectedAgent, setSelectedAgent] = useState("");
  const [mode, setMode] = useState<ChatMode>("agent");
  const [delibRoles, setDelibRoles] = useState<Array<{ id: string; name: string; prompt_template: string }>>([]);
  const [delibPrompt, setDelibPrompt] = useState("");
  const [delibLoading, setDelibLoading] = useState(false);
  const specTaskId = useSpecStore((s) => s.taskId);
  const [editingTaskId, setEditingTaskId] = useState<string | null>(null);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const messageListRef = useRef<MessageListHandle>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [sessionLoading, setSessionLoading] = useState(false); // S5: session 切换骨架屏
  const [ctxMenu, setCtxMenu] = useState<CtxMenuState | null>(null);
  const [versionTrees, setVersionTrees] = useState<Record<string, { nodes: VersionNode[]; edges: VersionEdge[] }>>({});

  /* ── @ autocomplete ── Phase 43: 对标 Cursor @ 引用 —— P0-7: 抽取到 useAtAutocomplete hook ── */
  const { atMenu, setAtMenu, sessionsForAt, handleAtAutocomplete } = useAtAutocomplete({ api, backendReady });

  // 全局点击关闭上下文菜单，仅注册一次
  useEffect(() => {
    const h = () => setCtxMenu(null);
    document.addEventListener("click", h);
    return () => document.removeEventListener("click", h);
  }, []);

  useEffect(() => {
    bumpSessionList?.();
  }, [backendReady, bumpSessionList]);

  // Phase D G1.1: 进入讨论模式时加载内置角色模板
  useEffect(() => {
    if (mode !== "deliberation") {
      setDelibRoles([]);
      setDelibLoading(false);
      return;
    }
    if (delibRoles.length > 0 || delibLoading) return;
    setDelibLoading(true);
    // AI 自动触发时已有 pendingDelibPrompt，不覆盖；手动进入时用 input
    setDelibPrompt((prev) => prev || input.trim() || t("phase49.deliberation.defaultPrompt"));
    api<Array<{ id: string; name: string; prompt_template: string; category: string }>>("/api/v1/roles?category=builtin")
      .then((roles) => {
        const defaultIds = ["architect", "developer", "reviewer", "product", "coordinator"];
        const selected = roles
          .filter((r) => defaultIds.includes(r.id))
          .sort((a, b) => defaultIds.indexOf(a.id) - defaultIds.indexOf(b.id));
        setDelibRoles(selected);
      })
      .catch(() => {
        setDelibRoles([]);
      })
      .finally(() => setDelibLoading(false));
  }, [mode, input, delibRoles.length, delibLoading, api, t]);

  // AI 自动触发讨论——监听 pendingDelibPrompt，直接切换到 DeliberationView（无确认）
  useEffect(() => {
    if (!pendingDelibPrompt) return;
    setDelibPrompt(pendingDelibPrompt);
    setMode("deliberation");
    clearPendingDelibPrompt(null);
  }, [pendingDelibPrompt, clearPendingDelibPrompt]);

  /* ── Data loading ── */
  useEffect(() => {
    if (!backendReady) return;
    api<typeof agents>("/api/v1/agents")
      .then(setAgents)
      .catch((e) => {
        if (isAbortedError(e)) return;
        console.warn("ChatView: agents load failed:", e);
        toast(t("chat.loadAgentsFailed"), "error");
      });
  }, [backendReady, api, toast, t]);

  useEffect(() => {
    if (!backendReady) return;
    api<Record<string, string>>("/api/v1/settings")
      .then((s) => {
        if (s.default_agent) setSelectedAgent(s.default_agent);
      })
      .catch((e) => {
        if (isAbortedError(e)) return;
        console.warn("ChatView: settings load failed:", e);
        toast(t("chat.loadSettingsFailed"), "error");
      });
  }, [backendReady, api, toast, t]);

  // 权限档位（权限 + 审批打包成一个选择）：对话区与设置页共用同一个 hook
  const { preset: securityPreset, change: changeSecurityPreset } = useSecurityControl(api, () =>
    toast(t("chat.approvalSaveFailed"), "error"),
  );

  const restoreSession = useCallback(
    async (sid: string) => {
      setSessionLoading(true);
      try {
        // Batch 4a F1: 竞态防护的恢复——快速切换时慢的旧响应不覆盖新会话
        await switchSessionFromApi(sid, api);
      } catch (e) {
        console.warn(`ChatView: restoreSession failed for ${sid}:`, e);
      } finally {
        setSessionLoading(false);
      }
    },
    [api],
  );
  useEffect(() => {
    if (!backendReady) return;
    if (useTaskStore.getState().currentSessionId) return;
    api<Array<{ session_id: string }>>("/api/v1/sessions")
      .then((l) => {
        if (l.length > 0) restoreSession(l[0].session_id);
      })
      .catch((e) => console.warn("ChatView: sessions list load failed:", e));
  }, [backendReady, api, restoreSession]);

  /* ── Input persistence ── */
  // P3 修复: 移除 sessionStorage 持久化——与上面的 localStorage 草稿持久化（L186-204）冲突。
  // 原 mount 时 sessionStorage.getItem("yuncat-input") 会覆盖 localStorage 的草稿值，
  // 导致 Electron reload 后草稿被旧 sessionStorage 值替换。
  // localStorage 在 Electron 中跨 reload/重启均持久，sessionStorage 完全冗余。
  // 草稿恢复由 L186 useState 初始化 + L194 useEffect 写入共同完成，无需此块。

  // Canvas: 监听代码块编辑后的"发送到输入框"事件——把编辑后的代码以代码块形式追加到输入框
  useEffect(() => {
    const handler = (e: Event) => {
      const detail = (e as CustomEvent).detail as string;
      if (detail) {
        setInput((prev) => prev ? `${prev}\n\n\`\`\`\n${detail}\n\`\`\`\n` : `\`\`\`\n${detail}\n\`\`\`\n`);
      }
    };
    window.addEventListener("yuncat-insert-input", handler);
    return () => window.removeEventListener("yuncat-insert-input", handler);
    // setInput 现在来自 useSessionDraft（返回的是 React state setter，引用稳定）
  }, [setInput]);

  /* ── Keyboard shortcuts ──
     Ctrl+B: 切换左侧面板
     Ctrl+`: 切换底部面板 */
  useEffect(() => {
    const h = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key === "b") {
        e.preventDefault();
        setLeftCollapsed((v) => !v);
      }
      if (e.ctrlKey && e.key === "`") {
        e.preventDefault();
        setTermCollapsed((v) => !v);
      }
    };
    document.addEventListener("keydown", h);
    return () => document.removeEventListener("keydown", h);
  }, [setLeftCollapsed, setTermCollapsed]);

  /* ── Attachments ── */
  // P0-7: 附件管理——抽取到 useAttachments hook（候选 13）
  const { attachmentPreviews, setAttachmentPreviews, addAtt, dropHandler, pasteHandler } = useAttachments({
    setInput,
    setStatusMsg,
    t,
  });

  /* ── Phase 43 WS.2: 搜索面板 ── */
  const [showSearch, setShowSearch] = useState(false);
  // P0-7: 斜杠命令——抽取到 useSlashCommands hook（候选 7）
  const { slashMenu, setSlashMenu, commands, handleLocalSlashCommand } = useSlashCommands({
    api,
    backendReady,
    agents,
    currentSessionId,
    setSelectedAgent,
    setShowSearch,
    toast,
    t,
    wsPort,
    wsToken,
  });
  // 完整封装：sending/pendingPrompt 状态 + 15s 超时 + task_created 重置 +
  // 编辑重试父链 + doSend 主流程。时序链作为整体搬迁，避免输入丢失 bug。
  const { sending, pendingPrompt, doSend, handleCompact } = useSendTask({
    input,
    setInput,
    commands,
    handleLocalSlashCommand,
    submitTask,
    steerTask,
    selectedAgent,
    api,
    mode,
    currentSessionId,
    setCurrentSession,
    editingTaskId,
    setEditingTaskId,
    setTaskSuperseded,
    store: stableStore,
    t,
    toast,
    wsPort,
    wsToken,
    setSlashMenu,
    attachmentPreviews,
    setAttachmentPreviews,
    setStatusMsg,
  });
  // 2026-09-10（对标 dsh per-model reasoning 描述）：当前模型真实支持的思考档位。
  // null = 尚未取到（按全档可用渲染，保持向后兼容）；[] = 该模型无思考档（隐藏思考区）。

  /* ── Scroll ──
     Batch 4a F2 (Task 2.3 下沉): stick-to-bottom 滚动逻辑已下沉到 MessageList 内部
     （其本身已订阅 taskList，加内部 runningChunksTotal selector 不引发本组件重渲）。
     原实现在此订阅 runningChunksTotal——每个流式 chunk 都触发 ChatView 全树重渲。 */
  useEffect(() => {
    if (!backendReady) return;
  }, [backendReady, api, toast, t]);


  useEffect(() => {
    const h = (e: KeyboardEvent) => {
      // FE4: 焦点在集成终端内时（终端 FindBar 也用 Ctrl+Shift+F）让终端自行处理，
      // 避免全局 SearchPanel 与终端 FindBar 同键同时弹出。
      if (document.activeElement?.closest("[data-terminal-container]")) return;
      if ((e.ctrlKey || e.metaKey) && e.shiftKey && e.key === "F") {
        e.preventDefault();
        setShowSearch((v) => !v);
      }
    };
    document.addEventListener("keydown", h);
    return () => document.removeEventListener("keydown", h);
  }, []);

  const handleNewChat = useCallback(() => {
    api<{ session_id: string }>("/api/v1/sessions/new", { method: "POST" })
      .then((r) => {
        useTaskStore.getState().clearTasks();
        useTaskStore.getState().setCurrentSession(r.session_id);
        useTaskStore.getState().bumpSessionList();
      })
      .catch(() => toast(t("common.newChatFailed"), "error"));
  }, [api, toast, t]);

  // Batch 4a F7: 主进程 new-chat（全局快捷键 Ctrl+Shift+N）/ yuncat:// deep-link
  // → App.tsx 订阅 IPC 后 dispatch CustomEvent → 此处消费（同 yuncat-insert-input 模式）
  useEffect(() => {
    return attachChatIntents({
      onNewChat: handleNewChat,
      onDeepLink: (payload) => {
        const prompt = payload.prompt;
        if (prompt) setInput((prev) => (prev ? `${prev}\n${prompt}` : prompt));
        if (payload.agent) setSelectedAgent(payload.agent);
      },
    });
  }, [handleNewChat, setInput]);

  const handleConfirm = async (approved: boolean, remember?: boolean) => {
    const d = confirmDialog;
    if (!d) return;
    if (!wsPort || !wsToken) throw new Error(t("toolConfirm.backendUnavailable"));
    if (d.items?.length) {
      const tokens = d.items.filter((it) => !it.expiresAt || it.expiresAt > Date.now() / 1000).map((it) => it.confirmToken).filter(Boolean);
      if (!tokens.length) return resolveConfirmDialog(false);
      await confirmToolBatch(tokens, approved, wsPort, wsToken, remember ?? false);
    } else if (d.confirmToken) {
      await confirmTool(d.confirmToken, approved, wsPort, wsToken, remember ?? false, d.toolName);
    } else {
      throw new Error(t("toolConfirm.invalidToken"));
    }
    resolveConfirmDialog(approved);
  };
  // Phase 78: 取出第一个有 clarification 字段的 task —— 同一时刻只展示一个澄清弹窗
  // Batch D H8: 用 taskId 选择器替代全 tasks 订阅——selector 返回 primitive string，
  // 仅在 clarification 任务出现/消失时变化，流式 chunk 不触发 re-render。
  const clarificationTaskId = useTaskStore((s) => {
    for (const t of Object.values(s.tasks)) {
      if (t.clarification) return t.taskId;
    }
    return null;
  });
  const clarificationTask = useMemo(() => {
    if (!clarificationTaskId) return null;
    return useTaskStore.getState().tasks[clarificationTaskId] ?? null;
  }, [clarificationTaskId]);
  const handleClarificationSubmit = (answers: { question: string; answer: string }[]) => {
    if (!clarificationTask) return;
    const tid = clarificationTask.taskId;
    sendGoalClarificationResponse(tid, answers, false);
    clearClarification(tid);
  };
  const handleClarificationNeedMore = (answers: { question: string; answer: string }[]) => {
    if (!clarificationTask) return;
    const tid = clarificationTask.taskId;
    sendGoalClarificationResponse(tid, answers, true);
    clearClarification(tid);
  };
  const handleClarificationSkip = () => {
    if (!clarificationTask) return;
    clearClarification(clarificationTask.taskId);
  };

  // Phase 44: Bridge 工具审批 (bridgeApproval 已通过选择器订阅——Batch D H8)
  const decideBridgeApproval = async (approved: boolean) => {
    const b = bridgeApproval;
    if (!b) return;
    if (!wsPort || !wsToken) throw new Error(t("toolConfirm.backendUnavailable"));
    const response = await fetch(`http://127.0.0.1:${wsPort}/api/v1/bridge/approval-request/${b.approvalId}/decide`, {
      method: "POST",
      headers: { Authorization: `Bearer ${wsToken}`, "Content-Type": "application/json" },
      body: JSON.stringify({ approved }),
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    setBridgeApproval(null);
  };

  const lang = i18n.language;

  // P0-7: taskActions 抽取到 useTaskActions hook（候选 1）——16 个回调方法 + useMemo 优化
  const taskActions = useTaskActions({
    setCtxMenu,
    setEditingTaskId,
    setInput,
    setTaskSuperseded,
    submitTask,
    cancelTask,
    currentSessionId,
    setCurrentSession,
    api,
    toast,
    t,
    store: stableStore,
    openFile: openInWorkspace,
    setVersionTrees,
    wsPort,
    wsToken,
    setStatusMsg,
  });

  const handleAddAttachments = (files: FileList) => {
    Array.from(files).forEach((f) => addAtt(f, f.type.startsWith("image/")));
  };
  const handleCancelRunning = () => {
    Object.entries(useTaskStore.getState().tasks)
      .filter(([_, t]) => t.status === "running")
      .forEach(([tid]) => cancelTask(tid));
  };

  if (!backendReady)
    return (
      <div className="flex-1 flex flex-col items-center justify-center gap-3 text-muted-foreground">
        <LoadingCurve variant="rose" size={40} particleCount={10} />
        <span className="text-sm">{t("chat.startup")}</span>
      </div>
    );

  return (
    <div
      className="flex h-full overflow-hidden"
      onDragOver={(e) => e.preventDefault()}
      onDrop={dropHandler}
      onPaste={pasteHandler}
      style={{ minWidth: 320 }}
    >
      {/* ── Left panel (desktop) ── */}
      {!leftCollapsed && (
        <div className="hidden md:flex shrink-0">
          <LeftPanel
            leftPanel={leftPanel}
            setLeftPanel={setLeftPanel}
            activeTabPath={null}
            onOpenFile={openInWorkspace}
            onAddToChat={handleAddToChat}
            showSearch={showSearch}
            setShowSearch={setShowSearch}
              className="flex w-full"
            style={{ width: leftPanelWidth }}
            t={t}
          />
          {/* P0-6.5: 可拖拽 resize handle——默认透明，悬停显示，减少常驻分割线 */}
          <div
            {...leftPanelDragProps}
            className="w-1 cursor-col-resize hover:bg-primary/40 transition-colors shrink-0"
            role="separator"
            aria-orientation="vertical"
            aria-label={t("chat.resizeLeftPanelAriaLabel")}
          />
        </div>
      )}

      {/* ── Left panel (mobile drawer) ── */}
      <Sheet open={mobileLeftOpen} onOpenChange={setMobileLeftOpen}>
        {/* P0-6.4: 移动端 Sheet 响应式宽度——小屏全宽，sm+ 固定 280px */}
        <SheetContent side="left" className="p-0 w-full sm:w-[280px]">
          <LeftPanel
            leftPanel={leftPanel}
            setLeftPanel={setLeftPanel}
            activeTabPath={null}
            onOpenFile={(p) => {
              setMobileLeftOpen(false);
              openInWorkspace(p);
            }}
            onAddToChat={handleAddToChat}
            showSearch={showSearch}
            setShowSearch={setShowSearch}
              className="h-full w-full"
            t={t}
          />
        </SheetContent>
      </Sheet>

      {/* ── Main ── */}
      <div className="flex-1 flex flex-col min-w-0">
        {/* Toolbar */}
        <ChatToolbar
          leftCollapsed={leftCollapsed}
          onToggleLeft={() => {
            // P0-6.3: 用 useIsMobile 替代手写 innerWidth < 768——与 Tailwind md: 断点同源
            if (isMobile) {
              setMobileLeftOpen(true);
            } else {
              setLeftCollapsed((v) => !v);
            }
          }}
          rightCollapsed={rightCollapsed}
          onToggleRight={toggleRightPanel}
          selectedAgent={selectedAgent}
          setSelectedAgent={setSelectedAgent}
          agents={agents.filter((a) => a.enabled || a.id === "yuncat")}
          mode={mode}
          setMode={setMode}
          showSearch={showSearch}
          setShowSearch={setShowSearch}
          termCollapsed={termCollapsed}
          setTermCollapsed={setTermCollapsed}
          autoApprove={securityPreset === "full_access"}
          backendDead={backendDead}
          onNewChat={handleNewChat}
          t={t}
          hasLongTask={hasLongTask}
          onOpenSupervisor={openSupervisor}
        />

        {/* Phase 43 WS.2: 搜索面板 */}
        <SearchPanel visible={showSearch} onClose={() => setShowSearch(false)} onOpenFile={openInWorkspace} sessionId={currentSessionId} onJumpToTask={(id) => { useTaskStore.getState().setActiveTask(id); setShowSearch(false); requestAnimationFrame(() => document.getElementById(`task-${id}`)?.scrollIntoView({ behavior: "smooth", block: "center" })); }} />

        <div className="flex-1 min-h-0 flex flex-col">
          {/* Messages */}
          {mode === "spec" && specTaskId && (
            <div className="bg-secondary/30" style={{ maxHeight: "50%", minHeight: 180 }}>
              <SpecView taskId={specTaskId} onBackToChat={() => useSpecStore.getState().reset()} />
            </div>
          )}

          {/* Phase D G1.1: 多 Agent 讨论面板 */}
          {mode === "deliberation" && (
            <div className="flex-1 min-h-0">
              {delibLoading ? (
                <div className="flex items-center justify-center h-full text-xs text-muted-foreground">
                  <LoadingCurve variant="spinner" size={20} className="mr-2" />
                  {t("phase49.deliberation.loading")}
                </div>
              ) : delibRoles.length === 0 ? (
                <div className="flex flex-col items-center justify-center h-full text-xs text-muted-foreground gap-2">
                  <span>{t("phase49.deliberation.noRoles")}</span>
                  <button
                    onClick={() => setMode("agent")}
                    className="px-3 py-1 border rounded hover:bg-secondary"
                  >
                    {t("phase49.deliberation.backToChat")}
                  </button>
                </div>
              ) : (
                <DeliberationView
                  prompt={delibPrompt}
                  suggestedRoles={delibRoles}
                  onStartExecution={(consensus) => {
                    setMode("agent");
                    setInput(consensus);
                    setDelibRoles([]);
                    useDeliberationStore.getState().reset();
                  }}
                  onBackToChat={() => {
                    setMode("agent");
                    setDelibRoles([]);
                    useDeliberationStore.getState().reset();
                  }}
                />
              )}
            </div>
          )}

          {mode !== "deliberation" && (
            <>
              <MessageList
                ref={messageListRef}
                sessionLoading={sessionLoading}
                pendingPrompt={pendingPrompt}
                currentSessionId={currentSessionId}
                lang={lang}
                t={t}
                messagesEndRef={messagesEndRef}
                inputRef={inputRef}
                taskActions={taskActions}
                versionTrees={versionTrees}
                onSuggestion={(text) => doSend(text)}
              />
              {/* P0-5.1: 已解析的 @引用药丸——显示在输入框上方，可点 × 删除，hover 预览 */}
              <ContextPills refs={inputRefs} onRemove={handleRemoveRef} />
              <InputArea
                input={input}
                setInput={setInput}
                sending={sending}
                attachmentPreviews={attachmentPreviews}
                setAttachmentPreviews={setAttachmentPreviews}
                atMenu={atMenu}
                setAtMenu={setAtMenu}
                sessionsForAt={sessionsForAt}
                agents={agents}
                inputRef={inputRef}
                fileInputRef={fileInputRef}
                onSend={doSend}
                onCompact={handleCompact}
                onCancelRunning={handleCancelRunning}
                handleAtAutocomplete={handleAtAutocomplete}
                onAddAttachments={handleAddAttachments}
                t={t}
                slashMenu={slashMenu}
                setSlashMenu={setSlashMenu}
                commands={commands}
                securityPreset={securityPreset}
                onSecurityPresetChange={changeSecurityPreset}
              />
            </>
          )}
        </div>

        {/* Bottom panel (desktop) */}
        <div className={`hidden md:flex shrink-0 flex-col ${termCollapsed ? "h-0 overflow-hidden" : ""}`}>
          {!termCollapsed && (
            <div
              {...termDragProps}
              className="h-1 hover:bg-primary/40 cursor-ns-resize shrink-0"
              role="separator"
              aria-orientation="horizontal"
              aria-label={t("chat.resizeTerminalAriaLabel")}
            />
          )}
          <BottomPanel
            collapsed={termCollapsed}
            onToggle={() => setTermCollapsed(true)}
            height={termHeight}
          />
        </div>
      </div>

      {/* Phase 69: RightContextPanel 已上提到 App.tsx 全局渲染 */}

      {/* Bottom panel (mobile sheet) */}
      <Sheet open={!termCollapsed} onOpenChange={(open) => setTermCollapsed(!open)}>
        <SheetContent side="bottom" className="p-0 h-[50vh] flex flex-col">
          <BottomPanel
            collapsed={false}
            onToggle={() => setTermCollapsed(true)}
            height={window.innerHeight * 0.8}
          />
        </SheetContent>
      </Sheet>

      {/* Context menu */}
      {ctxMenu && (
        <MessageContextMenu
          ctxMenu={ctxMenu}
          onClose={() => setCtxMenu(null)}
          onInsertRef={(seq) => {
            const ref = `#${seq}`;
            setInput((v) => (v ? `${v} ${ref}` : ref));
            inputRef.current?.focus();
          }}
          onCopyPrompt={(prompt) => {
            navigator.clipboard.writeText(prompt).catch(() => {});
          }}
          onQuoteWithContext={(seq) => {
            setInput((v) => `${v}\n${t("chat.quoteMarker", { seq })}`);
            inputRef.current?.focus();
          }}
          onEditRegenerate={(taskId, prompt) => {
            setEditingTaskId(taskId);
            setInput(prompt);
            inputRef.current?.focus();
          }}
        />
      )}

      {/* Phase 44: Bridge 危险工具审批 */}
      {bridgeApproval && (
        <BridgeApprovalDialog approval={bridgeApproval} onDecide={decideBridgeApproval} />
      )}

      {/* Phase 80 Tier 3: 长任务建议确认横幅（complexity=extreme 时推送） */}
      {longTaskSuggestion && (
        <div className="flex items-center gap-3 px-4 py-2.5 border-b border-b-warning/30 bg-warning/10 text-sm">
          <span className="flex-1 text-foreground">{t("chat.longTaskSuggested")}</span>
          <button
            onClick={() => {
              const task = useTaskStore.getState().tasks[longTaskSuggestion.taskId];
              const prompt = task?.prompt || "";
              const agentId = task?.agentId;
              const sid = useTaskStore.getState().currentSessionId || undefined;
              setLongTaskSuggestion(null);
              if (prompt) submitTask(prompt, agentId, sid, "long_task", undefined, capabilityLevel);
            }}
            className="px-3 py-1 rounded bg-warning-solid text-warning-foreground hover:bg-warning-solid/90 text-xs font-medium transition-colors"
          >
            {t("chat.longTaskConfirm")}
          </button>
          <button
            onClick={() => setLongTaskSuggestion(null)}
            className="px-3 py-1 rounded border border-warning/40 text-warning hover:bg-warning/10 text-xs font-medium transition-colors"
          >
            {t("chat.longTaskDismiss")}
          </button>
        </div>
      )}

      {/* Phase 78: Goal Clarification Dialog */}
      {clarificationTask?.clarification && (
        <GoalClarificationDialog
          taskId={clarificationTask.taskId}
          questions={clarificationTask.clarification.questions}
          stage={clarificationTask.clarification.stage}
          reason={clarificationTask.clarification.reason}
          onSubmit={handleClarificationSubmit}
          onNeedMore={handleClarificationNeedMore}
          onSkip={handleClarificationSkip}
        />
      )}

      {/* Confirm dialog */}
      {/* Batch 3.2: 根据 confirmDialog.items 切换单工具/批量确认弹窗 */}
      {confirmDialog &&
        (confirmDialog.items?.length ? (
          <ToolConfirmBatchDialog
            open={!!confirmDialog}
            items={confirmDialog.items}
            taskPrompt={confirmDialog.taskPrompt}
            onConfirm={handleConfirm}
            onExpire={() => resolveConfirmDialog(false)}
          />
        ) : (
          <ToolConfirmDialog
            open={!!confirmDialog}
            toolName={confirmDialog.toolName}
            preview={confirmDialog.preview}
            taskPrompt={confirmDialog.taskPrompt}
            expiresAt={confirmDialog.expiresAt}
            onConfirm={handleConfirm}
            onExpire={() => resolveConfirmDialog(false)}
          />
        ))}
    </div>
  );
}
