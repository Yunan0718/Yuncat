import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { useLocation } from "react-router-dom";
import { Menu, Bot, Cpu, Activity, Globe, Inbox } from "lucide-react";
import { Button } from "./ui/button";
import { NotificationBell } from "./NotificationBell";
import { ApprovalInboxDrawer } from "./ApprovalInboxDrawer";
import { LongTaskSupervisorDrawer } from "./LongTaskSupervisorDrawer";
import { useAppStore } from "../store/appStore";
import { useApi } from "../hooks/useApi";
import { useToast } from "../hooks/use-toast";
import { ROUTE_TITLE_KEYS } from "../lib/navigation";

interface TitleBarProps {
  onOpenMobileNav?: () => void;
}

/**
 * Phase D: 顶部标题栏 —— 对齐 Claude Code / Cursor 的极简标题栏
 *
 * - 左侧：当前页面标题 / 面包屑
 * - 中部：可拖拽区域（Electron 无框窗口）
 * - 右侧：Agent 状态、当前模型、通知铃铛
 */
export function TitleBar({ onOpenMobileNav }: TitleBarProps) {
  const { t } = useTranslation();
  const location = useLocation();
  const backendReady = useAppStore((s) => s.backendReady);
  const agentStatus = useAppStore((s) => s.agentStatus);
  const inboxBadgeCount = useAppStore((s) => s.inboxBadgeCount);
  const toggleInbox = useAppStore((s) => s.toggleInbox);
  const { api } = useApi();
  const [currentModel, setCurrentModel] = useState<string>("");

  const pageTitle = ROUTE_TITLE_KEYS[location.pathname] || "app.title";
  const { toast } = useToast();

  /**
   * 内置浏览器：窗口由 Electron 主进程托管，AI 与用户操作的是同一批标签页。
   * 打不开时必须说话——按钮点了没反应会让人以为功能坏了。
   */
  const handleOpenBrowser = () => {
    const bridge = window.yuncat;
    if (!bridge?.openBrowser) {
      toast(t("browser.unsupported"), "error");
      return;
    }
    bridge
      .openBrowser()
      .then((res) => {
        if (res && res.ok === false) toast(t("browser.openFailed"), "error");
      })
      .catch(() => toast(t("browser.openFailed"), "error"));
  };

  useEffect(() => {
    if (!backendReady) return;
    api<{ provider: string; model: string; name?: string }>("/api/v1/models/active")
      .then((r) => setCurrentModel(r?.model || ""))
      .catch(() => setCurrentModel(""));
  }, [backendReady, api, location.pathname]);

  const readyAgents = Object.entries(agentStatus).filter(([_, ready]) => ready);

  return (
    <header
      className="h-10 px-3 flex items-center shrink-0 bg-background/95 backdrop-blur-sm app-drag gap-2"
      style={{ paddingRight: "var(--electron-controls-width, 138px)" }}
    >
      <Button
        variant="ghost"
        size="icon"
        className="md:hidden h-8 w-8 -ml-1 app-no-drag"
        onClick={onOpenMobileNav}
        aria-label={t("phase49.mobile.menu")}
        title={t("phase49.mobile.menu")}
      >
        <Menu size={17} strokeWidth={1.7} />
      </Button>

      <div className="flex items-center gap-2 app-no-drag">
        <span className="text-xs font-semibold text-foreground">{t(pageTitle)}</span>
      </div>

      <div className="flex-1" />

      <div className="flex items-center gap-1.5 app-no-drag">
        {readyAgents.length > 0 && (
          <div
            className="hidden sm:flex items-center gap-1.5 px-2 py-1 rounded-md bg-secondary/70 text-[11px] text-muted-foreground"
            title={readyAgents.map(([id]) => id).join(", ")}
          >
            <Bot size={12} className="text-success" />
            <span className="text-foreground">{readyAgents.length}</span>
            <span>{t("agents.running")}</span>
          </div>
        )}

        {currentModel && (
          <div
            className="hidden sm:flex items-center gap-1.5 px-2 py-1 rounded-md bg-secondary/70 text-[11px] text-muted-foreground max-w-[180px]"
            title={currentModel}
          >
            <Cpu size={12} />
            <span className="truncate text-foreground">{currentModel}</span>
          </div>
        )}

        {backendReady && (
          <div
            className="flex items-center gap-1 px-2 py-1 rounded-md bg-secondary/70 text-[11px] text-muted-foreground"
            title={t("statusBar.ready")}
          >
            <Activity size={12} className="text-success" />
            <span className="hidden sm:inline">{t("statusBar.ready")}</span>
          </div>
        )}

        <Button
          onClick={handleOpenBrowser}
          variant="ghost"
          size="icon-sm"
          className="app-no-drag"
          title={t("browser.open")}
          aria-label={t("browser.open")}
        >
          <Globe size={16} strokeWidth={1.6} />
        </Button>

        <Button
          onClick={toggleInbox}
          variant="ghost"
          size="icon-sm"
          className="relative app-no-drag"
          title={t("inbox.title")}
          aria-label={t("inbox.title")}
        >
          <Inbox size={16} strokeWidth={1.6} />
          {inboxBadgeCount > 0 && (
            <span className="absolute -top-1 -right-1 bg-warning-solid text-warning-foreground text-[9px] w-4 h-4 rounded-full flex items-center justify-center font-bold">
              {inboxBadgeCount > 9 ? "9+" : inboxBadgeCount}
            </span>
          )}
        </Button>

        <NotificationBell />
      </div>

      <ApprovalInboxDrawer />
      {/* Phase 82 驾驶舱：之前只有组件与状态，没地方挂它，点了没反应 */}
      <LongTaskSupervisorDrawer />
    </header>
  );
}
