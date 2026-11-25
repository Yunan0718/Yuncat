import {
  MessageSquare,
  FolderOpen,
  Bot,
  Workflow,
  Brain,
  LayoutDashboard,
  Swords,
  Store,
  FileText,
  Plug,
  Sparkles,
  type LucideIcon,
} from "lucide-react";

export interface NavRoute {
  path: string;
  /** i18n key under the `nav` namespace, e.g. "nav.chat" */
  labelKey: string;
  icon: LucideIcon;
  section: "primary" | "secondary";
  end?: boolean;
}

/**
 * Single source of truth for navigation.
 *
 * Previously the route table, a `validRoutes` whitelist, the Sidebar nav, the
 * Command Palette page list and Settings > Advanced each maintained their own
 * (drifting) copy of the page list — 6 feature pages were orphaned (only
 * reachable via Settings "advanced" or the ⌘K palette). Everything now derives
 * from here:
 *   - PRIMARY_NAV   → always-visible rail items
 *   - SECONDARY_NAV → power-user pages, surfaced via the rail "More" popover,
 *                     the Command Palette, and Settings > Advanced
 */
export const NAV_ROUTES: NavRoute[] = [
  { path: "/", labelKey: "nav.chat", icon: MessageSquare, section: "primary", end: true },
  { path: "/workspace", labelKey: "nav.workspace", icon: FolderOpen, section: "primary" },
  { path: "/agents", labelKey: "nav.agents", icon: Bot, section: "primary" },
  { path: "/workflows", labelKey: "nav.workflows", icon: Workflow, section: "primary" },
  { path: "/memories", labelKey: "nav.memories", icon: Brain, section: "primary" },
  { path: "/dashboard", labelKey: "nav.dashboard", icon: LayoutDashboard, section: "secondary" },
  { path: "/arena", labelKey: "nav.arena", icon: Swords, section: "secondary" },
  { path: "/skills", labelKey: "nav.skills", icon: Store, section: "secondary" },
  { path: "/prompts", labelKey: "nav.prompts", icon: FileText, section: "secondary" },
  { path: "/mcp", labelKey: "nav.mcp", icon: Plug, section: "secondary" },
  { path: "/evolution-pending", labelKey: "nav.evolutionPending", icon: Sparkles, section: "secondary" },
];

export const PRIMARY_NAV = NAV_ROUTES.filter((r) => r.section === "primary");
export const SECONDARY_NAV = NAV_ROUTES.filter((r) => r.section === "secondary");
export const ALL_NAV_PATHS = NAV_ROUTES.map((r) => r.path);

/** Used by the session-restore guard so it never drifts from the real routes. */
export function isKnownRoute(path: string): boolean {
  return ALL_NAV_PATHS.includes(path) || path === "/chat" || path === "/settings";
}

/**
 * 页面标题（document.title / 标题栏）的单一来源 —— path → i18n key。
 * 从 NAV_ROUTES 自动派生（新增导航页自动覆盖），再补非导航表内但可达的路由，
 * 并保留各页面历史上实际使用的 i18n key（与既有文案一致）。
 * 取代 App.tsx 与 TitleBar.tsx 中两份手工维护、且各自漏了 /arena、/evolution-pending 的副本。
 */
export const ROUTE_TITLE_KEYS: Record<string, string> = {
  ...Object.fromEntries(NAV_ROUTES.map((r) => [r.path, r.labelKey])),
  "/chat": "nav.chat",
  "/settings": "nav.settings",
  "/arena": "nav.arena",
  "/evolution-pending": "evolution.pending.title",
  "/prompts": "prompts.title",
  "/mcp": "mcp.title",
  "/skills": "skillMarket.title",
};
