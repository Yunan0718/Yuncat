import { FileText, PanelRight, Plus, Search, Terminal, Rocket } from "lucide-react";

import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "../ui/select";
import type { ChatMode } from "./types";
import { TodoChecklist } from "./TodoChecklist";
import { DeliverablesPanel } from "./DeliverablesPanel";

/**
 * 对话顶部工具栏（2026-09-16 收敛重写）。
 *
 * 之前这一行摊着 9 个控件：文件/终端/左右面板/Agent/三档模式/模型/能力面板/搜索/新建，
 * 顶上还有一排状态胶囊，同一个决策要在两行里找。现在按"一次对话要决定的都放输入区、
 * 其余收成图标"来分：
 *   - 模型与强度 → 输入区上沿一行（ModelEffortBar）
 *   - 布局切换（文件/终端/右栏）→ 图标按钮，标签走 sr-only，视觉上只剩图标
 *   - Agent / 模式 → 发送时才决定的，留在这一行但更紧凑
 *   - 搜索 / 驾驶舱 / 新对话 → 右侧动作区
 *
 * 注意：sr-only 的标签仍然是真实文本节点，`getByText` 这类测试断言照旧成立。
 */
export interface ChatToolbarProps {
  leftCollapsed: boolean;
  onToggleLeft: () => void;
  rightCollapsed: boolean;
  onToggleRight: () => void;
  selectedAgent: string;
  setSelectedAgent: (v: string) => void;
  agents: Array<{ id: string; name: string; enabled?: boolean }>;
  mode: ChatMode;
  setMode: (m: ChatMode) => void;
  showSearch: boolean;
  /** 允许函数式更新，与调用方 setState 的签名一致 */
  setShowSearch: (v: boolean | ((prev: boolean) => boolean)) => void;
  /** 兼容旧调用点：模型名已在输入区那一行显示 */
  currentModelName?: string;
  termCollapsed: boolean;
  setTermCollapsed: (v: boolean) => void;
  autoApprove: boolean;
  backendDead: boolean;
  onNewChat: () => void;
  t: (key: string, opts?: Record<string, unknown>) => string;
  hasLongTask?: boolean;
  onOpenSupervisor?: () => void;
  /** 旧调用点仍在传，保留以便渐进收敛（不再渲染控件） */
  input?: string;
  capabilityLevel?: number;
  onCapabilityChange?: (v: number) => void;
  reasoningEffort?: string;
  onReasoningChange?: (v: never) => void;
  supportedReasoning?: string[] | null;
}

/** 只保留三个真实常用的模式：计划/Spec/讨论由 AI 或命令面板按需进入 */
const MODES: Array<{ id: ChatMode; labelKey: string; tipKey: string }> = [
  { id: "agent", labelKey: "chat.mode_agent", tipKey: "chat.mode_agent_tip" },
  { id: "ask", labelKey: "chat.mode_ask", tipKey: "chat.mode_ask_tip" },
  { id: "long_task", labelKey: "chat.mode_long_task", tipKey: "chat.mode_long_task_tip" },
];

const ICON_BUTTON =
  "h-7 w-7 inline-flex items-center justify-center rounded-lg text-muted-foreground hover:text-foreground hover:bg-secondary transition-colors shrink-0";

