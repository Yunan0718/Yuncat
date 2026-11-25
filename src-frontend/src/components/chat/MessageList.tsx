import { forwardRef, useCallback, useEffect, useImperativeHandle, useMemo, useRef, useState, type CSSProperties } from "react";
import { useTaskStore } from "../../store/taskStore";
import { useShallow } from "zustand/react/shallow";
import { MessageItemView, EmptyMessages, type TaskActions, type MessageItem } from "./MessageItem";
import { VirtualizedMessageList, type VirtualizedMessageListHandle } from "./VirtualizedMessageList";
import type { VersionNode, VersionEdge } from "../MessageVersionTree";

export type { TaskActions };
export interface MessageListHandle {
  scrollToBottom(behavior?: ScrollBehavior): void;
  /** Task 2.3: 用户是否停留在底部（距底 < NEAR_BOTTOM_THRESHOLD） */
  isNearBottom(): boolean;
}

interface MessageListProps {
  sessionLoading: boolean;
  pendingPrompt: string;
  currentSessionId: string | null;
  lang: string;
  t: (k: string, opts?: Record<string, unknown>) => string;
  messagesEndRef: React.RefObject<HTMLDivElement | null>;
  inputRef: React.RefObject<HTMLTextAreaElement | null>;
  taskActions: TaskActions;
  versionTrees: Record<string, { nodes: VersionNode[]; edges: VersionEdge[] }>;
  /** Phase 9-X: 首屏引导 chip 点击回调——填充并发送一条建议 prompt（DSH 式空状态）。 */
  onSuggestion?: (text: string) => void;
  /** Task 2.3: 用户滚动位置变化时通知父组件（nearBottom=true 表示在底部附近）。
   * 对标 Claude Code / Cursor 流式期间 stick-to-bottom 行为：用户上滑后停止自动滚动，回到底部后恢复。 */
  onScrollNearBottomChange?: (nearBottom: boolean) => void;
}

/** Task 2.3: 距底 < 100px 视为"在底部"——主流聊天工具常用阈值（Cursor/Claude.ai/ChatGPT） */
const NEAR_BOTTOM_THRESHOLD = 100;

/** FE1: 虚拟化切换迟滞——>60 启用、<30 回退，防止消息数在阈值附近（流式期间频繁
 * 增长/清空）反复切换虚拟化导致 DOM 整树替换与滚动跳变；中间带（30-60）保持当前模式。 */
const VIRTUAL_ENABLE_THRESHOLD = 60;
const VIRTUAL_DISABLE_THRESHOLD = 30;

