import { create } from "zustand";

type Theme = "light" | "dark" | "system";
type Density = "compact" | "comfortable";
type AccentHue = 210 | 140 | 270 | 25;
type LinkOpenBehavior = "copy" | "browser";
// Phase 98: 会话恢复——BottomPanel tab + WorkspacePage 底部面板
type ChatBottomTab = "output" | "audit";
type WorkspaceBottomPanel = "terminal" | "git" | "problems" | null;

function getInitialTheme(): Theme {
  try {
    const stored = localStorage.getItem("yuncat-theme");
    if (stored === "light" || stored === "dark" || stored === "system") return stored;
  } catch {
    /* ignore */
  }
  return "system";
}

function getInitialDensity(): Density {
  try {
    const stored = localStorage.getItem("yuncat-ui-density");
    if (stored === "compact" || stored === "comfortable") return stored;
  } catch {
    /* ignore */
  }
  return "comfortable";
}

function getInitialAccentHue(): AccentHue {
  try {
    const stored = localStorage.getItem("yuncat-accent-hue");
    const n = stored ? Number(stored) : 0;
    if (n === 210 || n === 140 || n === 270 || n === 25) return n;
  } catch {
    /* ignore */
  }
  return 210;
}

function getInitialUiFontSize(): number {
  try {
    const stored = localStorage.getItem("yuncat-ui-font-size");
    const n = stored ? Number(stored) : 0;
    if (n >= 12 && n <= 20) return n;
  } catch {
    /* ignore */
  }
  return 14;
}

function getInitialCapabilityLevel(): number {
  try {
    const raw = localStorage.getItem("yuncat-capability-level");
    if (raw === null) return 1; // 默认（非 MAX）
    const n = parseInt(raw, 10);
    // 2026-09-17: 能力档只有两档（默认 / MAX）；旧的“轻量”值归到默认
    return Number.isNaN(n) || n < 2 ? 1 : 2;
  } catch {
    return 1;
  }
}

/** Phase X (2026-09-10): 思考强度——对齐 Codex model_reasoning_effort（none/minimal 由后端 clamp 处理）。
 *  默认随能力等级联动：轻量→none（不思考）/ 标准→medium / 全功能→high；用户可单独覆盖。 */
export type ReasoningEffort = "none" | "minimal" | "low" | "medium" | "high" | "xhigh";
export function capabilityToReasoning(level: number): ReasoningEffort {
  // 2026-09-17: 思考档位不再由能力档限制（全开），这里只是“跟随”时的默认值
  return level >= 2 ? "high" : "medium";
}
function getInitialReasoningEffort(): ReasoningEffort {
  try {
    const raw = localStorage.getItem("yuncat-reasoning-effort");
    if (raw === "none" || raw === "minimal" || raw === "low" || raw === "medium" || raw === "high" || raw === "xhigh") return raw;
    return "medium"; // 默认标准思考
  } catch {
    return "medium";
  }
}

/** 旧版本只有一个 effort 键：它有值就说明用户从前显式选过，当 manual 处理 */
function getInitialReasoningEffortSource(): "auto" | "manual" {
  try {
    return localStorage.getItem("yuncat-reasoning-effort") ? "manual" : "auto";
  } catch {
    return "auto";
  }
}

function getInitialLinkOpenBehavior(): LinkOpenBehavior {
  try {
    const stored = localStorage.getItem("yuncat-link-open-behavior");
    if (stored === "copy" || stored === "browser") return stored;
  } catch {
    /* ignore */
  }
  return "copy";
}

// Phase 98: 会话恢复初始化函数——从 localStorage 读上次退出时的 UI 状态
function getInitialLastRoute(): string {
  try {
    const stored = localStorage.getItem("yuncat-last-route");
    if (stored && typeof stored === "string") return stored;
  } catch {
    /* ignore */
  }
  return "/";
}

