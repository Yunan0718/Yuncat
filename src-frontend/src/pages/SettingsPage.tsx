import { useEffect, useState, type ReactNode } from "react";
import { NavLink } from "react-router-dom";
import { useApi } from "../hooks/useApi";
import { useAppStore } from "../store/appStore";
import { ShortcutEditor } from "../components/settings/ShortcutEditor";
import { ModelsSection } from "../components/settings/ModelsSection";
import { AgentsSection } from "../components/settings/AgentsSection";
import { SecuritySection } from "../components/settings/SecuritySection";
import { WorkspaceSection } from "../components/settings/WorkspaceSection";
import { AppearanceSection } from "../components/settings/AppearanceSection";
import { AgentLoopSection } from "../components/settings/AgentLoopSection";
import { SystemSection } from "../components/settings/SystemSection";
import { GrantsSection } from "../components/settings/GrantsSection";
import { SchedulesSection } from "../components/settings/SchedulesSection";
import { LoopsSection } from "../components/settings/LoopsSection";
import { HarnessRecordingsSection } from "../components/settings/HarnessRecordingsSection";
import { PreferencesSection } from "../components/settings/PreferencesSection";
import { ErrorLogPanel } from "../components/ErrorLogPanel";
import { Tabs, TabsContent } from "../components/ui/tabs";
import { SECONDARY_NAV } from "../lib/navigation";

import { useTranslation } from "react-i18next";
import type { TFunction } from "i18next";
import { Brain, Bot, Shield, ShieldCheck, Folder, Palette, Keyboard, Cog, Settings2, FileWarning, Rocket, ChevronRight, Search, X, Heart, type LucideIcon, CalendarClock, Activity, Video } from "lucide-react";
import { LoadingCurve } from "../components/common/LoadingCurve";

/** api 实例类型——从 useApi 推导，避免与具体实现类型耦合 */
type SettingsApi = ReturnType<typeof useApi>["api"];

/** 分区渲染上下文——外壳注入的最小依赖集 */
interface SettingsTabContext {
  api: SettingsApi;
  t: TFunction;
}

/**
 * v67: 设置分区契约。
 *
 * 借鉴 DeepSeek harness 的设计意图——「a feature owns its own settings
 * pages; adding a setting never means editing the shell」：分区自己声明
 * 如何渲染，外壳只按注册表遍历。
 *
 * 这里刻意**不**引入 dsh 那套 slot/插件内核——Yuncat 是单体应用、所有分区
 * 编译期已知，运行时注册机制只会徒增间接层。只取「声明式自持」这一层。
 */
interface TabDef<Id extends string = string> {
  id: Id;
  labelKey: string;
  icon: LucideIcon;
  render: (ctx: SettingsTabContext) => ReactNode;
}

/**
 * 保留 id 字面量类型的分组定义助手。
 *
 * 直接 `as const` 会让每个分组的 tabs 被推断成各自独立的元组类型，
 * 平铺时 flatMap 退化为 unknown；改用泛型把 id 收拢为字面量联合。
 */
function defineTabGroups<Id extends string>(
  groups: ReadonlyArray<{ groupLabelKey: string; tabs: ReadonlyArray<TabDef<Id>> }>,
) {
  return groups;
}

