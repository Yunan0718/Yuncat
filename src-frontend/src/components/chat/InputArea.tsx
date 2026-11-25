import { useTaskStore } from "../../store/taskStore";
import { Button } from "../ui/button";
import { ContextRing } from "../ContextRing";

import { LoadingCurve } from "../common/LoadingCurve";
import { SuggestionMenu } from "./SuggestionMenu";
import { SecurityModeControl } from "./SecurityModeControl";
import { Coins, Paperclip, Send, Mic, Square } from "lucide-react";
import type { ChatAttachment } from "./types";
// Task 2.11: 统一快捷键读取层——支持用户自定义 send_message 快捷键
import { matchShortcut } from "../../lib/shortcuts";
// Phase 73-A: 输入框 ✨ 优化按钮
import { usePromptOptimize } from "../../hooks/usePromptOptimize";
import { useVoiceInput, mapI18nToBCP47 } from "../../hooks/useVoiceInput";
import { PromptOptimizeButton } from "./PromptOptimizeButton";
import { OptimizedPromptPreview } from "./OptimizedPromptPreview";
// Task 6.17: 优化失败 toast 反馈
import { useEffect, useRef } from "react";
import { useTranslation } from "react-i18next";
import ModelSelector from "../ModelSelector";
import { rankSlashCommands } from "../../lib/slashFilter";
import { RelatedMemories } from "../RelatedMemories";
import type { PresetId } from "../../lib/securityPreset";
import { useToast } from "../../hooks/use-toast";

interface AgentItem {
  id: string;
  name: string;
}

interface SessionItem {
  session_id: string;
  title: string;
  msg_count: number;
  last_at: number;
}

interface AtMenuState {
  visible: boolean;
  query: string;
  cursorPos: number;
}

/** Phase 64: / 命令补全菜单状态 */
export interface SlashMenuState {
  visible: boolean;
  query: string;
  cursorPos: number;
}

/** Phase 64: 后端 /api/v1/commands 返回的命令定义 */
export interface SlashCommandDef {
  name: string;
  slash_command: string;
  description: string;
  category: string;
  local: boolean;
  arguments?: Array<{ name: string; type: string; description?: string }>;
}

interface InputAreaProps {
  input: string;
  setInput: (v: string) => void;
  sending: boolean;
  attachmentPreviews: ChatAttachment[];
  setAttachmentPreviews: (v: ChatAttachment[] | ((p: ChatAttachment[]) => ChatAttachment[])) => void;
  atMenu: AtMenuState;
  setAtMenu: (v: AtMenuState) => void;
  sessionsForAt: SessionItem[];
  agents: AgentItem[];
  inputRef: React.RefObject<HTMLTextAreaElement | null>;
  fileInputRef: React.RefObject<HTMLInputElement | null>;
  onSend: () => void;
  onCompact: () => void;
  onCancelRunning: () => void;
  handleAtAutocomplete: (text: string, cursorPos: number) => void;
  onAddAttachments: (files: FileList) => void;
  t: (k: string, opts?: Record<string, unknown>) => string;
  /** Phase 64: / 命令补全——由 ChatView 持有状态并传入 */
  slashMenu: SlashMenuState;
  setSlashMenu: (v: SlashMenuState) => void;
  commands: SlashCommandDef[];
  /** 权限档位（权限 + 审批打包）——对标 Codex 放在对话框内切换 */
  securityPreset: PresetId;
  onSecurityPresetChange: (p: PresetId) => void;
}