function getInitialChatBottomTab(): ChatBottomTab {
  try {
    const stored = localStorage.getItem("yuncat-chat-bottom-tab");
    if (stored === "output" || stored === "audit") return stored;
  } catch {
    /* ignore */
  }
  return "output";
}

function getInitialWorkspaceBottomPanel(): WorkspaceBottomPanel {
  try {
    const stored = localStorage.getItem("yuncat-workspace-bottom-panel");
    // null / "terminal" / "git" / "problems"——其他值视为 null
    if (stored === "terminal" || stored === "git" || stored === "problems") return stored;
  } catch {
    /* ignore */
  }
  return null;
}

function getInitialSessionTabs(): { tabs: Array<{ sessionId: string; title: string }>; active: string | null } {
  try {
    const stored = localStorage.getItem("yuncat-session-tabs");
    if (stored) {
      const parsed = JSON.parse(stored);
      if (Array.isArray(parsed.tabs) && (parsed.active === null || typeof parsed.active === "string")) {
        // 校验 tabs 结构
        const valid = parsed.tabs.filter(
          (t: unknown): t is { sessionId: string; title: string } =>
            typeof t === "object" && t !== null && typeof (t as { sessionId: unknown }).sessionId === "string" && typeof (t as { title: unknown }).title === "string",
        );
        return { tabs: valid, active: valid.some((t: { sessionId: string; title: string }) => t.sessionId === parsed.active) ? parsed.active : (valid[0]?.sessionId ?? null) };
      }
    }
  } catch {
    /* ignore */
  }
  return { tabs: [], active: null };
}

// Phase 48 T1-1: Token 安全存储——renderer 仅持内存引用，不写 sessionStorage（防 XSS 读取）。
// 主进程为权威源（main.js 持 token + 通过 IPC get_backend_info 派发），
// 页面 reload 时由 useBackendReady hook 重新经 IPC 索取。
// 注：计划提到的 safeStorage 在此架构不适用——token 每次启动由 main.js 重生成
// （crypto.randomBytes），若 safeStorage 持久化到磁盘，下次启动会读到旧 token
// 与后端新 token 不匹配，反而破坏认证。故采用"内存+IPC 重取"方案。
function getInitialToken(): string | null {
  return null;
}
// 端口由 Electron 主进程 IPC 推送 —— 不从 sessionStorage 读（旧端口已失效）
function getInitialPort(): number | null {
  return null;
}

interface AppState {
  backendReady: boolean;
  backendDead: boolean;
  /** 各 Agent 运行状态 — WebSocket 推送更新 (Sidebar 指示灯用) */
  agentStatus: Record<string, boolean>;
  /** P0-1.11: 各 Agent 健康状态 — adapter_degraded WS 事件触发
   * 'healthy' 默认；'degraded' 重启上限耗尽；'down' 进程未启动 */
  adapterHealth: Record<string, "healthy" | "degraded" | "down">;
  setAdapterHealth: (id: string, status: "healthy" | "degraded" | "down") => void;
  port: number | null;
  token: string | null;
  /** Phase 7: 主题 */
  theme: Theme;
  /** Phase 49-G: 界面密度 */
  uiDensity: Density;
  /** Phase 49-G: UI 字体大小 (px) */
  uiFontSize: number;
  /** Phase 49-G: 强调色色相 */
  accentHue: AccentHue;
  /** Phase C: 聊天中 URL 实体点击行为 */
  linkOpenBehavior: LinkOpenBehavior;
  /** Phase 98: 上次退出时的路由——启动时恢复 */
  lastRoute: string;
  /** Phase 98: ChatView BottomPanel 当前 tab——启动时恢复 */
  chatBottomTab: ChatBottomTab;
  /** Phase 98: WorkspacePage 底部面板——启动时恢复 */
  workspaceBottomPanel: WorkspaceBottomPanel;
  /** Phase 7: 命令面板是否打开 */
  commandPaletteOpen: boolean;
  /** 2026-09-15: 快捷键参考弹窗是否打开——命令面板与 Shift+? 共用同一入口，避免各写一份 */
  shortcutHelpOpen: boolean;
  /** Phase 7: 选中的任务 ID（打开详情抽屉） */
  selectedTaskId: string | null;
  /** Batch 8.5: 离线消息队列计数（WS 断开期间缓冲的消息数） */
  outboxCount: number;

