import { useEffect, useState, useRef, useCallback } from "react";
import { useApi } from "../hooks/useApi";
import { useAppStore } from "../store/appStore";
import { useTaskStore, type SessionSummary } from "../store/taskStore";
import { switchSessionFromApi } from "../utils/sessionRestore";
import { useTranslation } from "react-i18next";
import { Plus, ChevronLeft, Pencil } from "lucide-react";
import { emitGlobalToast } from "./common/toast-context";
import { Button } from "./ui/button";
import { Skeleton } from "./ui/skeleton";
import { EmptyState } from "./common/EmptyState";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "./ui/dialog";

/** token 数紧凑显示：1234 → 1.2k，1234567 → 1.2M */
function fmtTokens(n: number | undefined): string {
  const v = n ?? 0;
  if (v < 1000) return String(v);
  if (v < 1_000_000) return `${(v / 1000).toFixed(1)}k`;
  return `${(v / 1_000_000).toFixed(1)}M`;
}

/** Phase 17: 会话列表侧栏（含右键删除） */
export function SessionList() {
  const { t } = useTranslation();

  function relativeTime(ts: number): string {
    const diff = Math.floor(Date.now() / 1000 - ts);
    if (diff < 60) return t("time.justNow");
    if (diff < 3600) return t("time.minutesAgo", { n: Math.floor(diff / 60) });
    if (diff < 86400) return t("time.hoursAgo", { n: Math.floor(diff / 3600) });
    return t("time.daysAgo", { n: Math.floor(diff / 86400) });
  }
  const { api } = useApi();
  const backendReady = useAppStore((s) => s.backendReady);
  const sessions = useTaskStore((s) => s.sessions);
  const setSessions = useTaskStore((s) => s.setSessions);
  const currentSessionId = useTaskStore((s) => s.currentSessionId);
  const setCurrentSession = useTaskStore((s) => s.setCurrentSession);
  const clearTasks = useTaskStore((s) => s.clearTasks);
  const addSession = useTaskStore((s) => s.addSession);
  const bumpSessionList = useTaskStore((s) => s.bumpSessionList);
  const sessionListVersion = useTaskStore((s) => s.sessionListVersion);
  const [loading, setLoading] = useState(false);
  // 存 i18n key 而不是翻译后的文案。若存文案，切换语言后这条提示会一直停留在旧语言，
  // 直到下次请求失败才更新；存 key 则随渲染重新翻译，语言一变就跟着变。
  // 附带好处：loadSessions 不再依赖 t——t 只属于渲染期，不该把数据请求和语言绑在一起
  // （否则切一次语言就要重新拉一次会话列表）。
  const [errorKey, setErrorKey] = useState<string | null>(null);
  // 右键菜单
  const [contextMenu, setContextMenu] = useState<{ x: number; y: number; sessionId: string } | null>(null);
  // P2: 全对话搜索
  const [searchQ, setSearchQ] = useState("");
  // 跨会话统计（近 30 天）：会话数 / 任务数 / token 用量
  const [stats, setStats] = useState<{
    session_count: number;
    task_count: number;
    total_tokens: number;
  } | null>(null);
  const [searchResults, setSearchResults] = useState<
    Array<{ session_id: string; preview: string; role: string; created_at: number }>
  >([]);
  const [searching, setSearching] = useState(false);
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [renameText, setRenameText] = useState("");
  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null);
  const renameRef = useRef<HTMLInputElement>(null);
  const startRename = (s: SessionSummary) => {
    setRenamingId(s.session_id);
    setRenameText(s.title || "");
    setTimeout(() => renameRef.current?.select(), 50);
  };
  const submitRename = async (sid: string) => {
    const nextTitle = renameText.trim();
    if (nextTitle && renamingId) {
      try {
        await api(`/api/v1/sessions/${sid}`, { method: "PATCH", body: JSON.stringify({ title: nextTitle }) });
        bumpSessionList();
      } catch {
        emitGlobalToast(t("sessionList.renameFailed"), "error");
      }
    }
    setRenamingId(null);
  };
  // Phase 17: 侧栏折叠
  const collapsed = useAppStore((s) => s.sessionSidebarCollapsed);
  const toggleCollapse = useAppStore((s) => s.toggleSessionSidebar);

  const loadSessions = useCallback(async () => {
    setLoading(true);
    setErrorKey(null);
    try {
      const list = await api<SessionSummary[]>("/api/v1/sessions");
      setSessions(list);
    } catch {
      // 只记 key，翻译交给渲染期（见 errorKey 声明处的说明）
      setErrorKey("sessionList.loadFailed");
    }
    setLoading(false);
  }, [api, setSessions]);

  // 跨会话统计：随会话列表一起刷新（失败就不显示，不影响列表）
  useEffect(() => {
    if (!backendReady) return;
    let cancelled = false;
    api<{ session_count: number; task_count: number; total_tokens: number }>(
      "/api/v1/sessions/stats?days=30",
    )
      .then((s) => {
        if (!cancelled) {
          setStats({
            session_count: s.session_count ?? 0,
            task_count: s.task_count ?? 0,
            total_tokens: s.total_tokens ?? 0,
          });
        }
      })
      .catch(() => {
        if (!cancelled) setStats(null);
      });
    return () => {
      cancelled = true;
    };
  }, [api, backendReady, sessionListVersion]);

  useEffect(() => {
    if (!backendReady) return;
    loadSessions();
  }, [backendReady, sessionListVersion, loadSessions]);

  const newSession = async () => {
    try {
      const res = await api<{ session_id: string }>("/api/v1/sessions/new", { method: "POST" });
      setCurrentSession(res.session_id);
      clearTasks();
      // 手动插入空会话到列表（新会话无 conversations 记录，SQL 查不出）
      addSession({
        session_id: res.session_id,
        msg_count: 0,
        last_at: Math.floor(Date.now() / 1000),
        agents: [],
        title: t("sessionList.newSession"),
        preview: "",
      });
    } catch {
      setErrorKey("sessionList.createFailed");
    }
  };

  // Batch 4a F1: 竞态防护的切换——快速连点时慢的旧响应被丢弃，不覆盖新会话
  const switchSession = async (sessionId: string) => {
    try {
      await switchSessionFromApi(sessionId, api);
    } catch {
      emitGlobalToast(t("sessionList.switchFailed"), "error");
    }
  };

  // 右键删除
  const handleContextMenu = (e: React.MouseEvent, sessionId: string) => {
    e.preventDefault();
    setContextMenu({ x: e.clientX, y: e.clientY, sessionId });
  };

  const confirmDeleteSession = (sessionId: string) => {
    setConfirmDeleteId(sessionId);
    setContextMenu(null);
  };

  const deleteSession = async (sessionId: string) => {
    try {
      await api(`/api/v1/sessions/${sessionId}`, { method: "DELETE" });
      if (currentSessionId === sessionId) {
        clearTasks();
        setCurrentSession(null);
      }
      bumpSessionList();
    } catch {
      emitGlobalToast(t("sessionList.deleteFailed"), "error");
    }
    setConfirmDeleteId(null);
  };

  // 点击其他地方关闭右键菜单
  useEffect(() => {
    if (!contextMenu) return;
    const h = () => setContextMenu(null);
    document.addEventListener("click", h);
    return () => document.removeEventListener("click", h);
  }, [contextMenu]);

  // 折叠态：只显示展开按钮
  if (collapsed) {
    return (
      <div className="h-full flex flex-col bg-background border-r w-10 shrink-0">
        <button
          onClick={toggleCollapse}
          className="w-full py-3 text-xs text-muted-foreground hover:bg-secondary transition-colors"
          title={t("legacy.sess_6c87")}
          aria-label={t("legacy.sess_6c87")}
        ></button>
      </div>
    );
  }

  return (
    <div className="h-full flex flex-col bg-background border-r">
      <div className="px-3 py-2 border-b shrink-0 space-y-1.5">
        <div className="flex items-center gap-1">
          <Button
            onClick={newSession}
            variant="default"
            size="sm"
            className="flex-1"
          >
            <Plus size={14} className="mr-1 shrink-0" />
            {t("chat.newChat")}
          </Button>
          <button
            onClick={toggleCollapse}
            className="text-muted-foreground hover:text-foreground text-xs px-1"
            title={t("legacy.sess_ae89")}
            aria-label={t("legacy.sess_ae89")}
          >
            <ChevronLeft size={12} />
          </button>
        </div>
        {stats && (
          <div
            className="flex items-center gap-2 text-[10px] text-muted-foreground"
            title={t("sessionList.statsHint")}
          >
            <span>{t("sessionList.statsLine", {
              sessions: stats.session_count,
              tasks: stats.task_count,
              tokens: fmtTokens(stats.total_tokens),
            })}</span>
          </div>
        )}
        {/* P2: 全对话搜索 */}
        <form
          onSubmit={async (e) => {
            e.preventDefault();
            if (!searchQ.trim()) {
              setSearchResults([]);
              return;
            }
            setSearching(true);
            try {
              const d = await api<{ results: typeof searchResults }>(
                `/api/v1/conversations/search?q=${encodeURIComponent(searchQ)}`,
              );
              setSearchResults(d.results || []);
            } catch {
              setSearchResults([]);
            }
            setSearching(false);
          }}
          className="flex gap-1"
        >
          <input
            value={searchQ}
            onChange={(e) => {
              setSearchQ(e.target.value);
              if (!e.target.value) setSearchResults([]);
            }}
            placeholder={t("sessionList.searchPlaceholder")}
            className="flex-1 px-2 py-1 text-[10px] border rounded bg-secondary"
          />
          <button
            type="submit"
            className="text-[10px] px-2 py-1 border rounded text-muted-foreground hover:bg-secondary"
          >
            {searching ? "…" : t("sessionList.search")}
          </button>
        </form>
      </div>

      {/* 搜索结果 */}
      {searchResults.length > 0 && (
        <div className="border-b max-h-40 overflow-y-auto">
          <div className="text-[9px] text-muted-foreground px-3 py-1 bg-muted sticky top-0">
            {t("sessionList.searchResults", { count: searchResults.length })}
          </div>
          {searchResults.map((r) => (
            <div
              key={r.session_id + ":" + r.created_at}
              className="px-3 py-1.5 text-[10px] hover:bg-secondary cursor-pointer border-b border-border"
              onClick={() => {
                switchSession(r.session_id);
                setSearchResults([]);
                setSearchQ("");
              }}
            >
              <span className="text-muted-foreground">{r.role === "user" ? t("sessionList.roleUser") : t("sessionList.roleAI")}</span>
              <span className="ml-1">{r.preview}</span>
            </div>
          ))}
        </div>
      )}

      <div className="flex-1 overflow-y-auto">
        {loading && (
          <div className="px-3 py-2 space-y-3" role="status" aria-label={t("legacy.git_26b5")}>
            {[0, 1, 2, 3].map((i) => (
              <div key={i} className="space-y-1.5">
                <Skeleton className="h-3 w-3/4" />
                <Skeleton className="h-2.5 w-1/2" />
              </div>
            ))}
          </div>
        )}
        {errorKey && (
          <div className="px-3 py-2">
            <p className="text-xs text-destructive">{t(errorKey)}</p>
            <Button onClick={loadSessions} variant="link" size="xs" className="mt-1">
              {t("legacy.planProg_132c")}
            </Button>
          </div>
        )}
        {!loading && !errorKey && sessions.length === 0 && (
          <EmptyState compact title={t("sessionList.empty")} />
        )}
        {sessions.map((s) => {
          const isActive = s.session_id === currentSessionId;
          return (
            <button
              key={s.session_id}
              onClick={() => switchSession(s.session_id)}
              onContextMenu={(e) => handleContextMenu(e, s.session_id)}
              className={`w-full text-left px-3 py-2 border-b border-border transition-colors group ${
                isActive
                  ? "bg-primary-muted border-l-2 border-l-accent"
                  : "hover:bg-secondary border-l-2 border-l-transparent"
              }`}
            >
              {renamingId === s.session_id ? (
                <input
                  ref={renameRef}
                  value={renameText}
                  onChange={(e) => setRenameText(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") submitRename(s.session_id);
                    else if (e.key === "Escape") setRenamingId(null);
                  }}
                  onBlur={() => submitRename(s.session_id)}
                  className="text-xs font-medium bg-background border rounded px-1.5 py-0.5 outline-none focus:border-primary w-full"
                  autoFocus
                />
              ) : (
                <div className="flex items-center gap-1">
                  <span
                    className="text-xs font-medium truncate text-foreground flex-1"
                    onDoubleClick={() => startRename(s)}
                  >
                    {s.title || t("sessionList.newSession")}
                  </span>
                  <span
                    role="button"
                    tabIndex={0}
                    aria-label={t("legacy.git_c8ce")}
                    onClick={(e) => {
                      e.stopPropagation();
                      startRename(s);
                    }}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" || e.key === " ") {
                        e.preventDefault();
                        e.stopPropagation();
                        startRename(s);
                      }
                    }}
                    className="opacity-0 group-hover:opacity-100 text-muted-foreground hover:text-foreground transition-opacity shrink-0 cursor-pointer"
                    title={t("legacy.git_c8ce")}
                  >
                    <Pencil size={12} />
                  </span>
                </div>
              )}
              <div className="flex items-center gap-2 mt-0.5 text-[10px] text-muted-foreground">
                <span>{t("sessionList.msgCount", { count: s.msg_count })}</span>
                {(s.total_tokens ?? 0) > 0 && (
                  <>
                    <span>·</span>
                    <span title={t("sessionList.tokenHint")}>
                      {t("sessionList.tokenUsage", { tokens: fmtTokens(s.total_tokens) })}
                    </span>
                  </>
                )}
                <span>·</span>
                <span>{relativeTime(s.last_at)}</span>
              </div>
              {s.preview && (
                <div className="text-[10px] text-muted-foreground truncate mt-0.5 opacity-70">{s.preview}</div>
              )}
            </button>
          );
        })}
      </div>

      {/* 右键菜单 */}
      {contextMenu && (
        <div
          className="fixed z-ctx bg-background border rounded shadow-lg py-1 min-w-[120px]"
          style={{ left: contextMenu.x, top: contextMenu.y }}
        >
          <button
            onClick={() => confirmDeleteSession(contextMenu.sessionId)}
            className="w-full text-left px-3 py-1.5 text-xs text-destructive hover:bg-destructive/10 transition-colors"
          >
            {t("sessionList.delete")}
          </button>
        </div>
      )}

      {/* 6.9: 删除会话二次确认 */}
      <Dialog open={!!confirmDeleteId} onOpenChange={(open) => !open && setConfirmDeleteId(null)}>
        <DialogContent showCloseButton={false}>
          <DialogHeader>
            <DialogTitle>{t("sessionList.confirmDeleteTitle")}</DialogTitle>
            <DialogDescription>
              {t("sessionList.confirmDeleteDesc", {
                title: sessions.find((s) => s.session_id === confirmDeleteId)?.title || t("sessionList.newSession"),
              })}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" size="sm" onClick={() => setConfirmDeleteId(null)}>
              {t("common.cancel")}
            </Button>
            <Button variant="destructive" size="sm" onClick={() => confirmDeleteId && deleteSession(confirmDeleteId)}>
              {t("sessionList.confirmDelete")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
