import { useEffect, useRef, forwardRef, useImperativeHandle, useState, useCallback } from "react";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import { WebglAddon } from "@xterm/addon-webgl";
import { SearchAddon } from "@xterm/addon-search";
import "@xterm/xterm/css/xterm.css";
import { ChevronDown, History, Trash2, Plus, X as XIcon } from "lucide-react";
import { useAppStore } from "../../store/appStore";
import { useTranslation } from "react-i18next";
import i18next from "i18next";
import { TerminalFindBar } from "./TerminalFindBar";
import { useResizablePanel } from "../../hooks/useResizablePanel";

export interface TerminalHandle {
  sendCommand: (cmd: string) => void;
}

interface TerminalTab {
  id: string;
  label: string;
  sessionId: string;
}

// Phase 66: localStorage 持久化 key
const CMD_HISTORY_KEY = "yuncat:terminal:cmdHistory";
const TABS_KEY = "yuncat:terminal:tabs";
const ACTIVE_TAB_KEY = "yuncat:terminal:activeTab";
const MAX_HISTORY = 200;

// Phase 66: 导出纯函数供单元测试（不影响运行时行为）
export function loadCmdHistory(): string[] {
  try {
    const raw = localStorage.getItem(CMD_HISTORY_KEY);
    if (!raw) return [];
    const arr = JSON.parse(raw);
    return Array.isArray(arr) ? arr.slice(0, MAX_HISTORY) : [];
  } catch {
    return [];
  }
}

export function loadTabs(): TerminalTab[] {
  const defaultLabel = i18next.t("terminalPanel.tabDefault", "终端");
  try {
    const raw = localStorage.getItem(TABS_KEY);
    if (!raw) return [{ id: "main", label: defaultLabel, sessionId: "" }];
    const arr = JSON.parse(raw);
    if (!Array.isArray(arr) || arr.length === 0) {
      return [{ id: "main", label: defaultLabel, sessionId: "" }];
    }
    return arr;
  } catch {
    return [{ id: "main", label: defaultLabel, sessionId: "" }];
  }
}

export function loadActiveTab(): string {
  try {
    return localStorage.getItem(ACTIVE_TAB_KEY) || "main";
  } catch {
    return "main";
  }
}

// FE5: 补全 ANSI 16 色（0-15），避免 xterm 回退内置默认色与 UI 主题不一致。
// 色值来源：VS Code 内置终端 ANSI 色板（Dark+ / Light+ 主题，
// microsoft/vscode src/vs/workbench/contrib/terminal/common/terminalColorRegistry.ts）。
interface TerminalAnsiColors {
  black: string;
  red: string;
  green: string;
  yellow: string;
  blue: string;
  magenta: string;
  cyan: string;
  white: string;
  brightBlack: string;
  brightRed: string;
  brightGreen: string;
  brightYellow: string;
  brightBlue: string;
  brightMagenta: string;
  brightCyan: string;
  brightWhite: string;
}

const ANSI_DARK: TerminalAnsiColors = {
  black: "#000000", red: "#cd3131", green: "#0dbc79", yellow: "#e5e510",
  blue: "#2472c8", magenta: "#bc3fbc", cyan: "#11a8cd", white: "#e5e5e5",
  brightBlack: "#666666", brightRed: "#f14c4c", brightGreen: "#23d18b",
  brightYellow: "#f5f543", brightBlue: "#3b8eea", brightMagenta: "#d670d6",
  brightCyan: "#29b8db", brightWhite: "#ffffff",
};

const ANSI_LIGHT: TerminalAnsiColors = {
  black: "#000000", red: "#cd3131", green: "#00bc00", yellow: "#949800",
  blue: "#0451a5", magenta: "#bc05bc", cyan: "#0598bc", white: "#555555",
  brightBlack: "#666666", brightRed: "#cd3131", brightGreen: "#14ce14",
  brightYellow: "#b5ba00", brightBlue: "#0451a5", brightMagenta: "#bc05bc",
  brightCyan: "#0598bc", brightWhite: "#a5a5a5",
};

interface TerminalThemeColors extends TerminalAnsiColors {
  background: string;
  foreground: string;
  cursor: string;
}

