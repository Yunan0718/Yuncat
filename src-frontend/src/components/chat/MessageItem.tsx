import { memo, useState, type CSSProperties } from "react";
import { Button } from "../ui/button";
import { Skeleton } from "../ui/skeleton";
import { LoadingCurve } from "../common/LoadingCurve";
import { StatusIndicator } from "../common/StatusIndicator";
import { ResultRenderer } from "../ResultRenderer";
import { TaskErrorNotice } from "./TaskErrorNotice";
import { TrajectoryCard } from "./TrajectoryCard";
import { projectTrajectory } from "../../lib/trajectory";
import { PlanProgress } from "./PlanProgress";
import { SubagentPanel } from "./SubagentPanel";
import { MessageVersionTree } from "../MessageVersionTree";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "../ui/dropdown-menu";
import {
  RefreshCw,
  Square,
  Pause,
  Play,
  Undo2,
  GitFork,
  ThumbsUp,
  ThumbsDown,
  Share2,
  GitBranch,
  MoreHorizontal,
  Copy,
  Check,
  type LucideIcon,
} from "lucide-react";
import { Bookmark, Layers } from "lucide-react";
import type { TaskState, PlanStep } from "../../store/taskStore";
import type { VersionNode, VersionEdge } from "../MessageVersionTree";

export interface TaskActions {
  onContextMenu: (e: React.MouseEvent, task: TaskState) => void;
  onEditPrompt: (taskId: string, prompt: string) => void;
  onRegenerate: (task: TaskState) => void;
  onFork: (taskId: string) => Promise<void>;
  onSaveAsTemplate: (taskId: string) => void;
  onUndo: (taskId: string) => Promise<void>;
  onRate: (taskId: string, rating: number) => Promise<void>;
  onShare: (taskId: string) => Promise<void>;
  onPause: (taskId: string) => Promise<void>;
  onForcePause: (taskId: string) => Promise<void>;
  onResume: (taskId: string) => Promise<void>;
  onStop: (taskId: string) => void;
  onRetry: (task: TaskState) => void;
  onOpenFile: (path: string, newContent?: string) => void;
  onExecutePlan: (taskId: string) => Promise<void>;
  onShowVersionTree: (taskId: string) => void;
  onSwitchVersion: (parentTaskId: string, versionTaskId: string) => void;
}

export type MessageItem =
  | { type: "pending"; id: string; prompt: string }
  | { type: "task"; id: string; task: TaskState };

interface SharedProps {
  currentSessionId: string | null;
  lang: string;
  t: (k: string, opts?: Record<string, unknown>) => string;
  inputRef: React.RefObject<HTMLTextAreaElement | null>;
  taskActions: TaskActions;
  versionTrees: Record<string, { nodes: VersionNode[]; edges: VersionEdge[] }>;
  planSteps: PlanStep[];
  planActiveId: string | null;
  convToTask: Map<string, string>;
  onOpenFile: (path: string, newContent?: string) => void;
}

interface MessageItemViewProps extends SharedProps {
  item: MessageItem;
  style?: CSSProperties;
}

function fullText(task: TaskState): string {
  return task.chunks.join("");
}

function SeqBadge({ seq }: { seq?: number }) {
  if (!seq) return null;
  return (
    <span
      className="text-2xs px-1.5 py-0.5 rounded font-mono select-none bg-primary/20 text-primary"
      title={`#${seq}`}
    >
      #{seq}
    </span>
  );
}

function StatusLabel({
  status,
  t,
}: {
  status: TaskState["status"];
  t: (k: string, opts?: Record<string, unknown>) => string;
}) {
  // G4: interrupted 显示可恢复中断标签（琥珀色，区别于 failed 红色 / completed 绿色）
  const text =
    status === "completed"
      ? t("chat.done")
      : status === "failed"
        ? t("chat.failed")
        : status === "interrupted"
          ? t("chat.interrupted")
          : "";
  const color =
    status === "completed"
      ? "text-success font-medium"
      : status === "failed"
        ? "text-destructive"
        : status === "interrupted"
          ? "text-amber-600 dark:text-amber-400 font-medium"
          : "";
  return text ? <span className={color}>{text}</span> : null;
}

