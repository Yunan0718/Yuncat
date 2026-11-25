import { useEffect, useRef, useState } from "react";
import { useApi } from "../hooks/useApi";
import { useDeliberationStore, type RoleOutput } from "../store/deliberationStore";
import { sendWsMessage } from "../hooks/useWebSocket";
import DOMPurify from "dompurify";
import { marked } from "marked";
import { useTranslation } from "react-i18next";
import { agentDisplayName } from "../lib/agentMeta";

function renderMd(text: string): string {
  // M10 修复：marked.parse 返回 string | Promise<string>——用运行时类型守卫替代 as string 断言，
  // 防止 marked 全局配置改为 async 时静默产出 "[object Promise]"。
  const parsed = marked.parse(text);
  const html = typeof parsed === "string" ? parsed : "";
  return `<div class="markdown-body">${DOMPurify.sanitize(html, { ADD_ATTR: ["data-code"] })}</div>`;
}

interface Props {
  prompt: string;
  suggestedRoles: { id: string; name: string; prompt_template: string }[];
  onStartExecution: (consensus: string) => void;
  onBackToChat: () => void;
}

/**
 * Phase 10: 多 Agent 并列讨论面板
 *
 * 网格布局，每角色一张实时流式卡片。
 * 三阶段：提案 → 审查 → 共识。
 * 用户可作为讨论参与者发送消息，也可暂停/恢复讨论。
 */
