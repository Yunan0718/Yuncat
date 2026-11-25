import { useCallback, useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { isAbortedError, useApi } from "../hooks/useApi";
import { useToast } from "../hooks/use-toast";
import { useNavigate } from "react-router-dom";
import { CheckIcon, CrossIcon, PinIcon, WarningIcon } from "./common/Icons";
import { useTranslation } from "react-i18next";
import { useAppStore, type AppNotification } from "../store/appStore";
import { FocusTrap } from "./FocusTrap";
import { Button } from "./ui/button";

/** 通知中心铃铛：实时通知、历史通知、未读状态与显式批量操作。 */
const NOTIFICATION_PANEL_ID = "notification-center-panel";

function LevelIcon({ level }: { level: AppNotification["level"] }) {
  switch (level) {
    case "success":
      return <CheckIcon size={12} className="text-success" />;
    case "error":
      return <CrossIcon size={12} className="text-destructive" />;
    case "warning":
      return <WarningIcon size={12} className="text-warning" />;
    default:
      return <PinIcon size={12} className="text-muted-foreground" />;
  }
}

function notificationDate(timestamp: number): Date | null {
  const value = new Date(timestamp * 1000);
  return Number.isNaN(value.getTime()) ? null : value;
}

function formatNotificationTime(timestamp: number): string {
  return notificationDate(timestamp)?.toLocaleString() ?? "";
}

export function NotificationBell() {
  const { t } = useTranslation();
  const { api } = useApi();
  const { toast } = useToast();
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const notifications = useAppStore((state) => state.notifications);
  const markNotificationRead = useAppStore((state) => state.markNotificationRead);
  const removeNotification = useAppStore((state) => state.removeNotification);
  const clearReadNotifications = useAppStore((state) => state.clearReadNotifications);
  const unreadCount = notifications.filter((item) => !item.read).length;

  const loadNotifications = useCallback(async () => {
    setLoadError(null);
    try {
      const rows = await api<AppNotification[]>("/api/v1/notifications");
      const existing = new Set(useAppStore.getState().notifications.map((item) => item.id));
      for (const notification of rows) {
        if (!existing.has(notification.id)) useAppStore.getState().addNotification(notification);
      }
    } catch (error) {
      if (isAbortedError(error)) return;
      setLoadError(error instanceof Error ? error.message : String(error));
    }
  }, [api]);

  useEffect(() => {
    void loadNotifications();
  }, [loadNotifications]);

  const showActionError = (error: unknown) => {
    const detail = error instanceof Error ? error.message : String(error);
    toast(t("phase106.actionFailed", { error: detail }), "error");
  };

  const markAllRead = async () => {
    try {
      const ids = useAppStore.getState().notifications.filter((item) => !item.read).map((item) => item.id);
      await api("/api/v1/notifications/read-all", { method: "POST" });
      for (const id of ids) markNotificationRead(id);
    } catch (error) {
      showActionError(error);
    }
  };

  const markRead = async (id: string) => {
    try {
      await api(`/api/v1/notifications/${id}/read`, { method: "POST" });
      markNotificationRead(id);
    } catch (error) {
      showActionError(error);
    }
  };

  const dismiss = async (id: string) => {
    try {
      await api(`/api/v1/notifications/${id}`, { method: "DELETE" });
      removeNotification(id);
    } catch (error) {
      showActionError(error);
    }
  };

  const clearRead = async () => {
    try {
      await api("/api/v1/notifications", { method: "DELETE" });
      clearReadNotifications();
    } catch (error) {
      showActionError(error);
    }
  };

  const sorted = [...notifications].sort((left, right) => right.created_at - left.created_at);
  const overlayRoot = document.getElementById("overlay-root") ?? document.body;
  const bellLabel = unreadCount > 0
    ? t("phase106.unreadCount", { count: unreadCount })
    : t("legacy.notifBell_f831");

  return (
    <div className="relative">
      <Button
        onClick={() => setOpen((value) => !value)}
        variant="ghost"
        size="icon-sm"
        className="relative"
        title={bellLabel}
        aria-label={bellLabel}
        aria-expanded={open}
        aria-haspopup="dialog"
        aria-controls={open ? NOTIFICATION_PANEL_ID : undefined}
      >
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" aria-hidden="true">
          <path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9" />
          <path d="M13.73 21a2 2 0 0 1-3.46 0" />
        </svg>
        {unreadCount > 0 && (
          <span aria-hidden="true" className="absolute -top-1 -right-1 flex h-4 w-4 items-center justify-center rounded-full bg-destructive-solid text-[9px] font-bold text-destructive-foreground">
            {unreadCount > 9 ? "9+" : unreadCount}
          </span>
        )}
      </Button>

      {open && createPortal(
        <>
          <div className="fixed inset-0 z-backdrop" onClick={() => setOpen(false)} aria-hidden="true" />
          <FocusTrap active={open}>
            <div
              id={NOTIFICATION_PANEL_ID}
              role="dialog"
              aria-modal="true"
              aria-label={t("legacy.notifBell_f831")}
              onKeyDown={(event) => {
                if (event.key === "Escape") {
                  event.preventDefault();
                  setOpen(false);
                }
              }}
              className="fixed left-3 right-3 top-12 z-dropdown max-h-[min(24rem,calc(100vh-4rem))] overflow-y-auto rounded border bg-background shadow-sm animate-fade-in sm:left-auto sm:right-3 sm:w-80"
            >
              <div className="flex items-center gap-2 border-b px-3 py-2">
                <span className="text-xs font-semibold text-muted-foreground">{t("legacy.notifBell_f831")}</span>
                <div className="ml-auto flex items-center gap-2">
                  {unreadCount > 0 && (
                    <button type="button" onClick={() => void markAllRead()} className="text-[10px] text-muted-foreground hover:text-foreground">
                      {t("phase106.markAllRead")}
                    </button>
                  )}
                  {notifications.some((item) => item.read) && (
                    <button type="button" onClick={() => void clearRead()} className="text-[10px] text-muted-foreground hover:text-foreground">
                      {t("phase106.clearRead")}
                    </button>
                  )}
                </div>
              </div>
              {loadError && (
                <div role="alert" className="flex items-center gap-2 border-b bg-destructive/10 px-3 py-2 text-[11px] text-destructive">
                  <span className="min-w-0 flex-1">{t("phase106.loadFailed", { error: loadError })}</span>
                  <button type="button" className="shrink-0 font-medium hover:underline" onClick={() => void loadNotifications()}>
                    {t("phase106.retry")}
                  </button>
                </div>
              )}
              {sorted.length === 0 ? (
                <p className="px-3 py-4 text-center text-xs text-muted-foreground">{t("phase49.notification.empty")}</p>
              ) : (
                sorted.map((notification) => (
                  <div key={notification.id} className={`group relative border-b last:border-b-0 ${!notification.read ? "bg-primary-muted" : ""}`}>
                    <button
                      type="button"
                      onClick={() => {
                        void markRead(notification.id);
                        if (!notification.link) return;
                        setOpen(false);
                        navigate(notification.link);
                      }}
                      className="w-full px-3 py-2 pr-9 text-left text-xs hover:bg-secondary"
                    >
                      <span className="flex items-center gap-1.5">
                        <LevelIcon level={notification.level} />
                        <span className="truncate font-semibold">{notification.title}</span>
                        {!notification.read && <span className="ml-auto h-1.5 w-1.5 shrink-0 rounded-full bg-primary" aria-hidden="true" />}
                      </span>
                      {notification.body && <span className="mt-0.5 line-clamp-2 text-muted-foreground">{notification.body}</span>}
                      <time className="mt-1 block text-[10px] text-muted-foreground" dateTime={notificationDate(notification.created_at)?.toISOString()}>
                        {formatNotificationTime(notification.created_at)}
                      </time>
                    </button>
                    <button
                      type="button"
                      onClick={() => void dismiss(notification.id)}
                      aria-label={t("common.delete")}
                      title={t("common.delete")}
                      className="absolute right-2 top-2 inline-flex h-6 w-6 items-center justify-center rounded text-sm text-muted-foreground opacity-60 hover:bg-background hover:text-destructive focus-visible:opacity-100 sm:opacity-0 sm:group-hover:opacity-100"
                    >
                      ×
                    </button>
                  </div>
                ))
              )}
            </div>
          </FocusTrap>
        </>,
        overlayRoot,
      )}
    </div>
  );
}
