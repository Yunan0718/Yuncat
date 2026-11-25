import { NavLink } from "react-router-dom";
import { useState, useEffect, useRef } from "react";
import { useTranslation } from "react-i18next";
import { useAppStore } from "../store/appStore";
import { useApi } from "../hooks/useApi";
import { useLanguage, LANGUAGES } from "../i18n";
import { PRIMARY_NAV, SECONDARY_NAV } from "../lib/navigation";
import { type LucideIcon } from "lucide-react";
import {
  Settings,
  Sun,
  Moon,
  Monitor,
  X,
  PanelLeftClose,
  PanelLeftOpen,
  MoreHorizontal,
} from "lucide-react";
import { FocusTrap } from "./FocusTrap";

interface NavItem {
  to: string;
  labelKey: string;
  icon: LucideIcon;
  end?: boolean;
  badge?: number;
}

// 左图标栏——对标 Cursor / ChatGPT 桌面版 / VS Code Activity Bar。
// 顶部：对话 / 工作区 / Agent / 工作流 / 记忆（PRIMARY_NAV，统一来自 lib/navigation）。
// 中部：「更多」弹出次级页面（SECONDARY_NAV：仪表盘 / 竞技场 / 技能 / 提示词 / MCP / 进化）。
// 底部：设置 / 主题 / 语言 / 运行中 agent 数 / 折叠开关。
function NavButton({ item, onClick, collapsed = false }: { item: NavItem; onClick?: () => void; collapsed?: boolean }) {
  const { t } = useTranslation();
  const Icon = item.icon;
  const label = t(item.labelKey);
  const widthCls = collapsed ? "w-9" : "w-14";
  const gapCls = collapsed ? "" : "gap-1";
  return (
    <NavLink
      to={item.to}
      end={item.end}
      onClick={onClick}
      title={collapsed ? label : undefined}
      aria-label={label}
      className={({ isActive }) =>
        `relative flex flex-col items-center justify-center py-2 ${gapCls} rounded-lg mx-auto ${widthCls} transition-all duration-150 ${
          isActive ? "text-foreground bg-secondary" : "text-muted-foreground hover:text-foreground hover:bg-secondary"
        }`
      }
    >
      <Icon size={18} strokeWidth={1.8} />
      {!collapsed && <span className="text-xs font-medium leading-none whitespace-nowrap">{label}</span>}
      {item.badge ? (
        <span className="absolute top-1 right-1 min-w-[14px] h-[14px] px-1 rounded-full bg-primary text-primary-foreground text-[9px] font-semibold flex items-center justify-center">
          {item.badge}
        </span>
      ) : null}
    </NavLink>
  );
}

interface SidebarProps {
  mobile?: boolean;
  onClose?: () => void;
}