export function DeliberationView({ prompt, suggestedRoles, onStartExecution, onBackToChat }: Props) {
  const { api } = useApi();
  const phase = useDeliberationStore((s) => s.phase);
  const active = useDeliberationStore((s) => s.active);
  const roles = useDeliberationStore((s) => s.roles);
  const consensus = useDeliberationStore((s) => s.consensus);
  const taskId = useDeliberationStore((s) => s.taskId);
  const userMessages = useDeliberationStore((s) => s.userMessages);
  const paused = useDeliberationStore((s) => s.paused);
  const initRoles = useDeliberationStore((s) => s.initRoles);
  const reset = useDeliberationStore((s) => s.reset);
  const setTaskId = useDeliberationStore((s) => s.setTaskId);
  const addUserMessage = useDeliberationStore((s) => s.addUserMessage);
  const setPaused = useDeliberationStore((s) => s.setPaused);
  const { t } = useTranslation();
  const [statusMsg, setStatusMsg] = useState("");
  const [started, setStarted] = useState(false);
  const [userInput, setUserInput] = useState("");
  // Phase 49-R4: 暂停按钮防抖——避免快速点击发送多个 WS 消息
  const [pauseBusy, setPauseBusy] = useState(false);
  const pauseTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // 启动讨论
  useEffect(() => {
    if (started || suggestedRoles.length === 0) return;
    setStarted(true);
    initRoles(suggestedRoles);

    let cancelled = false;
    (async () => {
      try {
        const res = await api<{ task_id: string }>("/api/v1/deliberation/start", {
          method: "POST",
          body: JSON.stringify({
            prompt,
            roles: suggestedRoles.map((r) => ({
              id: r.id,
              name: r.name,
              prompt_template: r.prompt_template,
            })),
          }),
        });
        if (res?.task_id) setTaskId(res.task_id);
      } catch {
        if (cancelled) return;
        setStatusMsg(t("phase49.deliberation.startFailed"));
        reset();
      }
    })();
    return () => { cancelled = true; };
  }, [suggestedRoles, prompt, started, api, initRoles, reset, setTaskId, t]);

  const isDone = phase === "consensus" && !active;

  const handleSendUserMessage = () => {
    const text = userInput.trim();
    if (!text || !taskId) return;
    addUserMessage(text);
    sendWsMessage({ type: "deliberation_user_message", task_id: taskId, message: text });
    setUserInput("");
  };

  const handleTogglePause = () => {
    if (!taskId || pauseBusy) return;
    const newPaused = !paused;
    setPaused(newPaused);
    sendWsMessage({ type: newPaused ? "deliberation_pause" : "deliberation_resume", task_id: taskId });
    // 防抖：500ms 内禁用按钮，避免快速点击发送多个 WS 消息
    setPauseBusy(true);
    if (pauseTimer.current) clearTimeout(pauseTimer.current);
    pauseTimer.current = setTimeout(() => setPauseBusy(false), 500);
  };

  // 组件卸载时清理防抖定时器
  useEffect(() => {
    return () => {
      if (pauseTimer.current) clearTimeout(pauseTimer.current);
    };
  }, []);

  // Phase 49-R1: 返回聊天时取消后端讨论任务——避免继续消耗 token
  const handleBackToChat = () => {
    if (taskId && !isDone) {
      // best-effort 取消，不等待结果——用户已决定返回
      api(`/api/v1/deliberation/${taskId}/cancel`, { method: "POST" }).catch(() => {});
    }
    onBackToChat();
  };

  return (
    <div className="flex flex-col h-full bg-background">
      {/* 顶部状态栏 */}
      <div className="border-b px-4 py-2 flex items-center gap-3 shrink-0">
        <div className="flex items-center gap-2">
          <span className={`w-2 h-2 rounded-full ${paused ? "bg-warning" : "bg-destructive animate-pulse"}`} />
          <span className="text-sm font-semibold">{t("phase49.deliberation.title")}</span>
          {paused && <span className="text-xs text-warning">{t("phase49.deliberation.paused")}</span>}
        </div>
        <div className="flex items-center gap-1 text-xs text-muted-foreground">
          {(["proposals", "reviews", "consensus"] as const).map((p, i) => (
            <span key={p} className="flex items-center gap-1">
              {i > 0 && <span className="text-muted-foreground">→</span>}
              <span
                className={
                  phase === p ? "font-semibold text-primary" : isDone && p === "consensus" ? "text-success" : ""
                }
              >
                {t(`phase49.deliberation.phase${p.charAt(0).toUpperCase() + p.slice(1)}`)}
              </span>
            </span>
          ))}
        </div>
        {/* 右侧操作组——统一 ml-auto 避免多个 ml-auto 元素冲突 */}
        <div className="ml-auto flex items-center gap-2">
          {statusMsg && <span className="text-xs text-destructive">{statusMsg}</span>}
          {/* 暂停/恢复按钮——讨论进行中可用 */}
          {!isDone && taskId && (
            <button
              onClick={handleTogglePause}
              disabled={pauseBusy}
              className={`text-xs px-2 py-0.5 border rounded hover:bg-secondary disabled:opacity-50 disabled:cursor-not-allowed ${
                paused ? "border-success text-success" : "border-warning text-warning"
              }`}
            >
              {paused ? t("phase49.deliberation.resume") : t("phase49.deliberation.pause")}
            </button>
          )}
          <button
            onClick={handleBackToChat}
            className="text-xs px-2 py-0.5 border rounded hover:bg-secondary"
          >
            {t("phase49.deliberation.backToChat")}
          </button>
        </div>
      </div>

      {/* 角色卡片网格 */}
      <div className="flex-1 overflow-auto p-4">
        <div
          className={`grid gap-3 ${roles.length === 1 ? "grid-cols-1" : roles.length === 2 ? "grid-cols-2" : "grid-cols-3"}`}
        >
          {roles.map((role) => (
            <RoleCard key={role.roleId} role={role} isDone={isDone} />
          ))}
        </div>

        {/* 用户消息区——作为讨论参与者发送的消息 */}
        {userMessages.length > 0 && (
          <div className="mt-3 space-y-1.5">
            {userMessages.map((msg, i) => (
              <div key={i} className="flex gap-2 items-start">
                <span className="text-xs font-semibold text-primary shrink-0 mt-0.5">
                  {t("phase49.deliberation.userRole")}
                </span>
                <div className="text-xs text-muted-foreground bg-primary/5 border border-primary/20 rounded px-2 py-1 flex-1">
                  {msg.text}
                </div>
              </div>
            ))}
          </div>
        )}

        {/* 共识区域 */}
        {isDone && consensus && (
          <div className="mt-4 border rounded-lg p-4 bg-secondary">
            <h3 className="text-sm font-semibold mb-2">{t("phase49.deliberation.consensus")}</h3>
            <div
              className="text-sm text-muted-foreground max-h-64 overflow-y-auto prose prose-sm max-w-none"
              dangerouslySetInnerHTML={{ __html: renderMd(consensus) }}
            />
          </div>
        )}
      </div>

      {/* 底部操作栏 */}
      <div className="border-t px-4 py-3 flex items-center gap-3 shrink-0 bg-secondary">
        {isDone ? (
          <>
            <button
              onClick={() => onStartExecution(consensus)}
              className="px-4 py-1.5 bg-primary text-white rounded text-sm font-semibold"
            >
              {t("phase49.deliberation.execute")}
            </button>
            <button
              onClick={() => {
                reset();
                setStarted(false);
              }}
              className="px-3 py-1.5 border rounded text-sm hover:bg-background"
            >
              {t("phase49.deliberation.restart")}
            </button>
          </>
        ) : (
          <>
            {/* 用户输入——作为讨论参与者发消息。
                暂停期间也允许输入——消息会入队，恢复后在下一阶段检查点注入。 */}
            <input
              type="text"
              value={userInput}
              onChange={(e) => setUserInput(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); handleSendUserMessage(); } }}
              placeholder={t("phase49.deliberation.userInputPlaceholder")}
              disabled={!taskId}
              className="flex-1 text-sm px-3 py-1.5 bg-background border rounded focus:outline-none focus:ring-1 focus:ring-primary disabled:opacity-50"
            />
            <button
              onClick={handleSendUserMessage}
              disabled={!userInput.trim() || !taskId}
              className="px-3 py-1.5 bg-primary text-white rounded text-sm font-medium disabled:opacity-50"
            >
              {t("phase49.deliberation.send")}
            </button>
          </>
        )}
        {!isDone && (
          <span className="text-[10px] text-muted-foreground shrink-0">
            {t("phase49.deliberation.rolesResponded", {
              responded: roles.filter((r) => r.output.length > 0).length,
              total: roles.length,
            })}
          </span>
        )}
      </div>
    </div>
  );
}