  setBackendReady: (ready: boolean) => void;
  setBackendDead: (dead: boolean) => void;
  setOutboxCount: (count: number) => void;
  setPort: (port: number) => void;
  setToken: (token: string) => void;
  setTheme: (theme: Theme) => void;
  setUiDensity: (density: Density) => void;
  setUiFontSize: (size: number) => void;
  setAccentHue: (hue: AccentHue) => void;
  setLinkOpenBehavior: (behavior: LinkOpenBehavior) => void;
  /** Phase 98: 设置上次路由（持久化） */
  setLastRoute: (route: string) => void;
  /** Phase 98: 设置 ChatView BottomPanel tab（持久化） */
  setChatBottomTab: (tab: ChatBottomTab) => void;
  /** Phase 98: 设置 WorkspacePage 底部面板（持久化） */
  setWorkspaceBottomPanel: (panel: WorkspaceBottomPanel) => void;
  toggleCommandPalette: () => void;
  setCommandPaletteOpen: (open: boolean) => void;
  setShortcutHelpOpen: (open: boolean) => void;
  setSelectedTaskId: (taskId: string | null) => void;
  fileWatchCounter: number;
  bumpFileWatch: () => void;
  /** v66: 从聊天中点击文件路径 → 跳转到工作区并打开该文件 */
  pendingWorkspaceFile: string | null;
  setPendingWorkspaceFile: (file: string | null) => void;
  /** AI 自动触发讨论时设置的 prompt——ChatView 监听后切换到 DeliberationView */
  pendingDeliberationPrompt: string | null;
  setPendingDeliberationPrompt: (prompt: string | null) => void;
  /** E3: LSP 诊断实时推送——WS 推送 ruff/tsc 诊断结果，MonacoEditor 订阅渲染 marker */
  diagnosticsByFile: Record<string, Diagnostic[]>;
  setDiagnostics: (file: string, diagnostics: Diagnostic[]) => void;
  clearDiagnostics: (file: string) => void;
  /** Phase 17: 会话侧栏折叠 */
  sessionSidebarCollapsed: boolean;
  toggleSessionSidebar: () => void;
  /** 主导航侧栏折叠（左侧 Sidebar 图标模式）——持久化到 localStorage
   *  对标 VS Code Activity Bar：折叠后仅显示图标，悬停显示 title 提示 */
  navSidebarCollapsed: boolean;
  toggleNavSidebar: () => void;
  setNavSidebarCollapsed: (v: boolean) => void;
  /** Phase 69: 右侧上下文面板折叠——全局化到 store 以便所有路由共享 */
  rightCollapsed: boolean;
  toggleRightPanel: () => void;
  setRightCollapsed: (v: boolean) => void;
  /** Phase 82: 长任务 Supervisor 驾驶舱抽屉开关 */
  supervisorDrawerOpen: boolean;
  toggleSupervisorDrawer: () => void;
  setSupervisorDrawerOpen: (v: boolean) => void;
  /** Phase 107: 统一审批 Inbox 抽屉开关 */
  inboxOpen: boolean;
  toggleInbox: () => void;
  setInboxOpen: (v: boolean) => void;
  /** Phase 107: Inbox pending 数量徽章 */
  inboxBadgeCount: number;
  setInboxBadgeCount: (n: number) => void;
  /** Phase 43 V.1: 编辑器上下文 — 对标 Cursor agent context */
  editorContext: EditorContext | null;
  setEditorContext: (ctx: Partial<EditorContext> | null) => void;
  /** Phase 108+: 能力等级 0=轻量 / 1=标准 / 2=强力（取代原 MAX 布尔开关） */
  capabilityLevel: number;
  setCapabilityLevel: (v: number) => void;
  /** Phase X (2026-09-10): 思考强度 low/medium/high——默认随能力等级联动，用户可单独调整 */
  reasoningEffort: ReasoningEffort;
  /** auto = 跟随能力档位；manual = 用户在模型选择器里显式选过（再改能力档不覆盖） */
  reasoningEffortSource: "auto" | "manual";
  setReasoningEffort: (v: ReasoningEffort) => void;
  /** 切回“跟随能力档位” */
  resetReasoningEffort: () => void;
  /** MAX 开关（能力档只有两档） */
  setMaxMode: (on: boolean) => void;
  setAgentStatus: (id: string, ready: boolean) => void;
  /** Phase 51: 终端命令推送 — 文件右键"在集成终端中打开" */
  terminalPendingCommand: string | null;
  setTerminalPendingCommand: (cmd: string | null) => void;
  sendTerminalCommand: (cmd: string) => void;
}

