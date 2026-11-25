/**
 * 工作区标签页的持久化（2026-09-15）。
 *
 * 为什么要单独成模块：这条逻辑原本长在 WorkspacePage 里，而该页面已经接近
 * 「文件 <800 行」的硬线。这里只放纯函数（不依赖 React），便于单元测试与复用。
 *
 * 设计要点：
 *  - 跨路由保留「打开了哪些文件」以及**未保存的编辑**（切到对话再回来不该丢）；
 *  - 已保存的正文不落盘（回来时从磁盘重读），避免多个大文件撑爆 localStorage；
 *  - 落盘失败（配额/隐私模式）只影响「下次是否还在」，不阻塞当前编辑。
 */

import { matchPreviewDefinition, type PreviewRendererId } from "./previewRegistry";

export interface WorkspaceTab {
  key: number;
  path: string;
  name: string;
  content: string;
  originalContent: string;
  loading: boolean;
  /** 打开时的磁盘 mtime（保存做乐观并发校验） */
  mtime?: number;
  /** 二进制文件：只读打开，禁止编辑保存 */
  binary?: boolean;
  /** 媒体预览方式（图片/PDF/音频/视频）——非空时正文不读入编辑器，直接渲染预览 */
  preview?: PreviewKind;
}

interface PersistedTab {
  path: string;
  name: string;
  content: string;
  originalContent: string;
}

const OPEN_TABS_KEY = "yuncat-workspace-open-tabs";
/** 序列化预算（字符数）：超出后退化为「只记路径」，宁可下次重读磁盘也不要撑爆配额。 */
const TABS_PERSIST_MAX_CHARS = 1_500_000;