// 分组导航——对标 VSCode 设置页 / macOS 系统设置
// 通用 / 外观 / 模型 / Agent / 安全与工具 / 诊断
const TAB_GROUPS = defineTabGroups([
  {
    groupLabelKey: "settings.groupGeneral",
    tabs: [
      { id: "workspace", labelKey: "settings.workspace", icon: Folder, render: ({ api }) => <WorkspaceSection api={api} /> },
      { id: "shortcuts", labelKey: "settings.shortcuts", icon: Keyboard, render: () => <ShortcutEditor /> },
    ],
  },
  {
    groupLabelKey: "settings.groupAppearance",
    tabs: [
      { id: "appearance", labelKey: "settings.appearance", icon: Palette, render: () => <AppearanceSection /> },
    ],
  },
  {
    groupLabelKey: "settings.groupModels",
    tabs: [{ id: "models", labelKey: "settings.models", icon: Brain, render: () => <ModelsSection /> }],
  },
  {
    groupLabelKey: "settings.groupAgent",
    tabs: [
      { id: "agents", labelKey: "nav.agents", icon: Bot, render: ({ api }) => <AgentsSection api={api} /> },
      { id: "agent-loop", labelKey: "settings.agentLoop", icon: Settings2, render: () => <AgentLoopSection /> },
      { id: "schedules", labelKey: "settings.schedules", icon: CalendarClock, render: () => <SchedulesSection /> },
    ],
  },
  {
    groupLabelKey: "settings.groupSecurityTools",
    tabs: [
      { id: "security", labelKey: "settings.security", icon: Shield, render: ({ api }) => <SecuritySection api={api} /> },
      { id: "grants", labelKey: "settings.grants", icon: ShieldCheck, render: () => <GrantsSection /> },
      { id: "preferences", labelKey: "settings.preferences", icon: Heart, render: () => <PreferencesSection /> },
      { id: "system", labelKey: "settings.system", icon: Cog, render: () => <SystemSection /> },
    ],
  },
  {
    groupLabelKey: "settings.groupDiagnostics",
    tabs: [
      { id: "loops", labelKey: "settings.loops", icon: Activity, render: () => <LoopsSection /> },
      { id: "harness", labelKey: "settings.harness", icon: Video, render: () => <HarnessRecordingsSection /> },
      {
        id: "errors",
        labelKey: "settings.errors",
        icon: FileWarning,
        render: ({ t }) => (
          <div className="space-y-4">
            <div>
              <h2 className="text-xl font-semibold tracking-tight">{t("errorLog.title")}</h2>
              <p className="text-sm text-muted-foreground mt-1">{t("errorLog.description")}</p>
            </div>
            <ErrorLogPanel />
          </div>
        ),
      },
    ],
  },
  {
    groupLabelKey: "settings.groupAdvanced",
    tabs: [
      {
        id: "advanced",
        labelKey: "settings.advanced",
        icon: Rocket,
        render: ({ t }) => (
          <div className="space-y-4">
            <div>
              <h2 className="text-xl font-semibold tracking-tight">{t("settings.advanced")}</h2>
              <p className="text-sm text-muted-foreground mt-1">{t("settings.advancedDesc")}</p>
            </div>
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
              {ADVANCED_ENTRIES.map((entry) => (
                <NavLink
                  key={entry.to}
                  to={entry.to}
                  className="flex items-center justify-between gap-2 rounded-lg bg-secondary hover:bg-secondary/70 transition-colors p-4"
                >
                  <span className="text-sm font-medium">{t(entry.labelKey)}</span>
                  <ChevronRight size={16} className="text-muted-foreground shrink-0" />
                </NavLink>
              ))}
            </div>
          </div>
        ),
      },
    ],
  },
]);

/**
 * TabValue 从注册表推导，而非手工维护的联合类型——
 * 新增分区只需改 TAB_GROUPS 一处，类型自动跟上，杜绝「加了 tab 忘了补类型」。
 */
type TabValue = (typeof TAB_GROUPS)[number]["tabs"][number]["id"];

/** 扁平化的分区清单——外壳渲染与搜索过滤共用，避免两处各写一遍 flatMap */
const ALL_TABS = TAB_GROUPS.flatMap((g) => g.tabs);

// 高级功能入口——与侧栏「更多」、⌘K 命令面板统一来自 lib/navigation，杜绝孤儿路由
const ADVANCED_ENTRIES = SECONDARY_NAV.map((r) => ({ to: r.path, labelKey: r.labelKey }));