function resolveRole(task: TaskState): NonNullable<TaskState["role"]> | null {
  if (task.role) return task.role;
  const phase = task.lifecycle?.phase;
  if (!phase) return null;
  if (["clarifying", "decomposing", "architect", "pm"].includes(phase)) return "pm";
  if (["executing", "developer", "dev"].includes(phase)) return "dev";
  if (["spec_review", "auditor", "review", "reviewer"].includes(phase)) return "reviewer";
  if (["convergence", "convergence_check", "merge"].includes(phase)) return "merge";
  return null;
}

function RoleBadge({ role, t }: { role: NonNullable<TaskState["role"]>; t: (k: string, opts?: Record<string, unknown>) => string }) {
  const config: Record<NonNullable<TaskState["role"]>, { label: string; cls: string }> = {
    pm: { label: "task.role.pm", cls: "bg-purple-100 text-purple-700 dark:bg-purple-900 dark:text-purple-200" },
    dev: { label: "task.role.dev", cls: "bg-blue-100 text-blue-700 dark:bg-blue-900 dark:text-blue-200" },
    reviewer: { label: "task.role.reviewer", cls: "bg-orange-100 text-orange-700 dark:bg-orange-900 dark:text-orange-200" },
    merge: { label: "task.role.merge", cls: "bg-green-100 text-green-700 dark:bg-green-900 dark:text-green-200" },
  };
  const c = config[role];
  return <span className={`text-[10px] px-1.5 py-0.5 rounded-full font-medium ${c.cls}`}>{t(c.label)}</span>;
}

function PromptBubble({
  task,
  inputRef,
  onEditPrompt,
  t,
}: {
  task: TaskState;
  inputRef: React.RefObject<HTMLTextAreaElement | null>;
  onEditPrompt: (taskId: string, prompt: string) => void;
  t: (k: string, opts?: Record<string, unknown>) => string;
}) {
  const role = resolveRole(task);
  const showRole = role && (task.isLongTask || task.lifecycle?.phase);
  return (
    <div className="flex justify-end group">
      {/* Phase 108: 用户气泡宽度上限 82%（对标 dsh 的 min(content*0.702, 82%)），
          避免长文本把整行撑满；纯视觉限制，不影响发送给 AI 的内容。 */}
      <div className="max-w-[82%] bg-primary text-primary-foreground rounded-2xl rounded-br-md px-4 py-2.5 text-sm leading-relaxed whitespace-pre-wrap break-words relative">
        {showRole && (
          <div className="flex justify-end mb-1">
            <RoleBadge role={role} t={t} />
          </div>
        )}
        {task.prompt}
        {task.status !== "running" && (
          // Phase 65: 小屏默认可见（触屏无 hover）；md: 以上 hover/focus 显示
          <div className="absolute -bottom-4 right-0 opacity-100 md:opacity-0 md:group-hover:opacity-100 md:focus-within:opacity-100 transition-opacity">
            <Button
              variant="outline"
              size="xs"
              onClick={() => {
                onEditPrompt(task.taskId, task.prompt);
                inputRef.current?.focus();
              }}
              aria-label={t("chat.editResend")}
              title={t("chat.editResend")}
            >
              {t("chat.editResend")}
            </Button>
          </div>
        )}
      </div>
    </div>
  );
}

function formatSecondsShort(seconds: number): string {
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  if (m === 0) return `${s}s`;
  if (s === 0) return `${m}m`;
  return `${m}m ${s}s`;
}