/** 单个角色卡片——实时流式渲染 */
function RoleCard({ role, isDone }: { role: RoleOutput; isDone: boolean }) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState(false);

  const hasContent = role.output.length > 0;
  const preview = role.output.slice(0, 200);
  const rest = role.output.slice(200);

  return (
    <div
      className={`border rounded-lg p-3 transition-colors ${
        hasContent ? "border-border bg-background" : "border-border bg-background opacity-60"
      }`}
    >
      <div className="flex items-center gap-2 mb-1">
        <span className="font-semibold text-sm">{role.name}</span>
        {role.agentId && (
          <span className="text-[9px] text-muted-foreground/50">
            {agentDisplayName(role.agentId)}
          </span>
        )}
        {hasContent && !isDone && <span className="w-1.5 h-1.5 rounded-full bg-primary animate-pulse" />}
        {hasContent && isDone && <span className="text-[10px] text-success">{t("legacy.uniWs_769d")}</span>}
      </div>
      <div className="text-xs text-muted-foreground max-h-32 overflow-y-auto">
        {hasContent ? (
          <>
            <div
              className="prose prose-sm prose-invert max-w-none"
              dangerouslySetInnerHTML={{ __html: renderMd(expanded ? role.output : preview) }}
            />
            {rest.length > 0 && !expanded && (
              <button onClick={() => setExpanded(true)} className="text-primary ml-1 hover:underline text-[11px]">
                {t("phase49.deliberation.expandAll")}
              </button>
            )}
            {expanded && rest.length > 0 && (
              <button onClick={() => setExpanded(false)} className="text-primary ml-1 hover:underline text-[11px]">
                {t("phase49.deliberation.collapse")}
              </button>
            )}
          </>
        ) : (
          <span className="italic text-muted-foreground">{t("phase49.deliberation.waitingResponse")}</span>
        )}
      </div>
    </div>
  );
}
