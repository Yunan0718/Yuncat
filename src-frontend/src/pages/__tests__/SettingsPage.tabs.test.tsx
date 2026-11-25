/**
 * v67: 设置页分区注册表测试。
 *
 * 背景：原先每个设置分区都要在 TAB_GROUPS（导航）+ `<TabsContent>`（内容）
 * 两处分别硬编码，新增分区需改 4 处。改造为「分区自持 render」的注册表后，
 * 外壳只负责遍历，新增分区只需改 TAB_GROUPS 一处。
 *
 * 验证：
 * - 注册表中每个分区都在导航中渲染
 * - 切换 tab 时渲染的是对应分区（注册表与内容区一致，不会错位）
 * - 搜索过滤按标签生效
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { SettingsPage } from "../SettingsPage";

vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (k: string) => k }),
}));

vi.mock("../../hooks/useApi", () => ({
  useApi: () => ({ api: { mock: true } }),
}));

vi.mock("../../store/appStore", () => ({
  useAppStore: (sel: (s: unknown) => unknown) => sel({ backendReady: true }),
}));

vi.mock("react-router-dom", () => ({
  NavLink: ({ children, to }: { children: React.ReactNode; to: string }) => <a href={to}>{children}</a>,
}));

// 各分区替换为可识别的探针，便于断言「当前渲染的是哪个分区」。
// 注意：vi.mock 工厂会被提升到文件顶部，不能引用顶层变量，必须逐个内联。
vi.mock("../../components/settings/ModelsSection", () => ({
  ModelsSection: () => <div data-testid="sec-ModelsSection" />,
}));
vi.mock("../../components/settings/AgentsSection", () => ({
  AgentsSection: () => <div data-testid="sec-AgentsSection" />,
}));
vi.mock("../../components/settings/AgentLoopSection", () => ({
  AgentLoopSection: () => <div data-testid="sec-AgentLoopSection" />,
}));
vi.mock("../../components/settings/SecuritySection", () => ({
  SecuritySection: () => <div data-testid="sec-SecuritySection" />,
}));
vi.mock("../../components/settings/WorkspaceSection", () => ({
  WorkspaceSection: () => <div data-testid="sec-WorkspaceSection" />,
}));
vi.mock("../../components/settings/AppearanceSection", () => ({
  AppearanceSection: () => <div data-testid="sec-AppearanceSection" />,
}));
vi.mock("../../components/settings/SystemSection", () => ({
  SystemSection: () => <div data-testid="sec-SystemSection" />,
}));
vi.mock("../../components/settings/ShortcutEditor", () => ({
  ShortcutEditor: () => <div data-testid="sec-ShortcutEditor" />,
}));
vi.mock("../../components/ErrorLogPanel", () => ({
  ErrorLogPanel: () => <div data-testid="sec-ErrorLogPanel" />,
}));

/** 点击左侧导航中指定标签文案的按钮 */
function clickTab(labelKey: string) {
  const btn = screen.getAllByRole("button").find((b) => b.textContent === labelKey);
  expect(btn, `导航中未找到标签 ${labelKey}`).toBeTruthy();
  fireEvent.click(btn as HTMLElement);
}

describe("v67: 设置页分区注册表", () => {
  it("注册表中的分区全部出现在左侧导航", () => {
    render(<SettingsPage />);
    const labels = [
      "settings.workspace",
      "settings.shortcuts",
      "settings.appearance",
      "settings.models",
      "nav.agents",
      "settings.agentLoop",
      "settings.security",
      "settings.system",
      "settings.errors",
      "settings.advanced",
    ];
    for (const label of labels) {
      expect(screen.getAllByText(label).length, `缺少导航项 ${label}`).toBeGreaterThan(0);
    }
  });

  it("默认渲染 models 分区", () => {
    render(<SettingsPage />);
    expect(screen.getByTestId("sec-ModelsSection")).toBeTruthy();
  });

  it("切换 tab 时渲染注册表对应的分区——注册表与内容区不错位", () => {
    render(<SettingsPage />);

    clickTab("settings.security");
    expect(screen.getByTestId("sec-SecuritySection")).toBeTruthy();
    expect(screen.queryByTestId("sec-ModelsSection")).toBeNull();

    clickTab("settings.workspace");
    expect(screen.getByTestId("sec-WorkspaceSection")).toBeTruthy();
    expect(screen.queryByTestId("sec-SecuritySection")).toBeNull();

    clickTab("settings.errors");
    expect(screen.getByTestId("sec-ErrorLogPanel")).toBeTruthy();
  });

  it("搜索按标签过滤导航，未匹配项被隐藏", () => {
    render(<SettingsPage />);
    const input = screen.getByLabelText("settings.searchPlaceholder");
    fireEvent.change(input, { target: { value: "settings.models" } });

    // models 仍可见，workspace 已被过滤掉
    expect(screen.getAllByText("settings.models").length).toBeGreaterThan(0);
    expect(screen.queryByText("settings.workspace")).toBeNull();
  });

  it("搜索无匹配时显示空态", () => {
    render(<SettingsPage />);
    const input = screen.getByLabelText("settings.searchPlaceholder");
    fireEvent.change(input, { target: { value: "zzz-不存在的设置" } });
    expect(screen.getByText("settings.noMatch")).toBeTruthy();
  });
});