function TaskProgress({ task, t }: { task: TaskState; t: SharedProps["t"] }) {
  if (task.status !== "running") return null;

  // 长任务：使用 lifecycle phase + stepIndex/totalSteps + 已用时长的实测值
  const lc = task.lifecycle;
  if (task.isLongTask && lc) {
    const step = lc.stepIndex ?? 0;
    const total = lc.totalSteps ?? 0;
    const hasTotal = total > 0;
    const pct = hasTotal ? Math.max(5, Math.round((step / total) * 100)) : 0;
    // 诚实耗时原则（借鉴 deepseek-harness）：只显示已发生的实测值，不外推「预计剩余」。
    // 外推值在长尾步骤上会把「还剩 5s」说成 2 分钟，且每 tick 跳动反而更不可信。
    const elapsed = lc.estimate?.elapsedSeconds;
    return (
      <div className="mb-3 space-y-1.5" aria-live="polite">
        <div className="flex items-center justify-between text-2xs text-muted-foreground">
          <span className="capitalize">{lc.phase}</span>
          {hasTotal ? (
            <span>{t("chat.progressStep", { step: step + 1, total })}</span>
          ) : (
            <span>{t("chat.progressUnknown")}</span>
          )}
        </div>
        <div
          className="h-1.5 bg-muted rounded-full overflow-hidden"
          role="progressbar"
          aria-valuenow={pct}
          aria-valuemin={0}
          aria-valuemax={100}
        >
          <div
            className="h-full bg-primary transition-[width] duration-300 ease-out rounded-full"
            style={{ width: `${pct}%` }}
          />
        </div>
        {elapsed !== undefined && elapsed !== null && elapsed >= 0 && (
          <div className="text-2xs text-muted-foreground">
            {t("chat.elapsed", { time: formatSecondsShort(elapsed) })}
          </div>
        )}
      </div>
    );
  }

  // 通用任务进度
  const progress = task.progress;
  if (progress && progress.total > 0) {
    const pct = Math.max(5, Math.round((progress.step / progress.total) * 100));
    return (
      <div className="mb-3 space-y-1.5" aria-live="polite">
        <div className="flex items-center justify-between text-2xs text-muted-foreground">
          <span>{t("chat.executing")}</span>
          <span>{t("chat.progressStep", { step: progress.step, total: progress.total })}</span>
        </div>
        <div
          className="h-1.5 bg-muted rounded-full overflow-hidden"
          role="progressbar"
          aria-valuenow={pct}
          aria-valuemin={0}
          aria-valuemax={100}
        >
          <div
            className="h-full bg-primary transition-[width] duration-300 ease-out rounded-full"
            style={{ width: `${pct}%` }}
          />
        </div>
      </div>
    );
  }

  return null;
}

function TaskBody({
  task,
  running,
  lang: _lang,
  t,
  planSteps,
  planActiveId,
  onExecutePlan,
  onOpenFile,
}: {
  task: TaskState;
  running: boolean;
  lang: string;
  t: SharedProps["t"];
  planSteps: PlanStep[];
  planActiveId: string | null;
  onExecutePlan: (taskId: string) => Promise<void>;
  onOpenFile: (path: string, newContent?: string) => void;
}) {
  const showPlan =
    (task.status === "completed" || task.status === "running") && planSteps.length > 0 && task.taskId === planActiveId;
  return (
    <>
      {showPlan && (
        <div className="mb-3">
          <PlanProgress
            taskId={task.taskId}
            onExecute={() => onExecutePlan(task.taskId)}
            onRetryStep={() => onExecutePlan(task.taskId)}
          />
        </div>
      )}
      <TaskProgress task={task} t={t} />
      {/* 一条回复一张卡片：过程信息（思考/计划/工具）成行折叠，正文与失败提示挂在卡片里 */}
      <TrajectoryCard items={projectTrajectory(task, t)} agentId={task.agentId}>
        {task.errorMessage && <TaskErrorNotice message={task.errorMessage} code={task.errorCode} />}
        {task.chunks.length > 0 ? (
          <ResultRenderer
            chunks={task.chunks}
            content={fullText(task)}
            resultType={task.resultType || "text"}
            isStreaming={task.isStreaming}
            onApplyDiff={(path, newContent) => onOpenFile(path, newContent)}
          />
        ) : running ? (
          <StatusIndicator
            status="running"
            chunkCount={task.thinkingChunks?.length || 0}
            thinkingLength={task.thinkingChunks?.join("").length || 0}
          />
        ) : null}
      </TrajectoryCard>
    </>
  );
}