export const MessageList = forwardRef<MessageListHandle, MessageListProps>(function MessageList(
  { sessionLoading, pendingPrompt, currentSessionId, lang, t, messagesEndRef, inputRef, taskActions, versionTrees, onSuggestion, onScrollNearBottomChange },
  ref,
) {
  // P1 (2026-07-04): useShallow 避免多余重渲染——
  // 旧实现行 30 `Object.values(s.tasks)` 每次 render 生成新数组引用，行 31 `useTaskStore()` 订阅整个 store。
  // 新实现：tasks 用 useShallow 浅比较（task 对象引用稳定时不重渲染），
  // planSteps/planActiveId 用 useShallow 选两个字段而非整个 store。
  const tasks = useTaskStore(useShallow((s) => s.tasks));
  const taskList = useMemo(() => Object.values(tasks), [tasks]);
  const { planSteps, planActiveId } = useTaskStore(
    useShallow((s) => ({ planSteps: s.planSteps, planActiveId: s.planActiveId })),
  );
  const virtualRef = useRef<VirtualizedMessageListHandle>(null);
  // Task 2.3: 非虚拟化模式下的滚动容器（虚拟化模式通过 virtualRef.getScrollElement() 拿）
  const scrollContainerRef = useRef<HTMLDivElement>(null);
  // Task 2.3: 内部跟踪用户是否在底部附近——避免每次 scroll 都触发回调（仅状态翻转时通知）
  const nearBottomRef = useRef(true);

  const convToTask = useMemo(
    () =>
      new Map(
        taskList
          .filter((task): task is typeof task & { convId: string } => !!task.convId)
          .map((task) => [task.convId, task.taskId] as [string, string]),
      ),
    [taskList],
  );

  const items: MessageItem[] = useMemo(() => {
    const arr: MessageItem[] = [];
    if (pendingPrompt) {
      arr.push({ type: "pending", id: "pending", prompt: pendingPrompt });
    }
    for (const task of taskList) {
      arr.push({ type: "task", id: task.taskId, task });
    }
    return arr;
  }, [pendingPrompt, taskList]);

  // FE1: 迟滞状态机——初始渲染按当前数量直接定模式（长会话恢复时免非虚拟化闪切），
  // 之后仅在越过上下阈值时切换，中间带保持现状
  const [useVirtual, setUseVirtual] = useState(() => items.length > VIRTUAL_ENABLE_THRESHOLD);
  useEffect(() => {
    if (items.length > VIRTUAL_ENABLE_THRESHOLD) setUseVirtual(true);
    else if (items.length < VIRTUAL_DISABLE_THRESHOLD) setUseVirtual(false);
  }, [items.length]);

  // Task 2.3: 计算元素是否在底部附近——主流聊天工具用 clientHeight+scrollTop vs scrollHeight 比较
  const computeNearBottom = useCallback((el: HTMLElement | null): boolean => {
    if (!el) return true;
    const distance = el.scrollHeight - el.scrollTop - el.clientHeight;
    return distance < NEAR_BOTTOM_THRESHOLD;
  }, []);

  useImperativeHandle(
    ref,
    () => ({
      scrollToBottom: (behavior = "auto") => {
        if (useVirtual) {
          virtualRef.current?.scrollToBottom(behavior);
        } else {
          messagesEndRef.current?.scrollIntoView({ behavior });
        }
        // Task 2.3: 程序触发滚动到底部后，重置 nearBottom 状态（避免状态滞后）
        nearBottomRef.current = true;
        onScrollNearBottomChange?.(true);
      },
      isNearBottom: () => {
        if (useVirtual) {
          return computeNearBottom(virtualRef.current?.getScrollElement() ?? null);
        }
        return computeNearBottom(scrollContainerRef.current);
      },
    }),
    [useVirtual, messagesEndRef, onScrollNearBottomChange, computeNearBottom],
  );

  // Task 2.3: attach scroll listener 检测用户上滑——仅状态翻转时通知父组件
  // 虚拟化与非虚拟化模式统一处理：根据 useVirtual 选择 scroll element
  useEffect(() => {
    const el = useVirtual ? virtualRef.current?.getScrollElement() ?? null : scrollContainerRef.current;
    if (!el) return;
    const handleScroll = () => {
      const near = computeNearBottom(el);
      if (near !== nearBottomRef.current) {
        nearBottomRef.current = near;
        onScrollNearBottomChange?.(near);
      }
    };
    el.addEventListener("scroll", handleScroll, { passive: true });
    // 初始化时同步一次状态（避免父组件初始 ref 值不准）
    handleScroll();
    return () => {
      el.removeEventListener("scroll", handleScroll);
    };
  }, [useVirtual, onScrollNearBottomChange, computeNearBottom, items.length]);

  // Batch 4a F2 (Task 2.3 下沉): 流式 stick-to-bottom 内部化——本组件已订阅 taskList，
  // 再加一个 runningChunksTotal 内部 selector（返回 number，chunk 不变时不重渲）即可驱动
  // 滚动，ChatView 不再因每个流式 chunk 全树重渲。行为对齐主流聊天工具（瞬时滚动 +
  // 用户上滑后不打扰）。
  const runningChunksTotal = useTaskStore((s) => {
    let total = 0;
    for (const t of Object.values(s.tasks)) {
      if (t.status === "running") total += t.chunks.length;
    }
    return total;
  });
  const didInitialScrollRef = useRef(false);

  const scrollToBottomInternal = useCallback(
    (behavior: ScrollBehavior = "auto") => {
      if (useVirtual) {
        virtualRef.current?.scrollToBottom(behavior);
      } else {
        // jsdom 未实现 scrollIntoView——可选调用保持测试环境可用
        messagesEndRef.current?.scrollIntoView?.({ behavior });
      }
      nearBottomRef.current = true;
      onScrollNearBottomChange?.(true);
    },
    [useVirtual, messagesEndRef, onScrollNearBottomChange],
  );

  // 新消息到达（数量变化）/ 首次挂载 → 滚到底（对齐原 ChatView taskCount/pendingPrompt effect）
  useEffect(() => {
    scrollToBottomInternal("auto");
    didInitialScrollRef.current = true;
  }, [items.length, scrollToBottomInternal]);

  // 流式 chunks 增长且用户停留在底部 → stick-to-bottom（用户上滑后不打扰）
  useEffect(() => {
    if (!didInitialScrollRef.current) return;
    if (!nearBottomRef.current) return;
    scrollToBottomInternal("auto");
  }, [runningChunksTotal, scrollToBottomInternal]);

  const shared = {
    currentSessionId,
    lang,
    t,
    inputRef,
    taskActions,
    versionTrees,
    planSteps,
    planActiveId,
    convToTask,
    onOpenFile: taskActions.onOpenFile,
  };

  const renderMessage = (item: MessageItem, style?: CSSProperties) => (
    <MessageItemView key={item.id} item={item} style={style} {...shared} />
  );

  return (
    <div
      ref={scrollContainerRef}
      // FE1: 统一滚动容器——虚拟化模式下外层不再滚动（overflow-hidden），
      // 由 VirtualizedMessageList 内层唯一滚动；非虚拟化保持外层滚动
      className={`flex-1 px-2 md:px-6 py-4 message-list relative min-h-0 bg-surface-2 ${
        useVirtual ? "overflow-hidden" : "overflow-y-auto"
      }`}
    >
      {items.length === 0 ? (
        <EmptyMessages sessionLoading={sessionLoading} t={t} onSuggestion={onSuggestion} />
      ) : useVirtual ? (
        <VirtualizedMessageList items={items} renderItem={renderMessage} ref={virtualRef} />
      ) : (
        <>
          {items.map((item) => renderMessage(item))}
          <div ref={messagesEndRef} />
        </>
      )}
    </div>
  );
});