export interface EditorContext {
  activeFile: string;
  cursorLine: number;
  /** 列号（1 起）——状态栏「行:列」显示用 */
  cursorColumn: number;
  selection: string;
  lastEditAt: number;
  /** 当前模型语言 id（如 typescript / python）与缩进宽度——状态栏显示用 */
  language: string;
  indentSize: number;
}

// E3: LSP 诊断对象——与后端 lint_service / WS diagnostics 消息字段对齐
export interface Diagnostic {
  line: number;
  col: number;
  message: string;
  severity: string; // "error" | "warning"
  code?: string;
}

// Phase 106: 统一通知对象，与后端 notifications 表 + WS 消息一致
export interface AppNotification {
  id: string;
  type: string;
  title: string;
  body: string;
  link: string;
  level: "info" | "success" | "warning" | "error";
  read: boolean;
  created_at: number;
  task_id?: string;
  agent_id?: string;
}

interface AppStateActions {
  /** Phase 106: 统一通知列表 */
  notifications: AppNotification[];
  addNotification: (n: AppNotification) => void;
  markNotificationRead: (id: string) => void;
  removeNotification: (id: string) => void;
  clearReadNotifications: () => void;
  clearNotifications: () => void;
  /** Phase 18: 多 session Tab 管理 */
  openSessionTabs: Array<{ sessionId: string; title: string }>;
  activeSessionTab: string | null;
  addSessionTab: (sessionId: string, title: string) => void;
  removeSessionTab: (sessionId: string) => void;
  setActiveSessionTab: (sessionId: string) => void;
  updateSessionTabTitle: (sessionId: string, title: string) => void;
}

// Phase 98: 会话 Tab 初始值——只解析一次 localStorage（避免双调用 getInitialSessionTabs）
const _initialSessionTabs = getInitialSessionTabs();