function TaskFooter({
  task,
  currentSessionId,
  taskActions,
  t,
}: {
  task: TaskState;
  currentSessionId: string | null;
  taskActions: TaskActions;
  t: (k: string, opts?: Record<string, unknown>) => string;
}) {
  // 复制反馈：2s 后自动还原图标。hook 必须早于下方的 early return。
  const [copied, setCopied] = useState(false);

  const handleCopy = () => {
    void navigator.clipboard
      ?.writeText(fullText(task))
      .then(() => {
        setCopied(true);
        setTimeout(() => setCopied(false), 2000);
      })
      .catch(() => {});
  };

  if (task.chunks.length === 0) return null;

  const actions: Array<{
    key: string;
    icon?: LucideIcon;
    label: string;
    onClick: () => void;
    variant?: "link" | "ghost";
    title?: string;
  }> = [];

  // 复制：对齐 ChatGPT/Claude，放在 hover 操作栏第一项。
  if (task.chunks.length > 0) {
    actions.push({
      key: "copy",
      icon: copied ? Check : Copy,
      label: copied ? t("resultRenderer.copied") : t("resultRenderer.copy"),
      onClick: handleCopy,
    });
  }
  if (task.status === "completed" && task.prompt) {
    actions.push({
      key: "regenerate",
      icon: RefreshCw,
      label: t("chat.regenerate"),
      onClick: () => taskActions.onRegenerate(task),
    });
  }
  if (task.variants && task.variants.length > 0) {
    actions.push({
      key: "variant-count",
      icon: Layers, label: `V${task.variants.length + 1}`,
      onClick: () => {},
    });
  }
  if (task.status === "completed" && currentSessionId) {
    actions.push({
      key: "fork",
      icon: GitFork,
      label: t("chat.fork"),
      onClick: () => taskActions.onFork(task.taskId),
    });
  }
  if (task.status === "completed") {
    actions.push({
      key: "save-template",
      icon: Bookmark, label: t("chat.saveAsTemplate"),
      onClick: () => taskActions.onSaveAsTemplate(task.taskId),
    });
  }
  if (task.status === "completed") {
    actions.push({
      key: "undo",
      icon: Undo2,
      label: t("chat.undo"),
      onClick: () => taskActions.onUndo(task.taskId),
    });
  }
  if (task.status === "completed") {
    actions.push({
      key: "like",
      icon: ThumbsUp,
      label: "",
      onClick: () => taskActions.onRate(task.taskId, 1),
      variant: "ghost",
      title: t("chat.like"),
    });
  }
  if (task.status === "completed") {
    actions.push({
      key: "dislike",
      icon: ThumbsDown,
      label: "",
      onClick: () => taskActions.onRate(task.taskId, -1),
      variant: "ghost",
      title: t("chat.dislike"),
    });
  }
  if (task.status === "completed" && currentSessionId) {
    actions.push({
      key: "share",
      icon: Share2,
      label: "",
      onClick: () => taskActions.onShare(task.taskId),
      variant: "ghost",
      title: t("chat.share"),
    });
  }

  // Phase 65: 核心按钮常显，次要按钮在小屏收入“更多”菜单。
  const primaryActions = actions.slice(0, 4);
  const secondaryActions = actions.slice(4);

  return (
    <div className="mt-3 pt-2 flex gap-2 text-2xs flex-wrap items-center">
      <StatusLabel status={task.status} t={t} />
      {primaryActions.map((a) => (
        <Button
          key={a.key}
          variant="ghost"
          size="icon-xs"
          className="h-6 w-6 p-0 rounded-md text-muted-foreground hover:text-foreground hover:bg-surface-3"
          onClick={a.onClick}
          title={a.title || a.label}
          aria-label={a.title || a.label}
        >
          {a.icon && <a.icon size={12} />}
        </Button>
      ))}
      {/* 次要按钮——桌面端平铺 */}
      {secondaryActions.map((a) => (
        <Button
          key={a.key}
          variant="ghost"
          size="icon-xs"
          className="h-6 w-6 p-0 text-muted-foreground hover:text-foreground hover:bg-surface-3 hidden md:inline-flex"
          onClick={a.onClick}
          title={a.title || a.label}
          aria-label={a.title || a.label}
        >
          {a.icon && <a.icon size={12} />}
        </Button>
      ))}
      {/* 次要按钮——小屏收入下拉菜单 */}
      {secondaryActions.length > 0 && (
        <DropdownMenu>
          <DropdownMenuTrigger
            className="inline-flex items-center justify-center h-auto px-1 py-0.5 rounded text-2xs text-muted-foreground hover:bg-secondary md:hidden"
            aria-label={t("phase65.moreActions")}
            title={t("phase65.moreActions")}
          >
            <MoreHorizontal size={12} />
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            {secondaryActions.map((a) => (
              <DropdownMenuItem
                key={a.key}
                onClick={a.onClick}
                aria-label={a.title || a.label}
              >
                {a.icon && <a.icon size={12} className="mr-2" />}
                {a.label || a.title}
              </DropdownMenuItem>
            ))}
          </DropdownMenuContent>
        </DropdownMenu>
      )}
    </div>
  );
}