export function Sidebar({ mobile, onClose }: SidebarProps) {
  const { t } = useTranslation();
  const theme = useAppStore((s) => s.theme);
  const setTheme = useAppStore((s) => s.setTheme);
  const backendReady = useAppStore((s) => s.backendReady);
  const agentStatus = useAppStore((s) => s.agentStatus);
  const setAgentStatus = useAppStore((s) => s.setAgentStatus);
  const navCollapsed = useAppStore((s) => s.navSidebarCollapsed);
  const toggleNavSidebar = useAppStore((s) => s.toggleNavSidebar);
  const setNavSidebarCollapsed = useAppStore((s) => s.setNavSidebarCollapsed);
  const { api } = useApi();
  const { current, setLanguage } = useLanguage();
  const [langOpen, setLangOpen] = useState(false);
  const [moreOpen, setMoreOpen] = useState(false);

  const collapsed = !mobile && navCollapsed;

  // 窄屏自动折叠——主流做法（VS Code / Slack）：窗口窄时 Activity Bar 自动收起。
  useEffect(() => {
    if (mobile) return;
    const onResize = () => {
      const narrow = window.innerWidth < 1024;
      const cur = useAppStore.getState().navSidebarCollapsed;
      if (narrow && !cur) setNavSidebarCollapsed(true);
    };
    onResize();
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, [mobile, setNavSidebarCollapsed]);

  // 轮询 agent 状态——用于「运行中 agent 数」徽标（与标题栏口径一致，不再写死 openclaw/hermes）。
  const apiRef = useRef(api);
  apiRef.current = api;
  useEffect(() => {
    if (!backendReady) return;
    const poll = () => {
      apiRef.current<Array<{ id: string; status: string }>>("/api/v1/agents")
        .then((list) => {
          if (Array.isArray(list)) list.forEach((a) => setAgentStatus(a.id, a.status === "running"));
        })
        .catch((e) => console.warn("Sidebar: agent status poll failed:", e));
    };
    poll();
    const timer = setInterval(poll, 30000);
    return () => clearInterval(timer);
  }, [backendReady, setAgentStatus]);

  const runningCount = Object.values(agentStatus).filter(Boolean).length;
  const agentsRoute = PRIMARY_NAV.find((r) => r.path === "/agents")!;
  const agentsItem: NavItem = {
    to: agentsRoute.path,
    labelKey: agentsRoute.labelKey,
    icon: agentsRoute.icon,
    end: agentsRoute.end,
    badge: runningCount > 0 ? runningCount : undefined,
  };

  const cycleTheme = () => {
    const next = theme === "light" ? "dark" : theme === "dark" ? "system" : "light";
    setTheme(next);
  };
  const ThemeIcon = theme === "light" ? Sun : theme === "dark" ? Moon : Monitor;
  const themeLabel = t(`theme.${theme}`);
  const langLabel = LANGUAGES.find((l) => l.code === current)?.label ?? current.toUpperCase();
  const widthBtn = collapsed ? "w-9" : "w-14";
  const gapBtn = collapsed ? "" : "gap-1";

  const moreItems = SECONDARY_NAV.map((r) => ({ ...r }));

  const content = (
    <>
      <div className="mb-2 mt-1">
        <div className="w-9 h-9 rounded-xl bg-muted flex items-center justify-center">
          <span className="text-foreground font-semibold text-sm tracking-tight">Y</span>
        </div>
      </div>

      <nav className="flex-1 flex flex-col gap-1 w-full px-1.5">
        {PRIMARY_NAV.filter((r) => r.path !== "/agents").map((r) => (
          <NavButton
            key={r.path}
            item={{ to: r.path, labelKey: r.labelKey, icon: r.icon, end: r.end }}
            onClick={mobile ? onClose : undefined}
            collapsed={collapsed}
          />
        ))}
        {/* Agents 项带「运行中数」徽标，替代原先写死的 OC/HA 两点状态 */}
        <NavButton item={agentsItem} onClick={mobile ? onClose : undefined} collapsed={collapsed} />

        {/* 次级页面入口——统一来自 lib/navigation，避免孤儿路由不可达 */}
        {moreItems.length > 0 && (
          <div className="relative">
            <button
              onClick={() => setMoreOpen(!moreOpen)}
              className={`flex flex-col items-center justify-center py-2 ${gapBtn} rounded-lg mx-auto ${widthBtn} text-muted-foreground hover:text-foreground hover:bg-secondary transition-all duration-150`}
              title={collapsed ? t("nav.morePages") : undefined}
              aria-label={t("nav.morePages")}
              aria-expanded={moreOpen}
            >
              <MoreHorizontal size={18} strokeWidth={1.8} />
              {!collapsed && <span className="text-xs font-medium leading-none whitespace-nowrap">{t("nav.morePages")}</span>}
            </button>
            {moreOpen && (
              <>
                <div className="fixed inset-0 z-backdrop" onClick={() => setMoreOpen(false)} />
                <FocusTrap active={moreOpen}>
                  <div className="absolute left-full bottom-0 ml-1.5 bg-popover border border-border rounded-lg shadow-md py-1 z-dropdown min-w-[160px]">
                    {moreItems.map((r) => {
                      const Icon = r.icon;
                      return (
                        <NavLink
                          key={r.path}
                          to={r.path}
                          onClick={() => {
                            setMoreOpen(false);
                            onClose?.();
                          }}
                          className="flex items-center gap-2.5 w-full text-left px-3 py-2 text-xs text-muted-foreground hover:bg-secondary hover:text-foreground transition-colors"
                        >
                          <Icon size={15} strokeWidth={1.8} />
                          {t(r.labelKey)}
                        </NavLink>
                      );
                    })}
                  </div>
                </FocusTrap>
              </>
            )}
          </div>
        )}
      </nav>

      <div className="flex flex-col gap-1 w-full px-1.5 mt-auto">
        {/* 设置 */}
        <NavLink
          to="/settings"
          title={collapsed ? t("nav.settings") : undefined}
          aria-label={t("nav.settings")}
          className={({ isActive }) =>
            `flex flex-col items-center justify-center py-2 ${gapBtn} rounded-lg mx-auto ${widthBtn} transition-all duration-150 ${
              isActive ? "text-foreground bg-secondary" : "text-muted-foreground hover:text-foreground hover:bg-secondary"
            }`
          }
        >
          <Settings size={18} strokeWidth={1.8} />
          {!collapsed && <span className="text-xs font-medium leading-none whitespace-nowrap">{t("nav.settings")}</span>}
        </NavLink>

        {/* 主题切换 */}
        <button
          onClick={cycleTheme}
          className={`flex flex-col items-center justify-center py-2 ${gapBtn} rounded-lg mx-auto ${widthBtn} text-muted-foreground hover:text-foreground hover:bg-secondary transition-all duration-150`}
          title={themeLabel}
          aria-label={themeLabel}
        >
          <ThemeIcon size={18} strokeWidth={1.8} />
          {!collapsed && <span className="text-xs font-medium leading-none whitespace-nowrap">{themeLabel}</span>}
        </button>

        {/* 语言切换 */}
        <div className="relative">
          <button
            onClick={() => setLangOpen(!langOpen)}
            className={`flex flex-col items-center justify-center py-2 ${gapBtn} rounded-lg mx-auto ${widthBtn} text-muted-foreground hover:text-foreground hover:bg-secondary transition-all duration-150`}
            aria-label={t("settings.language")}
            title={collapsed ? langLabel : undefined}
          >
            <span className="text-sm font-bold tracking-wider">{current.toUpperCase()}</span>
            {!collapsed && <span className="text-xs font-medium leading-none whitespace-nowrap">{langLabel}</span>}
          </button>
          {langOpen && (
            <>
              <div className="fixed inset-0 z-backdrop" onClick={() => setLangOpen(false)} />
              <FocusTrap active={langOpen}>
                <div className="absolute left-full bottom-0 ml-1.5 bg-popover border border-border rounded-lg shadow-md py-1 z-dropdown min-w-[100px]">
                  {LANGUAGES.map((l) => (
                    <button
                      key={l.code}
                      onClick={() => {
                        setLanguage(l.code);
                        setLangOpen(false);
                      }}
                      className={`w-full text-left px-3 py-1.5 text-xs transition-colors ${current === l.code ? "text-primary font-semibold bg-primary/5" : "text-muted-foreground hover:bg-secondary"}`}
                    >
                      {l.label}
                    </button>
                  ))}
                </div>
              </FocusTrap>
            </>
          )}
        </div>

        {/* 折叠开关——对标 VS Code Activity Bar 的折叠 chevron */}
        {!mobile && (
          <button
            onClick={toggleNavSidebar}
            className={`flex items-center justify-center py-2 rounded-lg mx-auto ${widthBtn} text-muted-foreground hover:text-foreground hover:bg-secondary transition-all duration-150`}
            title={collapsed ? t("nav.expand") : t("nav.collapse")}
            aria-label={collapsed ? t("nav.expand") : t("nav.collapse")}
            aria-expanded={!collapsed}
          >
            {collapsed ? <PanelLeftOpen size={18} strokeWidth={1.8} /> : <PanelLeftClose size={18} strokeWidth={1.8} />}
          </button>
        )}
        {mobile && (
          <button
            onClick={onClose}
            className="flex items-center justify-center gap-1.5 py-2 mt-1 mx-auto w-full text-xs text-muted-foreground hover:text-foreground hover:bg-secondary rounded-lg transition-colors"
            aria-label={t("phase49.mobile.close")}
            title={t("phase49.mobile.close")}
          >
            <X size={14} />
            {t("phase49.mobile.close")}
          </button>
        )}
      </div>
    </>
  );

  if (mobile) {
    return (
      <div
        className="h-full w-full flex flex-col items-center bg-background py-2 gap-1 select-none"
        style={{ paddingTop: "var(--electron-drag-height, 34px)" }}
      >
        {content}
      </div>
    );
  }

  // 折叠态 w-12（48px），展开态 w-[80px]——主流侧栏宽度
  const asideWidth = collapsed ? "w-12" : "w-[80px]";
  return (
    <aside
      className={`${asideWidth} shrink-0 flex flex-col items-center bg-sidebar py-2 gap-1 select-none transition-[width] duration-150`}
      style={{ paddingTop: "var(--electron-drag-height, 34px)" }}
    >
      {content}
    </aside>
  );
}