export function SettingsPage() {
  const { t } = useTranslation();
  const { api } = useApi();
  const backendReady = useAppStore((s) => s.backendReady);
  const [tab, setTab] = useState<TabValue>("models");

  // 设置内搜索——按 tab 标签过滤左侧导航（对标 VS Code / macOS 系统设置）
  const [settingsQuery, setSettingsQuery] = useState("");
  const q = settingsQuery.trim().toLowerCase();
  const filteredGroups = q
    ? TAB_GROUPS.map((g) => ({
        ...g,
        tabs: g.tabs.filter((it) => t(it.labelKey).toLowerCase().includes(q)),
      })).filter((g) => g.tabs.length > 0)
    : TAB_GROUPS;

  // 当前选中项被过滤掉时，自动切到首个匹配项，避免内容区停留在不可见的 tab
  useEffect(() => {
    if (!q) return;
    const visible = ALL_TABS.filter((it) => t(it.labelKey).toLowerCase().includes(q)).map((it) => it.id);
    if (visible.length > 0 && !visible.includes(tab)) setTab(visible[0]);
  }, [q, t, tab]);

  if (!backendReady) {
    return (
      <div className="flex flex-col items-center justify-center h-64 gap-3 text-muted-foreground">
        <LoadingCurve variant="lissajous" size={40} particleCount={10} />
        <span className="text-sm">{t("common.back")}</span>
      </div>
    );
  }

  return (
    <div className="h-full flex">
      {/* ── Vertical tab nav (macOS Settings style) ── */}
      <div className="w-48 shrink-0 bg-muted/30 pt-6 pb-4 px-3 flex flex-col gap-1 overflow-y-auto">
        <h2 className="text-xs font-semibold text-muted-foreground px-3 mb-2">{t("settings.title")}</h2>
        <div className="relative mb-1">
          <Search size={13} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-muted-foreground pointer-events-none" />
          <input
            value={settingsQuery}
            onChange={(e) => setSettingsQuery(e.target.value)}
            placeholder={t("settings.searchPlaceholder")}
            aria-label={t("settings.searchPlaceholder")}
            className="w-full pl-7 pr-7 py-1.5 text-xs rounded-md border border-border bg-background text-foreground outline-none focus-visible:ring-1 focus-visible:ring-primary"
          />
          {settingsQuery && (
            <button
              type="button"
              onClick={() => setSettingsQuery("")}
              aria-label={t("common.close")}
              className="absolute right-2 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
            >
              <X size={13} />
            </button>
          )}
        </div>
        {filteredGroups.length === 0 ? (
          <div className="px-3 py-6 text-xs text-muted-foreground text-center">{t("settings.noMatch")}</div>
        ) : (
          filteredGroups.map((group) => (
            <div key={group.groupLabelKey} className="flex flex-col gap-0.5">
              <span className="text-[11px] font-medium text-muted-foreground/70 px-3 pt-3 pb-1">
                {t(group.groupLabelKey)}
              </span>
              {group.tabs.map((it) => {
                const Icon = it.icon;
                const active = tab === it.id;
                return (
                  <button
                    key={it.id}
                    onClick={() => setTab(it.id)}
                    className={`flex items-center gap-2.5 px-3 py-2 rounded-lg text-sm transition-colors text-left w-full ${
                      active
                        ? "bg-primary text-primary-foreground font-medium shadow-sm"
                        : "text-muted-foreground hover:bg-secondary hover:text-foreground"
                    }`}
                  >
                    <Icon size={16} strokeWidth={1.8} />
                    {t(it.labelKey)}
                  </button>
                );
              })}
            </div>
          ))
        )}
      </div>

      {/* ── Content area ── */}
      <div className="flex-1 overflow-y-auto bg-background">
        <div className="page-shell">
          <Tabs value={tab} onValueChange={(v) => setTab(v as TabValue)}>
            {/* v67: 按注册表遍历——新增设置分区只需在 TAB_GROUPS 加一条，此处无需改动。
                Radix TabsContent 仅挂载选中项，未选中分区的组件体不会执行。 */}
            {ALL_TABS.map((it) => (
              <TabsContent key={it.id} value={it.id}>
                {it.render({ api, t })}
              </TabsContent>
            ))}
          </Tabs>
        </div>
      </div>
    </div>
  );
}