export function InputArea({
  input,
  setInput,
  sending,
  attachmentPreviews,
  setAttachmentPreviews,
  atMenu,
  setAtMenu,
  sessionsForAt,
  agents,
  inputRef,
  fileInputRef,
  onSend,
  onCompact,
  onCancelRunning,
  handleAtAutocomplete,
  onAddAttachments,
  t,
  slashMenu,
  setSlashMenu,
  commands,
  securityPreset,
  onSecurityPresetChange,
}: InputAreaProps) {
  const contextUsage = useTaskStore((s) => s.contextUsage);
  // Phase 73-A: 输入框 ✨ 优化
  const { optimize, loading: optimizing, result: optimizeResult, error: optimizeError, reset: resetOptimize } = usePromptOptimize();
  // Task 6.17: 优化失败（后端返回 error 字段）→ toast 提示 + 清空 error state
  const { toast } = useToast();
  useEffect(() => {
    if (optimizeError && optimizeError !== "empty") {
      toast(t("chat.optimizePrompt.failed"), "error");
      resetOptimize();
    }
  }, [optimizeError, toast, resetOptimize, t]);

  // 语音输入:Web Speech API 封装(Chrome/Edge 支持,Firefox 自动隐藏按钮)
  const { i18n } = useTranslation();
  const latestInputRef = useRef(input);
  latestInputRef.current = input;
  const { listening: voiceListening, supported: voiceSupported, toggle: toggleVoice } = useVoiceInput(
    mapI18nToBCP47(i18n.language),
    (text, isFinal) => {
      if (isFinal) {
        setInput(latestInputRef.current + text);
      }
    },
  );

  const insertAt = (insert: string) => {
    const bf = input.slice(0, atMenu.cursorPos);
    const af = input.slice(inputRef.current?.selectionStart ?? bf.length);
    setInput(bf + insert + af);
    setAtMenu({ visible: false, query: "", cursorPos: 0 });
    inputRef.current?.focus();
  };

  /** Phase 64: / 命令补全——从光标位置回找最近的 / */
  const handleSlashAutocomplete = (text: string, cursorPos: number) => {
    const beforeCursor = text.slice(0, cursorPos);
    // / 必须在行首或空格后（避免匹配代码中的除号）
    const m = beforeCursor.match(/(?:^|\s)(\/[a-z]*)$/i);
    if (!m) {
      if (slashMenu.visible) setSlashMenu({ visible: false, query: "", cursorPos: 0 });
      return;
    }
    const slashIdx = beforeCursor.length - m[1].length;
    setSlashMenu({ visible: true, query: m[1].slice(1).toLowerCase(), cursorPos: slashIdx });
  };

  /** Phase 64: 选中命令后插入到光标位置 */
  const insertSlash = (cmd: SlashCommandDef) => {
    const bf = input.slice(0, slashMenu.cursorPos);
    const af = input.slice(slashMenu.cursorPos + slashMenu.query.length + 1); // +1 跳过旧的 /query
    // 本地命令（带参数的 /agent）插入后保留空格让用户继续输入；
    // 无参数命令不留尾空格，避免用户每次都要手动删除。
    const suffix = cmd.arguments && cmd.arguments.length > 0 ? " " : "";
    setInput(bf + cmd.slash_command + suffix + af);
    setSlashMenu({ visible: false, query: "", cursorPos: 0 });
    inputRef.current?.focus();
  };

  /** Phase 64: 同时检测 @ 和 / */
  const handleChange = (v: string) => {
    setInput(v);
    const pos = inputRef.current?.selectionStart ?? v.length;
    handleAtAutocomplete(v, pos);
    handleSlashAutocomplete(v, pos);
  };

  const hasRunning = Object.values(useTaskStore((s) => s.tasks)).some((task) => task.status === "running");

  // 输入框自动增高：根据内容 scrollHeight 调整，max 200px 后出滚动条
  // 对标 ChatGPT / Claude.ai / Cursor 的多行输入体验。
  // max-height 由 inline style 单一来源控制，这里读取该值避免硬编码重复。
  useEffect(() => {
    const el = inputRef.current;
    if (!el) return;
    const maxHeight = parseInt(el.style.maxHeight, 10) || 200;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, maxHeight)}px`;
    // inputRef 是 ref 对象，引用恒定，列入依赖不会引发额外重跑；
    // 但不列的话规则无法确定这里读到的 ref 与上次渲染是同一个。
  }, [input, inputRef]);

  // Phase 64: / 命令过滤
  // 2026-09-16: 模糊匹配 + 相关度排序（见 lib/slashFilter.ts）——只前缀匹配时
  // 用户只记得关键字母就找不到命令（/cpt 找不到 /compact）
  const filteredCommands = slashMenu.visible ? rankSlashCommands(commands, slashMenu.query) : [];

  return (
    <footer className="px-4 sm:px-6 py-3 shrink-0 bg-surface-2">
      <div className="max-w-3xl mx-auto">
        {/* Phase 17 相关记忆：上一轮回答结束后自动搜一次，没命中就不渲染 */}
        <RelatedMemories />
        {attachmentPreviews.length > 0 && (
          <div className="flex gap-1.5 mb-2 overflow-x-auto">
            {attachmentPreviews.map((att) => (
              <div
                key={att.name + ":" + att.data.slice(0, 10)}
                className="relative shrink-0 bg-secondary border rounded-lg p-2 text-[10px] max-w-[160px]"
              >
                {att.type.startsWith("image/") ? (
                  <img
                    src={`data:${att.type};base64,${att.data}`}
                    alt={att.name}
                    className="max-h-16 rounded object-cover"
                  />
                ) : (
                  <span className="truncate block">{att.name}</span>
                )}
                <button
                  onClick={() => setAttachmentPreviews((p) => p.filter((x) => x !== att))}
                  className="absolute -top-1.5 -right-1.5 w-4 h-4 bg-destructive-solid text-destructive-foreground rounded-full text-[8px] flex items-center justify-center"
                  title={t("common.close")}
                  aria-label={t("common.close")}
                >
                  ×
                </button>
              </div>
            ))}
          </div>
        )}
        <div className="flex flex-col md:flex-row gap-2 items-stretch md:items-end">
          <input
            ref={fileInputRef}
            type="file"
            multiple
            onChange={(e) => {
              const fs = e.target.files;
              if (fs) onAddAttachments(fs);
              e.target.value = "";
            }}
            className="hidden"
            accept="image/*,.pdf,.txt,.csv,.json,.md,.py,.ts,.tsx,.js,.rs,.go,.java,.yaml,.yml,.toml"
          />
          {/* Phase 56-C4: 输入框统一为单容器，减少视觉碎片（对标 Claude Code / Cursor） */}
          <div className="flex-1 flex flex-col gap-1 rounded-2xl bg-surface-1 px-3 py-2 focus-within:ring-1 focus-within:ring-primary/20 transition-colors">
            <div className="flex items-end gap-2">
            <Button
              variant="ghost"
              size="icon"
              className="shrink-0 w-8 h-8 rounded-lg"
              onClick={() => fileInputRef.current?.click()}
              title={t("chat.addFile")}
              aria-label={t("chat.addFile")}
            >
              <Paperclip size={16} />
            </Button>
            <div className="flex-1 relative min-w-0">
              <textarea
                ref={inputRef}
                value={input}
                onChange={(e) => handleChange(e.target.value)}
                onKeyDown={(e) => {
                  // Phase 64: / 菜单可见时 Enter/Arrow/Escape 由 SuggestionMenu 处理
                  if (slashMenu.visible) {
                    if (e.key === "Enter" || e.key === "ArrowUp" || e.key === "ArrowDown" || e.key === "Escape") {
                      e.preventDefault();
                      e.stopPropagation();
                      return;
                    }
                  }
                  // Batch 3.8: send_message 默认 Enter 发送（对标 ChatGPT/Claude），Shift+Enter 换行。
                  // 用户可在快捷键设置中改回 Ctrl+Enter 或禁用。
                  if (matchShortcut(e, "send_message")) {
                    e.preventDefault();
                    if (atMenu.visible) {
                      e.stopPropagation();
                      return;
                    }
                    onSend();
                  }
                  if (e.key === "Escape" && hasRunning) {
                    e.preventDefault();
                    onCancelRunning();
                  }
                }}
                placeholder={t("chat.placeholder")}
                rows={1}
                className="w-full px-1 py-2 resize-none text-sm bg-transparent outline-none placeholder:text-muted-foreground overflow-y-auto"
                style={{ minHeight: 40, maxHeight: 200 }}
              />
              {/* Phase 64: / 命令补全菜单 */}
              <SuggestionMenu
                items={filteredCommands}
                visible={slashMenu.visible}
                onSelect={insertSlash}
                onClose={() => setSlashMenu({ visible: false, query: "", cursorPos: 0 })}
                getKey={(c) => c.slash_command}
                getLabel={(c) => c.slash_command}
                getDescription={(c) => c.description}
                header={t("phase64.slashHint")}
                renderBadge={(c) =>
                  c.local ? (
                    <span className="text-[8px] px-1 rounded bg-primary/10 text-primary shrink-0">local</span>
                  ) : null
                }
              />
              {atMenu.visible && (
                <div className="absolute bottom-full left-0 mb-1 w-72 bg-card border rounded-lg shadow-lg z-dropdown max-h-48 overflow-y-auto">
                  <div className="px-2 py-1 text-[9px] text-muted-foreground bg-muted/50 sticky top-0">
                    {t("phase49.inputArea.atHint")}
                  </div>
                  <div className="text-[9px] text-muted-foreground/70 px-2 py-0.5">Agents</div>
                  {agents
                    .filter(
                      (a) =>
                        !atMenu.query || a.id.includes(atMenu.query) || a.name.toLowerCase().includes(atMenu.query),
                    )
                    .map((a) => (
                      <button
                        key={a.id}
                        className="w-full text-left px-3 py-1 text-[11px] hover:bg-secondary flex items-center gap-2"
                        onClick={() => insertAt(`@agent:${a.id} `)}
                      >
                        <span className="text-entity-agent">@</span>agent:<strong>{a.id}</strong>
                        <span className="text-muted-foreground ml-auto">{a.name}</span>
                      </button>
                    ))}
                  <div className="text-[9px] text-muted-foreground/70 px-2 py-0.5">Chats</div>
                  {sessionsForAt
                    .filter(
                      (s) =>
                        !atMenu.query || s.session_id.includes(atMenu.query) || (s.title || "").includes(atMenu.query),
                    )
                    .slice(0, 5)
                    .map((s) => (
                      <button
                        key={s.session_id}
                        className="w-full text-left px-3 py-1 text-[11px] hover:bg-secondary flex items-center gap-2"
                        onClick={() => insertAt(`@session:${s.session_id} `)}
                      >
                        <span className="text-entity-workflow">@</span>session:
                        <strong className="truncate max-w-[120px]">{s.session_id.slice(0, 8)}</strong>
                        <span className="text-muted-foreground ml-auto text-[9px]">
                          {s.title || t("phase49.inputArea.defaultSessionTitle")}
                        </span>
                      </button>
                    ))}
                  <div className="text-[9px] text-muted-foreground/70 px-2 py-0.5">{t("phase49.inputArea.more")}</div>
                  {[
                    { key: "memory", label: t("phase49.inputArea.memoryHint") },
                    { key: "file", label: t("phase49.inputArea.fileHint") },
                    { key: "folder", label: t("phase49.inputArea.folderHint") },
                    { key: "web", label: t("phase49.inputArea.webHint") },
                    { key: "git", label: t("phase49.inputArea.gitHint") },
                  ]
                    .filter((h) => !atMenu.query || `${h.key}:${h.label}`.includes(atMenu.query))
                    .map((h) => (
                      <button
                        key={h.key}
                        className="w-full text-left px-3 py-1 text-[11px] hover:bg-secondary"
                        onClick={() => insertAt(`@${h.key}:${h.label} `)}
                      >
                        <span className="text-entity-agent">@</span>
                        {h.key}:{h.label}
                      </button>
                    ))}
                </div>
              )}
            </div>
            {/* 语音输入:Chrome/Edge 支持,Firefox 自动隐藏 */}
            {voiceSupported && (
              <Button
                variant="ghost"
                size="icon"
                className={`shrink-0 h-8 w-8 rounded-lg ${voiceListening ? "text-destructive animate-pulse" : "text-muted-foreground"}`}
                onClick={toggleVoice}
                title={t("chat.voiceInput")}
                aria-label={t("chat.voiceInput")}
                aria-pressed={voiceListening}
              >
                <Mic className="h-4 w-4" />
              </Button>
            )}
            {/* Phase 73-A: ✨ 优化按钮——空输入禁用，loading 时 spinner */}
            <PromptOptimizeButton
              disabled={!input.trim()}
              loading={optimizing}
              onClick={() => optimize(input)}
            />
            {hasRunning ? (
              <Button
                onClick={onCancelRunning}
                variant="destructive"
                className="w-full md:w-auto rounded-xl gap-1.5"
                aria-label={t("chat.stop")}
              >
                <Square size={16} fill="currentColor" />
                {t("chat.stop")}
              </Button>
            ) : (
              <Button
                onClick={() => onSend()}
                disabled={!input.trim() || sending}
                variant="capsule"
                className="w-full md:w-auto gap-1.5"
                aria-label={t("chat.send")}
                data-testid="chat-send"
              >
                {sending ? (
                  <LoadingCurve variant="spinner" size={16} color="currentColor" />
                ) : (
                  <>
                    <Send size={16} />
                    {t("chat.send")}
                  </>
                )}
              </Button>
            )}
            </div>
            {/* 控件都在大框底部：模型 / 强度 / 用量 / 安全模式（原来是框外单独一行） */}
            <div className="flex flex-wrap items-center gap-2 px-1">
              <ModelSelector variant="chat" />
              {contextUsage.tokens > 0 && (
                <span className="inline-flex items-center gap-1 text-[9px] text-muted-foreground/60 font-mono">
                  <Coins size={10} />
                  {(contextUsage.tokens / 1000).toFixed(1)}k / {(contextUsage.window / 1000).toFixed(0)}k
                </span>
              )}
              {(contextUsage.pct >= 50 || attachmentPreviews.length > 0) && <ContextRing onCompact={onCompact} />}
              <div className="ml-auto">
                <SecurityModeControl
                  preset={securityPreset}
                  setPreset={onSecurityPresetChange}
                  t={t}
                />
              </div>
            </div>
          </div>
        </div>
        {/* Phase 73-A: 优化结果预览——应用后替换输入框内容 */}
        {optimizeResult && (
          <OptimizedPromptPreview
            rawPrompt={input}
            result={optimizeResult}
            onApply={(optimized) => {
              setInput(optimized);
              resetOptimize();
              inputRef.current?.focus();
            }}
            onDiscard={resetOptimize}
          />
        )}
      </div>
    </footer>
  );
}