export const useAppStore = create<AppState & AppStateActions>((set) => ({
  backendReady: false,
  backendDead: false,
  agentStatus: {},
  adapterHealth: {},
  port: getInitialPort(),
  token: getInitialToken(),
  theme: getInitialTheme(),
  uiDensity: getInitialDensity(),
  uiFontSize: getInitialUiFontSize(),
  accentHue: getInitialAccentHue(),
  linkOpenBehavior: getInitialLinkOpenBehavior(),
  // Phase 98: 会话恢复字段初始值
  lastRoute: getInitialLastRoute(),
  chatBottomTab: getInitialChatBottomTab(),
  workspaceBottomPanel: getInitialWorkspaceBottomPanel(),
  commandPaletteOpen: false,
  shortcutHelpOpen: false,
  selectedTaskId: null,
  outboxCount: 0,
  capabilityLevel: getInitialCapabilityLevel(),
  reasoningEffort: getInitialReasoningEffort(),
  reasoningEffortSource: getInitialReasoningEffortSource(),

  // 互斥设置——避免 startup 阶段 30s 超时与后端实际就绪的状态竞争
  // 场景：后端启动慢 → 30s 超时触发 backendDead=true → 后端终于就绪触发 backendReady=true
  // 此时两个状态都是 true，StatusBar 会错误显示"断开"。修复：设一个时清另一个。
  setBackendReady: (ready) =>
    set({ backendReady: ready, ...(ready ? { backendDead: false } : {}) }),
  setBackendDead: (dead) =>
    set({ backendDead: dead, ...(dead ? { backendReady: false } : {}) }),
  setOutboxCount: (count) => set({ outboxCount: count }),
  setPort: (port) => {
    try {
      sessionStorage.setItem("yuncat-port", String(port));
    } catch {
      // no-op
    }
    set({ port });
  },
  setToken: (token) => {
    try {
      sessionStorage.removeItem("yuncat-token");
    } catch {
      // no-op
    }
    set({ token });
  },
  setTheme: (theme) => {
    try {
      localStorage.setItem("yuncat-theme", theme);
    } catch {
      /* ignore */
    }
    set({ theme });
  },
  setUiDensity: (density) => {
    try {
      localStorage.setItem("yuncat-ui-density", density);
    } catch {
      /* ignore */
    }
    set({ uiDensity: density });
  },
  setUiFontSize: (size) => {
    const clamped = Math.max(12, Math.min(20, size));
    try {
      localStorage.setItem("yuncat-ui-font-size", String(clamped));
    } catch {
      // no-op
    }
    set({ uiFontSize: clamped });
  },
  setAccentHue: (hue) => {
    try {
      localStorage.setItem("yuncat-accent-hue", String(hue));
    } catch {
      // no-op
    }
    set({ accentHue: hue });
  },
  setLinkOpenBehavior: (behavior) => {
    try {
      localStorage.setItem("yuncat-link-open-behavior", behavior);
    } catch {
      // no-op
    }
    set({ linkOpenBehavior: behavior });
  },
  // Phase 98: 会话恢复 setter——持久化到 localStorage
  setLastRoute: (route) => {
    try {
      localStorage.setItem("yuncat-last-route", route);
    } catch {
      // no-op
    }
    set({ lastRoute: route });
  },
  setChatBottomTab: (tab) => {
    try {
      localStorage.setItem("yuncat-chat-bottom-tab", tab);
    } catch {
      // no-op
    }
    set({ chatBottomTab: tab });
  },
  setWorkspaceBottomPanel: (panel) => {
    try {
      // null 存为空字符串，读取时视为 null
      localStorage.setItem("yuncat-workspace-bottom-panel", panel ?? "");
    } catch {
      // no-op
    }
    set({ workspaceBottomPanel: panel });
  },
  toggleCommandPalette: () => set((s) => ({ commandPaletteOpen: !s.commandPaletteOpen })),
  setCommandPaletteOpen: (open) => set({ commandPaletteOpen: open }),
  setShortcutHelpOpen: (open) => set({ shortcutHelpOpen: open }),
  setSelectedTaskId: (taskId) => set({ selectedTaskId: taskId }),
  setCapabilityLevel: (v) => {
    // 两档：>=2 即 MAX，其余默认
    const clamped = Math.round(v) >= 2 ? 2 : 1;
    try {
      localStorage.setItem("yuncat-capability-level", String(clamped));
    } catch {
      // no-op
    }
    // 2026-09-17: 思考强度只在“跟随档位”时被带着改；用户在模型选择器里
    // 显式选过档位后就不再覆盖他的选择（对标 Codex：模型和强度分开）
    set((state) => ({
      capabilityLevel: clamped,
      reasoningEffort:
        state.reasoningEffortSource === "auto" ? capabilityToReasoning(clamped) : state.reasoningEffort,
    }));
  },
  setReasoningEffort: (v) => {
    try {
      localStorage.setItem("yuncat-reasoning-effort", v);
    } catch {
      // no-op
    }
    set({ reasoningEffort: v, reasoningEffortSource: "manual" });
  },
  setMaxMode: (on) => {
    // 能力档只有两档：MAX 开 = 2，关 = 默认 1
    useAppStore.getState().setCapabilityLevel(on ? 2 : 1);
  },
  resetReasoningEffort: () => {
    try {
      localStorage.removeItem("yuncat-reasoning-effort");
    } catch {
      // no-op
    }
    set((state) => ({
      reasoningEffortSource: "auto",
      reasoningEffort: capabilityToReasoning(state.capabilityLevel),
    }));
  },
  fileWatchCounter: 0,
  bumpFileWatch: () => set((s) => ({ fileWatchCounter: s.fileWatchCounter + 1 })),
  // E3: LSP 诊断实时推送——单文件覆盖式更新（清空时传 []）
  diagnosticsByFile: {},
  setDiagnostics: (file, diagnostics) =>
    set((s) => ({ diagnosticsByFile: { ...s.diagnosticsByFile, [file]: diagnostics } })),
  clearDiagnostics: (file) =>
    set((s) => {
      const next = { ...s.diagnosticsByFile };
      delete next[file];
      return { diagnosticsByFile: next };
    }),
  pendingWorkspaceFile: null,
  setPendingWorkspaceFile: (file) => set({ pendingWorkspaceFile: file }),
  pendingDeliberationPrompt: null,
  setPendingDeliberationPrompt: (prompt) => set({ pendingDeliberationPrompt: prompt }),
  sessionSidebarCollapsed: (() => {
    try {
      return localStorage.getItem("yuncat-session-collapsed") === "true";
    } catch {
      return false;
    }
  })(),
  toggleSessionSidebar: () =>
    set((s) => {
      const next = !s.sessionSidebarCollapsed;
      try {
        localStorage.setItem("yuncat-session-collapsed", String(next));
      } catch {
        /* ignore */
      }
      return { sessionSidebarCollapsed: next };
    }),
  setSessionSidebarCollapsed: (v: boolean) => set({ sessionSidebarCollapsed: v }),
  // 主导航侧栏折叠——localStorage 持久化，默认展开（icon + label）
  navSidebarCollapsed: (() => {
    try {
      return localStorage.getItem("yuncat-nav-collapsed") === "true";
    } catch {
      return false;
    }
  })(),
  toggleNavSidebar: () =>
    set((s) => {
      const next = !s.navSidebarCollapsed;
      try {
        localStorage.setItem("yuncat-nav-collapsed", String(next));
      } catch {
        /* ignore */
      }
      return { navSidebarCollapsed: next };
    }),
  setNavSidebarCollapsed: (v: boolean) => {
    try {
      localStorage.setItem("yuncat-nav-collapsed", String(v));
    } catch {
      /* ignore */
    }
    set({ navSidebarCollapsed: v });
  },
  // Phase 69: 右侧面板全局状态——localStorage 持久化
  // 默认折叠（true）：主流聊天应用（ChatGPT/Claude）首屏以对话为主，
  // 右侧面板作为可选辅助区，由用户手动打开，避免初始界面繁杂。
  rightCollapsed: (() => {
    try {
      const stored = localStorage.getItem("yuncat-right-collapsed");
      return stored === null ? true : stored === "true";
    } catch {
      return true;
    }
  })(),
  toggleRightPanel: () =>
    set((s) => {
      const next = !s.rightCollapsed;
      try {
        localStorage.setItem("yuncat-right-collapsed", String(next));
      } catch {
        /* ignore */
      }
      return { rightCollapsed: next };
    }),
  setRightCollapsed: (v: boolean) => {
    try {
      localStorage.setItem("yuncat-right-collapsed", String(v));
    } catch {
      /* ignore */
    }
    set({ rightCollapsed: v });
  },
  // Phase 82: Supervisor 驾驶舱抽屉——纯内存状态，不持久化（每次启动默认关闭）
  supervisorDrawerOpen: false,
  toggleSupervisorDrawer: () => set((s) => ({ supervisorDrawerOpen: !s.supervisorDrawerOpen })),
  setSupervisorDrawerOpen: (v: boolean) => set({ supervisorDrawerOpen: v }),
  // Phase 107: 审批 Inbox——纯内存状态，不持久化
  inboxOpen: false,
  toggleInbox: () => set((s) => ({ inboxOpen: !s.inboxOpen })),
  setInboxOpen: (v: boolean) => set({ inboxOpen: v }),
  inboxBadgeCount: 0,
  setInboxBadgeCount: (n: number) => set({ inboxBadgeCount: Math.max(0, n) }),
  editorContext: null,
  setAgentStatus: (id, ready) => set((s) => ({ agentStatus: { ...s.agentStatus, [id]: ready } })),
  setAdapterHealth: (id, status) => set((s) => ({ adapterHealth: { ...s.adapterHealth, [id]: status } })),
  setEditorContext: (ctx: Partial<EditorContext> | null) =>
    set((s) => ({
      editorContext: ctx === null ? null : ({ ...s.editorContext, ...ctx } as EditorContext),
    })),
  terminalPendingCommand: null,
  setTerminalPendingCommand: (cmd) => set({ terminalPendingCommand: cmd }),
  sendTerminalCommand: (cmd) => set({ terminalPendingCommand: cmd }),
  notifications: [],
  addNotification: (n) =>
    set((s) => {
      const filtered = s.notifications.filter((x) => x.id !== n.id);
      return { notifications: [...filtered.slice(-49), n] };
    }),
  markNotificationRead: (id) =>
    set((s) => ({
      notifications: s.notifications.map((n) => (n.id === id ? { ...n, read: true } : n)),
    })),
  removeNotification: (id) =>
    set((s) => ({
      notifications: s.notifications.filter((n) => n.id !== id),
    })),
  clearReadNotifications: () =>
    set((s) => ({
      notifications: s.notifications.filter((n) => !n.read),
    })),
  clearNotifications: () => set({ notifications: [] }),
  // Phase 98: 会话 Tab 持久化——启动时恢复上次打开的会话列表
  openSessionTabs: _initialSessionTabs.tabs,
  activeSessionTab: _initialSessionTabs.active,
  addSessionTab: (sessionId, title) =>
    set((s) => {
      let next: { openSessionTabs: Array<{ sessionId: string; title: string }>; activeSessionTab: string | null };
      if (s.openSessionTabs.find((t) => t.sessionId === sessionId)) {
        next = { openSessionTabs: s.openSessionTabs, activeSessionTab: sessionId };
      } else {
        next = {
          openSessionTabs: [...s.openSessionTabs, { sessionId, title }],
          activeSessionTab: sessionId,
        };
      }
      try {
        localStorage.setItem("yuncat-session-tabs", JSON.stringify({ tabs: next.openSessionTabs, active: next.activeSessionTab }));
      } catch {
        // no-op
      }
      return next;
    }),
  removeSessionTab: (sessionId) =>
    set((s) => {
      const tabs = s.openSessionTabs.filter((t) => t.sessionId !== sessionId);
      const active = s.activeSessionTab === sessionId ? tabs[tabs.length - 1]?.sessionId || null : s.activeSessionTab;
      try {
        localStorage.setItem("yuncat-session-tabs", JSON.stringify({ tabs, active }));
      } catch {
        // no-op
      }
      return { openSessionTabs: tabs, activeSessionTab: active };
    }),
  setActiveSessionTab: (sessionId) => {
    try {
      const tabs = useAppStore.getState().openSessionTabs;
      localStorage.setItem("yuncat-session-tabs", JSON.stringify({ tabs, active: sessionId }));
    } catch {
      // no-op
    }
    set({ activeSessionTab: sessionId });
  },
  updateSessionTabTitle: (sessionId, title) =>
    set((s) => {
      const tabs = s.openSessionTabs.map((t) => (t.sessionId === sessionId ? { ...t, title } : t));
      try {
        localStorage.setItem("yuncat-session-tabs", JSON.stringify({ tabs, active: s.activeSessionTab }));
      } catch {
        // no-op
      }
      return { openSessionTabs: tabs };
    }),
}));