export function ChatToolbar(props: ChatToolbarProps) {
  const {
    leftCollapsed, onToggleLeft, rightCollapsed, onToggleRight,
    selectedAgent, setSelectedAgent, agents, mode, setMode,
    showSearch, setShowSearch, termCollapsed, setTermCollapsed,
    backendDead, onNewChat, t, hasLongTask, onOpenSupervisor,
  } = props;

  return (
    <div className="px-3 py-1.5 flex items-center gap-1.5 shrink-0 bg-surface-2">
      {/* 布局：图标按钮，标签视觉隐藏但仍在 DOM 里（无障碍与测试都依赖它） */}
      <button
        type="button"
        onClick={onToggleLeft}
        aria-label={t("chat.fileBrowser")}
        title={t("chat.fileBrowser")}
        className={`${ICON_BUTTON} ${leftCollapsed ? "" : "bg-secondary text-foreground"}`}
      >
        <FileText size={15} strokeWidth={1.7} />
      </button>
      <button
        type="button"
        onClick={() => setTermCollapsed(!termCollapsed)}
        title={t("chat.terminal")}
        aria-label={t("chat.terminal")}
        className={`${ICON_BUTTON} ${termCollapsed ? "" : "bg-secondary text-foreground"}`}
      >
        <Terminal size={15} strokeWidth={1.7} />
        <span className="sr-only">{t("chat.terminal")}</span>
      </button>
      <button
        type="button"
        onClick={onToggleRight}
        aria-label={t("chat.contextPanel")}
        title={t("chat.contextPanel")}
        className={`${ICON_BUTTON} ${rightCollapsed ? "" : "bg-secondary text-foreground"}`}
      >
        <PanelRight size={15} strokeWidth={1.7} />
      </button>

      <span className="w-px h-4 bg-border/60 mx-0.5 hidden sm:block" aria-hidden="true" />

      {/* Agent */}
      <Select value={selectedAgent || "yuncat"} onValueChange={(v) => v && setSelectedAgent(v)}>
        <SelectTrigger className="h-7 w-auto max-w-[150px] text-xs border-0 bg-transparent hover:bg-secondary px-2 gap-1">
          <SelectValue placeholder={t("chat.agent")} />
        </SelectTrigger>
        <SelectContent>
          {agents.map((a) => (
            <SelectItem key={a.id} value={a.id}>{a.name}</SelectItem>
          ))}
        </SelectContent>
      </Select>

      {/* 模式：三档分段控件 */}
      <div className="flex items-center gap-0.5 rounded-lg bg-muted p-0.5 shrink-0">
        {MODES.map((m) => (
          <button
            key={m.id}
            type="button"
            onClick={() => setMode(m.id)}
            title={t(m.tipKey)}
            aria-pressed={mode === m.id}
            className={`px-2 py-0.5 rounded-md text-[11px] transition-colors ${
              mode === m.id
                ? "bg-card text-foreground shadow-sm font-medium"
                : "text-muted-foreground hover:text-foreground"
            }`}
          >
            {t(m.labelKey)}
          </button>
        ))}
      </div>

      <TodoChecklist />
      <DeliverablesPanel />

      <div className="flex-1 min-w-2" />

      {backendDead && (
        <span
          data-testid="backend-dead"
          aria-label="backend-dead"
          title={t("chat.noModelWarning")}
          className="h-1.5 w-1.5 rounded-full bg-destructive-solid shrink-0"
        />
      )}

      <button
        type="button"
        onClick={() => setShowSearch(!showSearch)}
        aria-pressed={showSearch}
        aria-label={t("chat.searchConversation")}
        title={t("chat.searchConversation")}
        className={`${ICON_BUTTON} ${showSearch ? "bg-surface-3 text-foreground" : ""}`}
      >
        <Search size={15} strokeWidth={1.7} />
      </button>

      {hasLongTask && onOpenSupervisor && (
        <button
          type="button"
          onClick={onOpenSupervisor}
          aria-label={t("chat.supervisor")}
          title={t("chat.supervisor")}
          className={ICON_BUTTON}
        >
          <Rocket size={15} strokeWidth={1.7} />
          <span className="sr-only">{t("chat.supervisor")}</span>
        </button>
      )}

      <button type="button" onClick={onNewChat} className={ICON_BUTTON} aria-label={t("chat.newChat")} title={t("chat.newChat")}>
        <Plus size={15} strokeWidth={1.7} />
        <span className="sr-only">{t("chat.newChat")}</span>
      </button>
    </div>
  );
}