function VersionSwitch({
  task,
  versionTrees,
  convToTask,
  taskActions,
  t,
}: {
  task: TaskState;
  versionTrees: Record<string, { nodes: VersionNode[]; edges: VersionEdge[] }>;
  convToTask: Map<string, string>;
  taskActions: TaskActions;
  t: (k: string, opts?: Record<string, unknown>) => string;
}) {
  const hasVersionTree = (task.status === "completed" || task.versions?.length) && task.convId;
  return (
    <>
      {hasVersionTree && (
        <div className="mt-2 flex items-center gap-2">
          <Button
            variant="ghost"
            size="xs"
            className="h-6 w-6 p-0 text-muted-foreground hover:text-foreground hover:bg-surface-3"
            onClick={() => taskActions.onShowVersionTree(task.taskId)}
            title={t("phase49.versionTree.title")}
            aria-label={t("phase49.versionTree.title")}
          >
            <GitBranch size={12} />
          </Button>
          {task.versions && task.versions.length > 0 && (
            <div className="flex gap-1">
              {task.versions.map((v, idx) => {
                const active = task.activeVersionTaskId === v.taskId;
                return (
                  <Button
                    key={v.taskId}
                    variant={active ? "default" : "outline"}
                    size="xs"
                    className="h-5 px-1.5 text-[10px]"
                    onClick={() => taskActions.onSwitchVersion(task.taskId, v.taskId)}
                  >
                    V{idx + 1}
                  </Button>
                );
              })}
            </div>
          )}
        </div>
      )}
      {versionTrees[task.taskId] && (
        <div className="mt-2">
          <MessageVersionTree
            nodes={versionTrees[task.taskId].nodes}
            edges={versionTrees[task.taskId].edges}
            activeId={task.convId}
            onSelect={(id) => {
              const versionTaskId = convToTask.get(id);
              if (versionTaskId && versionTaskId !== task.taskId) {
                taskActions.onSwitchVersion(task.taskId, versionTaskId);
              }
            }}
          />
        </div>
      )}
    </>
  );
}

function TaskControls({
  task,
  taskActions,
  t,
}: {
  task: TaskState;
  taskActions: TaskActions;
  t: (k: string, opts?: Record<string, unknown>) => string;
}) {
  const running = task.status === "running";
  return (
    <>
      {running && task.chunks.length > 0 && (
        <>
          <DropdownMenu>
            <DropdownMenuTrigger
              className="inline-flex items-center justify-center gap-1 mt-1.5 rounded-md border border-input bg-background hover:bg-accent hover:text-accent-foreground h-6 px-2 text-xs"
              title={t("chat.pause")} aria-label={t("chat.pause")} data-testid="task-pause"
            >
              <Pause size={12} />
            </DropdownMenuTrigger>
            <DropdownMenuContent align="start">
              <DropdownMenuItem data-testid="task-pause-now" onClick={() => taskActions.onPause(task.taskId)}>
                {t("chat.pause")}
              </DropdownMenuItem>
              <DropdownMenuItem
                onClick={() => taskActions.onForcePause(task.taskId)}
                className="text-amber-600 dark:text-amber-400"
              >
                {t("chat.forcePause")}
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
          <Button
            variant="outline"
            size="xs"
            className="text-destructive mt-1.5 gap-1"
            onClick={() => taskActions.onStop(task.taskId)}
          >
            <Square size={12} />
            {t("chat.stop")}
          </Button>
        </>
      )}
      {task.status === "paused" && (
        <Button
          variant="outline"
          size="xs"
          className="gap-1 mt-1.5"
          onClick={() => taskActions.onResume(task.taskId)}
          title={t("chat.resume")} data-testid="task-resume"
        >
          <Play size={12} /> {t("chat.resume")}
        </Button>
      )}
      {/* G4: interrupted 可恢复——重用 onResume（后端 resume_task 已接受 interrupted） */}
      {task.status === "interrupted" && (
        <Button
          variant="outline"
          size="xs"
          className="gap-1 mt-1.5 border-amber-400 text-amber-700 dark:text-amber-300"
          onClick={() => taskActions.onResume(task.taskId)}
          title={t("chat.resume")} data-testid="task-resume"
        >
          <Play size={12} /> {t("chat.resume")}
        </Button>
      )}
      {task.status === "failed" && task.prompt && (
        <Button variant="outline" size="xs" className="mt-1.5 gap-1" onClick={() => taskActions.onRetry(task)}>
          <RefreshCw size={12} />
          {t("chat.retry")}
        </Button>
      )}
    </>
  );
}