/* ── xterm 终端工厂 ── */
function createTerminal(): Terminal {
  const isDark = document.documentElement.classList.contains("dark");
  const cs = getComputedStyle(document.documentElement);
  return new Terminal({
    fontSize: 12,
    fontFamily: "'Cascadia Mono', 'Consolas', 'Fira Code', monospace",
    theme: isDark
      ? {
          background: cs.getPropertyValue("--background").trim() || "#0F0F11",
          foreground: cs.getPropertyValue("--foreground").trim() || "#EDEDF0",
          cursor: cs.getPropertyValue("--primary").trim() || "#6E79F6",
          ...ANSI_DARK,
        }
      : {
          background: cs.getPropertyValue("--secondary").trim() || "#F0F0F2",
          foreground: cs.getPropertyValue("--foreground").trim() || "#17161A",
          cursor: cs.getPropertyValue("--primary").trim() || "#5B63D3",
          ...ANSI_LIGHT,
        },
    cursorBlink: true,
    cursorStyle: "bar",
    scrollback: 10000,
    allowProposedApi: true,
  });
}

export function buildDarkTheme(): TerminalThemeColors {
  const cs = getComputedStyle(document.documentElement);
  return {
    background: cs.getPropertyValue("--background").trim() || "#0F0F11",
    foreground: cs.getPropertyValue("--foreground").trim() || "#EDEDF0",
    cursor: cs.getPropertyValue("--primary").trim() || "#6E79F6",
    ...ANSI_DARK,
  };
}
export function buildLightTheme(): TerminalThemeColors {
  const cs = getComputedStyle(document.documentElement);
  return {
    background: cs.getPropertyValue("--secondary").trim() || "#F0F0F2",
    foreground: cs.getPropertyValue("--foreground").trim() || "#17161A",
    cursor: cs.getPropertyValue("--primary").trim() || "#5B63D3",
    ...ANSI_LIGHT,
  };
}

/**
 * Batch 4a F3: WS 连接 guard 状态机——「同一时刻只允许一个连接尝试」。
 * 修复：原 wsStrictGuard 置 true 后全文件无重置，onclose 的 3s 重连 connectWS
 * 被 guard 挡死，后端重启后终端永久失联。现于 onopen / onclose / unmount 释放槽位。
 * 导出纯类供单元测试（fake timers 验证 onclose → 3s 重连）。
 */
export class WsConnectionGuard {
  private inFlight = false;
  private timer: ReturnType<typeof setTimeout> | null = null;

  constructor(
    private readonly connect: () => void,
    private readonly delayMs = 3000,
  ) {}

  /** 发起连接——已有尝试在途则拒绝（返回 false） */
  tryConnect(): boolean {
    if (this.inFlight) return false;
    this.inFlight = true;
    this.connect();
    return true;
  }

  /** ws.onopen——连接已建立，释放尝试槽 */
  onOpen(): void {
    this.inFlight = false;
  }

  /** ws.onclose——释放尝试槽；此前 auth 成功过则调度 delayMs 后重连 */
  onClose(hadAuth: boolean, canReconnect: () => boolean): void {
    this.inFlight = false;
    this.clearTimer();
    if (!hadAuth) return;
    this.timer = setTimeout(() => {
      this.timer = null;
      if (canReconnect()) this.tryConnect();
    }, this.delayMs);
  }

  /** unmount 清理——取消 pending 重连并释放槽位（StrictMode 重挂载安全） */
  dispose(): void {
    this.clearTimer();
    this.inFlight = false;
  }

  private clearTimer(): void {
    if (this.timer) {
      clearTimeout(this.timer);
      this.timer = null;
    }
  }
}

/**
 * Phase 21: 嵌入式终端面板 (XTerm.js)
 * Phase 44: wordWrap·scrollback·theme reactivity·fit on resize·auto-reconnect
 */