/** 读取上次会话的标签页；无数据/数据损坏返回 null。 */
export function loadPersistedTabs(
  nextKey: () => number,
): { tabs: WorkspaceTab[]; activeIdx: number } | null {
  try {
    const raw = localStorage.getItem(OPEN_TABS_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as { tabs?: PersistedTab[]; activeIdx?: number };
    const list = Array.isArray(parsed?.tabs) ? parsed.tabs : [];
    const tabs: WorkspaceTab[] = list
      .filter((tab) => tab && typeof tab.path === "string" && tab.path)
      .map((tab) => ({
        key: nextKey(),
        path: tab.path,
        name: typeof tab.name === "string" && tab.name ? tab.name : tab.path,
        content: typeof tab.content === "string" ? tab.content : "",
        originalContent: typeof tab.originalContent === "string" ? tab.originalContent : "",
        loading: false,
      }));
    if (!tabs.length) return null;
    const activeIdx = Math.min(Math.max(parsed.activeIdx ?? 0, 0), tabs.length - 1);
    return { tabs, activeIdx };
  } catch {
    return null;
  }
}

/** 落盘标签页：只有未保存的编辑才写正文，其余折叠成路径。 */
export function savePersistedTabs(tabs: WorkspaceTab[], activeIdx: number): void {
  const brief = (t: WorkspaceTab) => ({ path: t.path, name: t.name, content: "", originalContent: "" });
  try {
    const payload = JSON.stringify({
      activeIdx,
      tabs: tabs.map((tab) => {
        const unsaved = tab.content !== tab.originalContent && !tab.loading;
        return unsaved
          ? { path: tab.path, name: tab.name, content: tab.content, originalContent: tab.originalContent }
          : brief(tab);
      }),
    });
    if (payload.length > TABS_PERSIST_MAX_CHARS) {
      console.warn("[workspaceTabs] open tabs exceed persist budget — keeping paths only");
      localStorage.setItem(OPEN_TABS_KEY, JSON.stringify({ activeIdx, tabs: tabs.map(brief) }));
      return;
    }
    localStorage.setItem(OPEN_TABS_KEY, payload);
  } catch {
    // 配额/序列化失败只影响「下次进来还在不在」，不阻塞当前编辑
  }
}

/** 工作区接口接受的路径：绝对路径原样透传（后端用 validate_path 校验是否在工作区内）。 */
export function toWorkspacePath(path: string): string {
  return path.replace(/\\/g, "/");
}

/**
 * 转成「相对工作区根」的路径（FileTree 各处 API 统一用这个形态）。
 *
 * 2026-09-15 修复：此前各处按 "/shared/" 切分，导致
 *  - shared 根节点（路径以 /shared 结尾、没有尾斜杠）退回**整条绝对路径** →
 *    `dir=shared/C:/…` 后端 500 → 目录永远显示为空；
 *  - 文件丢掉 `shared/` 前缀 → 后端按工作区根解析 → 404 → 文件树里打不开任何文件。
 */
export function toWorkspaceRelative(path: string, workspaceRoot: string): string {
  const p = path.replace(/\\/g, "/");
  const root = workspaceRoot.replace(/\\/g, "/").replace(/\/$/, "");
  if (root) {
    if (p === root) return "";
    if (p.startsWith(root + "/")) return p.slice(root.length + 1);
  }
  // 兜底：拿不到工作区根（设置接口失败等）时，从 /shared|/agents 标记处推断，
  // 保证仍是「工作区相对」形态——退化成 shared 相对会让 dir/path 再次错位。
  const marker = p.match(/\/(shared|agents)(\/|$)/);
  return marker && marker.index !== undefined ? p.slice(marker.index + 1) : p;
}

/** 预览方式——与后端 routes/workspace.py 的 PREVIEW_MEDIA_TYPES 对应。 */

/** 浏览器原生就能渲染的媒体 */
export type MediaPreviewKind = "image" | "pdf" | "audio" | "video";

/** 需要解析后才能展示的文档：docx / xlsx / pptx（只读，见 DocumentPreview） */
export type DocumentPreviewKind = "document" | "spreadsheet" | "slides";

export type PreviewKind = MediaPreviewKind | DocumentPreviewKind;

/**
 * 取文件命中的预览渲染器。
 *
 * 2026-09-16 起改成查注册表（lib/previewRegistry）——后缀匹配、最长后缀、外部实现优先
 * 都在那一处实现；这里只剩"是不是文档类"这种调用方关心的判断。
 */
export function previewKind(nameOrPath: string): PreviewKind | null {
  const definition = matchPreviewDefinition(nameOrPath);
  if (!definition) return null;
  const id = definition.id as PreviewRendererId;
  if (id === "html") return null; // html 走文本通道 + 渲染切换，不算媒体预览
  return id as PreviewKind;
}

/** 收窄用：是文档类预览还是媒体类预览，决定交给哪个组件 */
export function isDocumentPreview(kind: PreviewKind): kind is DocumentPreviewKind {
  return kind === "document" || kind === "spreadsheet" || kind === "slides";
}

/** 表格视图（CSV/TSV）：内容仍是文本、仍可编辑，只是可以切成表格看 */
const TABLE_PREVIEW_EXTENSIONS = [".csv", ".tsv", ".tab"];

const TEXT_PREVIEW_EXTENSIONS = [".md", ".markdown", ".html", ".htm"];

/** 旧版二进制 Office（.doc/.xls/.ppt）无法在应用内解析，给一句比"二进制文件"更有用的提示 */
const LEGACY_OFFICE_EXTENSIONS = [".doc", ".xls", ".ppt"];

function extOf(nameOrPath: string): string {
  const lower = (nameOrPath ?? "").toLowerCase();
  const dot = lower.lastIndexOf(".");
  return dot >= 0 ? lower.slice(dot) : "";
}

export function supportsTablePreview(nameOrPath: string): boolean {
  return TABLE_PREVIEW_EXTENSIONS.includes(extOf(nameOrPath));
}

export function isLegacyOfficeFile(nameOrPath: string): boolean {
  return LEGACY_OFFICE_EXTENSIONS.includes(extOf(nameOrPath));
}

export function supportsRenderedPreview(nameOrPath: string): boolean {
  return TEXT_PREVIEW_EXTENSIONS.includes(extOf(nameOrPath));
}