function TaskMessageView({
  task,
  style,
  shared,
}: {
  task: TaskState;
  style?: CSSProperties;
  shared: Omit<SharedProps, "item">;
}) {
  const running = task.status === "running";
  return (
    <div
      id={`task-${task.taskId}`}
      style={style}
      className={`space-y-3 message-enter pb-4 ${task.superseded ? "opacity-30" : ""}`}
      onContextMenu={(e) => shared.taskActions.onContextMenu(e, task)}
    >
      {task.prompt && (
        <PromptBubble
          task={task}
          inputRef={shared.inputRef}
          onEditPrompt={shared.taskActions.onEditPrompt}
          t={shared.t}
        />
      )}
      {task.routeReason && (
        <div className="flex justify-start">
          <span className="text-2xs text-muted-foreground ml-2">
            {task.agentId} · {task.routeReason}
          </span>
        </div>
      )}
      <div className="flex justify-start gap-2">
        <div className="flex flex-col items-center gap-1 shrink-0">
          <SeqBadge seq={task.msgSeq} />
        </div>
        <div className="max-w-[92%] md:max-w-[90%] min-w-0">
          <TaskBody
            task={task}
            running={running}
            lang={shared.lang}
            t={shared.t}
            planSteps={shared.planSteps}
            planActiveId={shared.planActiveId}
            onExecutePlan={shared.taskActions.onExecutePlan}
            onOpenFile={shared.onOpenFile}
          />
          <SubagentPanel taskId={task.taskId} status={task.status} initial={task.subTasks} />
          <TaskFooter
            task={task}
            currentSessionId={shared.currentSessionId}
            taskActions={shared.taskActions}
            t={shared.t}
          />
          <VersionSwitch
            task={task}
            versionTrees={shared.versionTrees}
            convToTask={shared.convToTask}
            taskActions={shared.taskActions}
            t={shared.t}
          />
          <TaskControls task={task} taskActions={shared.taskActions} t={shared.t} />
        </div>
      </div>
    </div>
  );
}

/** Batch 4a F2: convToTask 内容比较——Map 在每次任务更新（含流式 chunk）时都会重建
 * （新引用），按内容比较避免 memo 失效；值为 string，O(n) 便宜。 */
function convToTaskEqual(a: Map<string, string>, b: Map<string, string>): boolean {
  if (a === b) return true;
  if (a.size !== b.size) return false;
  for (const [k, v] of a) {
    if (b.get(k) !== v) return false;
  }
  return true;
}

// Batch 4a F2: React.memo——流式 chunk 只重渲正在更新的消息 item，历史消息跳过。
// 按 item.task / item.prompt 实体引用比较；taskActions 经 useTaskActions useMemo
// 稳定，t 仅随语言变化，inputRef/versionTrees/planSteps 引用稳定。
export const MessageItemView = memo(
  function MessageItemView({ item, style, ...shared }: MessageItemViewProps) {
    if (item.type === "pending") {
      return (
        <div style={style} className="flex justify-end message-enter pb-4">
          <div className="max-w-[82%] bg-primary/70 text-primary-foreground rounded-2xl rounded-br-md px-4 py-2.5 text-sm leading-relaxed whitespace-pre-wrap break-words">
            {item.prompt}
            <span className="inline-flex items-center ml-2 align-middle opacity-80">
              <LoadingCurve variant="spinner" size={14} />
            </span>
          </div>
        </div>
      );
    }
    return <TaskMessageView task={item.task} style={style} shared={shared} />;
  },
  (prev, next) => {
    if (prev.item.type !== next.item.type) return false;
    if (prev.item.type === "pending" && next.item.type === "pending") {
      if (prev.item.prompt !== next.item.prompt) return false;
    } else if (prev.item.type === "task" && next.item.type === "task") {
      if (prev.item.task !== next.item.task) return false;
    }
    return (
      prev.style === next.style &&
      prev.currentSessionId === next.currentSessionId &&
      prev.lang === next.lang &&
      prev.t === next.t &&
      prev.inputRef === next.inputRef &&
      prev.taskActions === next.taskActions &&
      prev.onOpenFile === next.onOpenFile &&
      prev.versionTrees === next.versionTrees &&
      prev.planSteps === next.planSteps &&
      prev.planActiveId === next.planActiveId &&
      convToTaskEqual(prev.convToTask, next.convToTask)
    );
  },
);