export const TerminalPanel = forwardRef<TerminalHandle, { collapsed: boolean; onToggle: () => void }>(
  function TerminalPanel({ collapsed, onToggle }, ref) {
    const { t } = useTranslation();
    const port = useAppStore((s) => s.port);
    const token = useAppStore((s) => s.token);
    const backendReady = useAppStore((s) => s.backendReady);
    const theme = useAppStore((s) => s.theme); // ← 响应暗色/亮色切换
    const terminalPendingCommand = useAppStore((s) => s.terminalPendingCommand);
    const setTerminalPendingCommand = useAppStore((s) => s.setTerminalPendingCommand);
    const termRef = useRef<HTMLDivElement>(null);
    const containerRef = useRef<HTMLDivElement>(null);
    const wsRef = useRef<WebSocket | null>(null);
    const termInstance = useRef<Terminal | null>(null);
    const fitAddonRef = useRef<FitAddon | null>(null);
    const [tabs, setTabs] = useState<TerminalTab[]>(() => loadTabs());
    const [activeTabId, setActiveTabId] = useState(() => loadActiveTab());
    // 保持 ref 同步——避免 connectWS onclose 闭包捕获 stale tabId
    useEffect(() => {
      activeTabIdRef.current = activeTabId;
      try { localStorage.setItem(ACTIVE_TAB_KEY, activeTabId); } catch { /* ignore */ }
    }, [activeTabId]);
    // P0-6.5: 终端面板可拖拽调整高度——用 useResizablePanel 替换手写拖拽代码
    // 持久化到 localStorage，重启后保留用户偏好（对标 VS Code Panel 高度持久化）
    const { size: height, dragHandleProps: termDragProps } = useResizablePanel({
      storageKey: "yuncat.workspaceTerminal.height",
      // 对标 VSCode 集成终端：默认高度约 25vh（紧凑），避免打开时占据半个屏幕
      initialSize: Math.max(200, Math.floor(window.innerHeight * 0.25)),
      minSize: 100,
      maxSize: Math.floor(window.innerHeight * 0.6),
      side: "bottom",
    });
    const [showHistory, setShowHistory] = useState(false);
    const [showFindBar, setShowFindBar] = useState(false); // Phase 66: 可视化查找栏
    const [historyIdx, setHistoryIdx] = useState(-1); // Phase 66: 历史弹窗键盘导航索引
    const cmdHistoryRef = useRef<string[]>(loadCmdHistory());
    const cmdBufferRef = useRef("");
    const activeTabIdRef = useRef(activeTabId);
    const searchAddonRef = useRef<SearchAddon | null>(null);
    const showFindBarRef = useRef(false); // Phase 66: 同步 showFindBar 避免闭包陈旧

    useEffect(() => {
      showFindBarRef.current = showFindBar;
    }, [showFindBar]);

    // Phase 66: beforeunload 保存 tabs + activeTab 状态
    useEffect(() => {
      const saveTabs = () => {
        try {
          localStorage.setItem(TABS_KEY, JSON.stringify(tabs));
          localStorage.setItem(ACTIVE_TAB_KEY, activeTabId);
        } catch { /* ignore */ }
      };
      window.addEventListener("beforeunload", saveTabs);
      return () => window.removeEventListener("beforeunload", saveTabs);
    }, [tabs, activeTabId]);

    // Phase 66: tabs 变化时也实时保存（不依赖 beforeunload）
    useEffect(() => {
      try { localStorage.setItem(TABS_KEY, JSON.stringify(tabs)); } catch { /* ignore */ }
    }, [tabs]);

    useImperativeHandle(
      ref,
      () => ({
        sendCommand: (cmd: string) => {
          const ws = wsRef.current;
          if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ type: "input", data: cmd + "\n" }));
          }
        },
      }),
      [],
    );

    const addTab = useCallback(() => {
      const id = `tab-${Date.now()}`;
      setTabs((prev) => [...prev, { id, label: i18next.t("terminalPanel.tabN", { n: prev.length + 1 }), sessionId: "" }]);
      setActiveTabId(id);
      wsRef.current?.send(JSON.stringify({ type: "new_tab" }));
    }, []);

    const closeTab = useCallback(
      (tabId: string) => {
        // Phase 47: 通知后端关闭终端会话
        const tab = tabs.find((t) => t.id === tabId);
        if (tab?.sessionId) {
          wsRef.current?.send(JSON.stringify({ type: "close_tab", session_id: tab.sessionId }));
        }
        setTabs((prev) => {
          if (prev.length <= 1) return prev;
          const next = prev.filter((t) => t.id !== tabId);
          if (activeTabId === tabId) {
            setActiveTabId(next[0]?.id || "main");
          }
          return next;
        });
      },
      [activeTabId, tabs],
    );

    // ── WebSocket 连接 —— 对标主流：不因 StrictMode 双重挂载创建多个连接
    // Batch 4a F3: guard 语义改为「同一时刻只允许一个连接尝试」——onopen/onclose
    // 释放槽位，onclose 的 3s 重连可再次进入（原 wsStrictGuard 永不重置，断线即永久失联）
    const wsGuardRef = useRef<WsConnectionGuard | null>(null);
    const wsAuthRef = useRef(false);
    const connectWS = useCallback(() => {
      if (!port || !token) return;
      if (!wsGuardRef.current) {
        wsGuardRef.current = new WsConnectionGuard(() => {
          wsAuthRef.current = false;
          const ws = new WebSocket(`ws://127.0.0.1:${port}/ws/terminal`);
          wsRef.current = ws;

          ws.onopen = () => {
            // 连接已建立——释放尝试槽（StrictMode 重挂载/重连路径可再次进入）
            wsGuardRef.current?.onOpen();
            ws.send(JSON.stringify({ type: "auth", token }));
          };

          ws.onmessage = (event) => {
            wsAuthRef.current = true;
            const term = termInstance.current;
            if (!term) return;
            try {
              const msg = JSON.parse(event.data);
              if (msg.type === "output") {
                term.write(msg.data);
              } else if (msg.type === "agent_block") {
                const { command, exit_code, agent_id } = msg;
                term.write(`\r\n\x1b[48;5;22m\x1b[K [Agent:${agent_id}] ${command} → exit ${exit_code} \x1b[0m\r\n`);
              } else if (msg.type === "terminal_mode") {
                // 后端告知这是真终端还是降级管道。降级时没有提示符也没有回显，
                // 不说明白用户只会以为终端坏了（best-effort：只提示一次，不影响会话）。
                if (msg.pty === false) {
                  term.write(`\r\n\x1b[33m${i18next.t("terminalPanel.pipeHint")}\x1b[0m\r\n`);
                }
              } else if (msg.type === "tab_created") {
                setTabs((prev) =>
                  prev.map((t) =>
                    t.id === activeTabIdRef.current
                      ? { ...t, sessionId: msg.session_id }
                      : t,
                  ),
                );
              }
            } catch {
              term.write(event.data);
            }
          };

          ws.onclose = () => {
            // 已被 unmount 或新连接取代——不动 guard（新连接持有它）
            if (wsRef.current !== ws) return;
            // Phase 47: 只有之前 auth 成功过的连接才重连——握手 403 时 e.code 可能是 1006 无法分辨
            wsGuardRef.current?.onClose(wsAuthRef.current, () => backendReady);
          };
        });
      }
      wsGuardRef.current.tryConnect();
    }, [port, token, backendReady]); // activeTabId → ref，避免闭包陈旧

    // ── 初始化终端 ──
    useEffect(() => {
      if (!backendReady || !port || !token || !termRef.current) return;
      if (termInstance.current) return; // 已初始化

      const term = createTerminal();
      termInstance.current = term;
      const fitAddon = new FitAddon();
      fitAddonRef.current = fitAddon;
      term.loadAddon(fitAddon);
      // Phase 47: WebGL 加速 + 搜索 + clipboard 快捷键
      try {
        const webglAddon = new WebglAddon();
        // FE5: WebGL context 丢失（OOM / 系统休眠恢复）时安全降级——dispose 后
        // xterm 自动回退到默认 DOM renderer，避免终端黑屏无法恢复。
        webglAddon.onContextLoss(() => {
          webglAddon.dispose();
          console.warn("[TerminalPanel] WebGL context lost, falling back to DOM renderer");
        });
        term.loadAddon(webglAddon);
      } catch {
        /* fallback to canvas */
      }
      const searchAddon = new SearchAddon();
      term.loadAddon(searchAddon);
      searchAddonRef.current = searchAddon;
      // Ctrl+Shift+F → 打开终端内搜索. Ctrl+Shift+C/V → 复制/粘贴
      term.attachCustomKeyEventHandler((e: KeyboardEvent) => {
        if ((e.ctrlKey || e.metaKey) && e.shiftKey && e.key === "f") {
          e.preventDefault();
          setShowFindBar((v) => !v); // Phase 66: 切换可视化 FindBar
          return false;
        }
        // Phase 47: 对标 VSCode —— Ctrl+Shift+C 复制选中，Ctrl+Shift+V 粘贴
        if ((e.ctrlKey || e.metaKey) && e.shiftKey && e.key === "c" && term.hasSelection()) {
          const sel = term.getSelection();
          navigator.clipboard.writeText(sel).catch(() => {});
          return false;
        }
        if ((e.ctrlKey || e.metaKey) && e.shiftKey && e.key === "v") {
          navigator.clipboard
            .readText()
            .then((text) => {
              const ws = wsRef.current;
              if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "input", data: text }));
            })
            .catch(() => {});
          return false;
        }
        if (e.key === "Escape") {
          if (showFindBarRef.current) {
            setShowFindBar(false);
            return false;
          }
          searchAddon.findNext("");
          return false;
        }
        return true;
      });
      term.open(termRef.current!);
      fitAddon.fit();

      term.onData((data) => {
        const ws = wsRef.current;
        if (ws && ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ type: "input", data }));
          if (data === "\r" || data === "\n") {
            const cmd = cmdBufferRef.current.trim();
            const hist = cmdHistoryRef.current;
            if (cmd && cmd !== hist[hist.length - 1]) {
              hist.push(cmd);
              if (hist.length > MAX_HISTORY) hist.shift();
              // Phase 66: 持久化到 localStorage
              try { localStorage.setItem(CMD_HISTORY_KEY, JSON.stringify(hist)); } catch { /* ignore */ }
            }
            cmdBufferRef.current = "";
          } else if (data === "\x7f" || data === "\b") {
            cmdBufferRef.current = cmdBufferRef.current.slice(0, -1);
          } else if (data.length === 1 && data.charCodeAt(0) >= 32) {
            cmdBufferRef.current += data;
          }
        }
      });

      term.onResize(({ cols, rows }) => {
        const ws = wsRef.current;
        if (ws && ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ type: "resize", cols, rows }));
        }
      });

      connectWS();

      return () => {
        // Batch 4a F3: unmount 释放 guard（取消 pending 重连 + 尝试槽）——StrictMode 重挂载安全
        wsGuardRef.current?.dispose();
        wsGuardRef.current = null;
        term.dispose();
        termInstance.current = null;
        wsRef.current?.close();
        wsRef.current = null;
      };
    }, [backendReady, port, token, connectWS]);

    // ── 主题响应 ──
    useEffect(() => {
      const term = termInstance.current;
      if (!term) return;
      const isDark =
        theme === "dark" || (theme === "system" && window.matchMedia("(prefers-color-scheme: dark)").matches);
      term.options.theme = isDark ? buildDarkTheme() : buildLightTheme();
    }, [theme]);

    // ── 面板高度变化时重 fit ──
    useEffect(() => {
      fitAddonRef.current?.fit();
    }, [height]);

    // ── 外部推送命令（文件右键"在集成终端中打开"） ──
    useEffect(() => {
      if (!terminalPendingCommand) return;
      const ws = wsRef.current;
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: "input", data: terminalPendingCommand + "\n" }));
      }
      setTerminalPendingCommand(null);
    }, [terminalPendingCommand, setTerminalPendingCommand]);

    // ── 窗口 resize 时重 fit ──

    useEffect(() => {
      const handler = () => fitAddonRef.current?.fit();
      window.addEventListener("resize", handler);
      return () => window.removeEventListener("resize", handler);
    }, []);

    // P0-6.5: 拖拽逻辑已迁移到 useResizablePanel hook（termDragProps）

    if (collapsed) {
      return (
        <button
          onClick={onToggle}
          className="h-8 border-t flex items-center gap-1.5 px-3 text-xs text-muted-foreground hover:bg-secondary shrink-0"
        >
          <ChevronDown size={12} className="rotate-180" /> {t("chat.terminal")}
        </button>
      );
    }

    return (
      <div ref={containerRef} data-terminal-container className="border-t shrink-0 flex flex-col bg-card animate-terminal-enter" style={{ height }}>
        {/* 拖拽分割条 — P0-6.5: 由 useResizablePanel 提供 dragHandleProps */}
        <div
          {...termDragProps}
          className="h-1 bg-border hover:bg-muted-foreground/40 cursor-ns-resize shrink-0 active:bg-primary/30 transition-colors"
          role="separator"
          aria-orientation="horizontal"
          aria-label={t("chat.resizeTerminalAriaLabel")}
        />

        {/* Tab 栏 */}
        <div className="h-7 flex items-center bg-secondary px-1 shrink-0 gap-0.5">
          {tabs.map((tab) => (
            <div
              key={tab.id}
              onClick={() => {
                setActiveTabId(tab.id);
                wsRef.current?.send(JSON.stringify({ type: "switch_tab", session_id: tab.sessionId }));
              }}
              className={`flex items-center gap-1 px-2 h-full text-[11px] cursor-pointer border-r border-border rounded-t ${
                activeTabId === tab.id
                  ? "bg-card text-foreground font-medium"
                  : "text-muted-foreground hover:text-foreground"
              }`}
            >
              {tab.label}
              {tabs.length > 1 && (
                <button
                  onClick={(e) => {
                    e.stopPropagation();
                    closeTab(tab.id);
                  }}
                  className="ml-0.5 text-muted-foreground hover:text-destructive text-[10px] leading-none rounded hover:bg-destructive/10 p-0.5 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
                  aria-label={t("common.close")}
                >
                  <XIcon size={9} />
                </button>
              )}
            </div>
          ))}
          <button
            onClick={addTab}
            className="text-muted-foreground hover:text-foreground hover:bg-muted rounded px-1.5 text-xs font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
            title={t("terminalPanel.newTab")}
            aria-label={t("terminalPanel.newTab")}
          >
            <Plus size={13} />
          </button>
          <div className="flex-1" />
          <button
            onClick={() => setShowHistory(!showHistory)}
            className={`text-muted-foreground hover:text-foreground hover:bg-muted rounded p-1 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background ${showHistory ? "bg-muted text-foreground" : ""}`}
            title={t("legacy.term_eb41")}
            aria-label={t("legacy.term_eb41")}
          >
            <History size={13} />
          </button>
          <button
            onClick={() => {
              termInstance.current?.clear();
            }}
            className="text-muted-foreground hover:text-foreground hover:bg-muted rounded p-1 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
            title={t("legacy.term_34b9")}
            aria-label={t("legacy.term_34b9")}
          >
            <Trash2 size={13} />
          </button>
          <button
            onClick={onToggle}
            className="text-muted-foreground hover:text-foreground hover:bg-muted rounded p-1 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
            title={t("terminalPanel.collapse")}
            aria-label={t("terminalPanel.collapse")}
          >
            <ChevronDown size={14} />
          </button>
        </div>

        {/* 命令历史弹窗 (relative定位在容器内) */}
        {showHistory && (
          <div className="relative z-dropdown" style={{ height: 0 }}>
            <div
              className="absolute bottom-0 right-2 mb-1 bg-card border border-border rounded-lg shadow-lg max-h-52 overflow-y-auto min-w-[260px]"
              // Phase 66: 历史弹窗键盘导航
              onKeyDown={(e) => {
                const hist = [...cmdHistoryRef.current].reverse();
                if (hist.length === 0) return;
                if (e.key === "ArrowDown") {
                  e.preventDefault();
                  setHistoryIdx((i) => (i + 1 < hist.length ? i + 1 : 0));
                } else if (e.key === "ArrowUp") {
                  e.preventDefault();
                  setHistoryIdx((i) => (i - 1 >= 0 ? i - 1 : hist.length - 1));
                } else if (e.key === "Enter") {
                  e.preventDefault();
                  const cmd = hist[historyIdx >= 0 ? historyIdx : 0];
                  if (cmd) {
                    wsRef.current?.send(JSON.stringify({ type: "input", data: cmd + "\n" }));
                    setShowHistory(false);
                    setHistoryIdx(-1);
                  }
                } else if (e.key === "Escape") {
                  e.preventDefault();
                  setShowHistory(false);
                  setHistoryIdx(-1);
                }
              }}
              tabIndex={0}
            >
              <div className="text-[10px] text-muted-foreground px-3 py-1.5 border-b border-border sticky top-0 bg-secondary">
                {t("legacy.term_eb41")} ({cmdHistoryRef.current.length})
              </div>
              {cmdHistoryRef.current.length === 0 ? (
                <div className="text-[10px] text-muted-foreground px-3 py-2">{t("terminalPanel.noHistory")}</div>
              ) : (
                <>
                  {/* eslint-disable react/no-array-index-key */}
                  {[...cmdHistoryRef.current].reverse().map((cmd, i) => (
                    <div
                      key={cmd.slice(0, 20) + ":" + i}
                      className={`px-3 py-1 text-[11px] text-foreground hover:bg-secondary cursor-pointer font-mono truncate ${
                        i === historyIdx ? "bg-secondary" : ""
                      }`}
                      onClick={() => {
                        wsRef.current?.send(JSON.stringify({ type: "input", data: cmd + "\n" }));
                        setShowHistory(false);
                        setHistoryIdx(-1);
                      }}
                      onMouseEnter={() => setHistoryIdx(i)}
                    >
                      {cmd}
                    </div>
                  ))}
                  {/* eslint-enable react/no-array-index-key */}
                </>
              )}
            </div>
          </div>
        )}

        {/* 终端主体 */}
        <div ref={termRef} className="flex-1 relative overflow-hidden">
          {/* Phase 66: 可视化查找栏 */}
          <TerminalFindBar
            visible={showFindBar}
            onClose={() => setShowFindBar(false)}
            searchAddon={searchAddonRef.current}
            t={t}
          />
        </div>
      </div>
    );
  },
);