/**
 * 空会话占位（骨架屏 / 欢迎文案）。
 *
 * 注：这里曾接受 `onSuggestionClick`，用于点击示例提示 chip 后回填输入框。
 * Task 6.10 按用户反馈移除了固定示例 chips（对标 Claude / ChatGPT / Cursor 的极简空状态），
 * 该 prop 从此无人调用。为避免留下"收下却忽略"的死参数，已连同上层的
 * ChatView → MessageList 转发链一并移除；需要恢复 chips 时可从 git 历史取回。
 */
export function EmptyMessages({
  sessionLoading,
  t,
  onSuggestion,
}: {
  sessionLoading: boolean;
  t: (k: string, opts?: Record<string, unknown>) => string;
  onSuggestion?: (text: string) => void;
}) {
  // Phase 9-X: DSH 式首屏引导 chips——极简空状态下的一行轻量建议。
  // 点击即填充并发送（对标 deepseek-harness 的空会话引导）。仅在父组件提供
  // onSuggestion 时渲染；未提供则保持纯文案（兼容测试/受限视图）。
  const SUGGESTION_KEYS = [
    "chat.chipCode",
    "chat.chipExplain",
    "chat.chipWrite",
    "chat.chipPlan",
  ];
  if (sessionLoading) {
    return (
      <div className="px-2 md:px-6 py-4 space-y-4">
        {[
          { id: "start-1", align: "start", width: "w-3/4", bg: "bg-card border" },
          { id: "end", align: "end", width: "w-2/3", bg: "bg-primary/10" },
          { id: "start-2", align: "start", width: "w-3/4", bg: "bg-card border" },
        ].map((s) => (
          <div
            key={`session-skeleton-${s.id}`}
            className={`flex ${s.align === "end" ? "justify-end" : "justify-start"}`}
          >
            <div className={`rounded-2xl px-4 py-3 ${s.width} ${s.bg}`}>
              <Skeleton className="h-4 w-full mb-2" />
              <Skeleton className="h-4 w-3/4 mb-2" />
              <Skeleton className="h-4 w-1/2" />
            </div>
          </div>
        ))}
      </div>
    );
  }
  return (
    <div className="flex flex-col items-center justify-center h-full text-center select-none py-8">
      {/* Logo */}
      <div className="w-12 h-12 rounded-xl bg-muted flex items-center justify-center">
        <span className="text-foreground font-semibold text-lg tracking-tight">Y</span>
      </div>
      {/* 欢迎语 */}
      <h2 className="text-xl font-semibold tracking-tight mt-5">{t("chat.welcomeHeading")}</h2>
      <p className="text-sm text-muted-foreground mt-1.5 max-w-sm">{t("chat.emptyDesc")}</p>
      {/* Phase 9-X: DSH 式首屏引导 chips——轻量药丸，点击即发送一条建议 prompt */}
      {onSuggestion && (
        <div className="flex flex-wrap items-center justify-center gap-2 mt-6 max-w-md px-4">
          {SUGGESTION_KEYS.map((k) => {
            const text = t(k);
            return (
              <button
                key={k}
                type="button"
                data-testid="suggestion-chip"
                onClick={() => onSuggestion(text)}
                className="rounded-full border border-border bg-card px-3.5 py-1.5 text-xs text-foreground-secondary transition-colors hover:bg-secondary hover:text-foreground hover:border-border-hover"
              >
                {text}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}
