/**
 * 工具视图注册表——借鉴 deepseek-harness (dsh) 的 keyed toolview 分发。
 *
 * dsh 用 slot 注册表按工具名派发专用卡片（`ReadRow`/`SearchRow`/`GenericToolCard`），
 * 避免渲染处堆 if/else 链。Yuncat 体量不需要那套插件内核，但「按名分发 + 通用兜底」
 * 的思路直接可用，故落成这张纯数据注册表：
 *
 * - `variant`：决定图标与配色语义（图标表只有 12 项，却能覆盖任意多工具名）。
 * - `summary(args)`：折叠态的一行摘要，让人不展开就知道「读了哪个文件 / 跑了什么命令」。
 *   返回 `null` 表示无摘要，渲染处不再显示（原始 args 仍可展开查看）。
 *
 * 新增工具 = 往 `TOOL_VIEWS` 加一行；不注册也能用（走 GENERIC 兜底）。
 * 全文件为纯函数，无 React 依赖，便于单测。
 */

export type ToolVariant =
  | "read"
  | "write"
  | "edit"
  | "delete"
  | "bash"
  | "search"
  | "dir"
  | "web"
  | "think"
  | "delegate"
  | "install"
  | "other";

export type ToolArgs = Record<string, unknown>;

export interface ToolViewDef {
  variant: ToolVariant;
  /** 折叠行摘要；null = 无摘要 */
  summary?: (args: ToolArgs, t?: TranslateFn) => string | null;
}

// ── 取值助手 ────────────────────────────────────────────────

/** 取非空字符串参数 */
function str(v: unknown): string | null {
  return typeof v === "string" && v.trim() ? v.trim() : null;
}

/** 取有限数值参数 */
function num(v: unknown): number | null {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

/** 压成单行（摘要只能占一行，命令/正文里常带换行） */
function oneLine(s: string): string {
  return s.replace(/\s+/g, " ").trim();
}

/** 取第一个非空参数值，按候选键顺序 */
function firstOf(args: ToolArgs, keys: string[]): string | null {
  for (const k of keys) {
    const v = str(args[k]);
    if (v) return v;
  }
  return null;
}

// ── 各类工具的摘要 ───────────────────────────────────────────

/** read_file: `src/app.tsx` 或 `src/app.tsx (L101-200)` */
function readSummary(args: ToolArgs): string | null {
  const path = str(args.path);
  if (!path) return null;
  const offset = num(args.offset);
  const limit = num(args.limit);
  if (offset && offset > 0) {
    return limit && limit > 0
      ? `${path} (L${offset}-${offset + limit})`
      : `${path} (L${offset}+)`;
  }
  return path;
}

/** write_file: `src/app.tsx` 或 `src/app.tsx (append)` */
function writeSummary(args: ToolArgs, t?: TranslateFn): string | null {
  const path = firstOf(args, ["path", "file_path"]);
  if (!path) return null;
  const mode = str(args.mode);
  const lines = str(args.content)?.split("\n").length;
  // 单位文案由调用方传入翻译函数（本模块是纯数据/helper，**不引入 i18n 依赖**——
  // 直接 import i18n 单例会让所有"部分 mock react-i18next"的测试在加载期就崩）。
  // 未传 t 时退化为不带单位，宁可少一点信息，也不要写死某一种语言。
  const linesLabel = lines ? t?.("toolViews.lines", { count: lines }) ?? String(lines) : "";
  const tail = mode && mode !== "overwrite" ? ` (${mode})` : lines ? ` (${linesLabel})` : "";
  return path + tail;
}

/** search_files: `/useState/ in src/` */
function searchSummary(args: ToolArgs): string | null {
  const pattern = str(args.pattern);
  if (!pattern) return null;
  const path = str(args.path);
  return path && path !== "." ? `/${pattern}/ in ${path}` : `/${pattern}/`;
}

/** rename_file: `a.ts → b.ts` */
function renameSummary(args: ToolArgs): string | null {
  const oldPath = str(args.old_path);
  const newPath = str(args.new_path);
  if (oldPath && newPath) return `${oldPath} → ${newPath}`;
  return oldPath ?? newPath;
}

/** docs_lookup: `fastapi — lifespan async` */
function docsSummary(args: ToolArgs): string | null {
  const library = str(args.library);
  const query = str(args.query);
  if (library && query) return `${library} — ${query}`;
  return library ?? query;
}

/** delegate_to_agent: `hermes · run_command` */
function delegateSummary(args: ToolArgs): string | null {
  const target = str(args.target_agent);
  const tool = str(args.tool_name);
  if (target && tool) return `${target} · ${tool}`;
  return target ?? tool;
}

/** configure_mcp: `server_name ← source` */
function mcpSummary(args: ToolArgs): string | null {
  const name = str(args.server_name);
  const source = str(args.source);
  if (name && source) return `${name} ← ${source}`;
  return name ?? source;
}

/** install_*_from_url: `url` 或 `skill_name ← url` */
function installSummary(args: ToolArgs): string | null {
  const url = str(args.url);
  const name = firstOf(args, ["skill_name", "template_name"]);
  if (name && url) return `${name} ← ${url}`;
  return url ?? name;
}

/** 通用兜底：先试常见键，再退化到第一个字符串值。等价于 dsh 的 GenericToolCard。 */
function genericSummary(args: ToolArgs): string | null {
  const known = firstOf(args, [
    "path",
    "file_path",
    "command",
    "pattern",
    "query",
    "url",
    "name",
    "target_agent",
  ]);
  if (known) return known;
  for (const v of Object.values(args)) {
    const s = str(v);
    if (s) return s;
  }
  return null;
}

// ── 注册表 ──────────────────────────────────────────────────

/** 工具名 → 视图定义。未收录的工具走 GENERIC 兜底，不需要在此穷举。 */
const TOOL_VIEWS: Record<string, ToolViewDef> = {
  read_file: { variant: "read", summary: readSummary },
  reveal_in_explorer: { variant: "read", summary: (a) => str(a.path) },
  write_file: { variant: "write", summary: writeSummary },
  edit_file: { variant: "edit", summary: (a) => firstOf(a, ["path", "file_path"]) },
  rename_file: { variant: "edit", summary: renameSummary },
  delete_file: { variant: "delete", summary: (a) => str(a.path) },
  delete_dir: { variant: "delete", summary: (a) => str(a.path) },
  list_dir: { variant: "dir", summary: (a) => str(a.path) ?? "." },
  mkdir: { variant: "dir", summary: (a) => str(a.path) },
  run_command: { variant: "bash", summary: (a) => str(a.command) },
  search_files: { variant: "search", summary: searchSummary },
  codegraph_context: { variant: "search", summary: (a) => str(a.query) },
  docs_lookup: { variant: "web", summary: docsSummary },
  sequential_thinking: { variant: "think", summary: (a) => str(a.problem) },
  delegate_to_agent: { variant: "delegate", summary: delegateSummary },
  install_skill_from_url: { variant: "install", summary: installSummary },
  install_workflow_from_url: { variant: "install", summary: installSummary },
  configure_mcp: { variant: "install", summary: mcpSummary },
};

/** 兜底视图——未注册工具、MCP 外部工具、未知工具名都落到这里。 */
const GENERIC: ToolViewDef = { variant: "other", summary: genericSummary };

/**
 * 解析工具视图。未注册工具返回 GENERIC 兜底，永不返回 undefined。
 */
export function resolveToolView(name: string | undefined | null): ToolViewDef {
  if (!name) return GENERIC;
  return TOOL_VIEWS[name] ?? GENERIC;
}

/** 工具的视觉变体（决定图标/配色语义）。 */
export function toolVariant(name: string | undefined | null): ToolVariant {
  return resolveToolView(name).variant;
}

/**
 * 折叠行的一行摘要；无摘要返回 null。
 *
 * args 可能是字符串（部分后端直接透传 JSON 字符串）——此处一并兼容。
 */
/** 可选翻译函数（由调用方从 useTranslation 注入）——签名故意与 i18next 的 t 兼容 */
export type TranslateFn = (key: string, opts?: Record<string, unknown>) => string;

export function toolSummary(
  name: string | undefined | null, args: unknown, t?: TranslateFn,
): string | null {
  const parsed = parseArgs(args);
  if (!parsed) return null;
  const raw = resolveToolView(name).summary?.(parsed, t) ?? null;
  return raw ? oneLine(raw) : null;
}

/** 把可能是 JSON 字符串的 args 规范化为对象 */
function parseArgs(args: unknown): ToolArgs | null {
  if (!args) return null;
  if (typeof args === "string") {
    const trimmed = args.trim();
    if (!trimmed.startsWith("{")) return null;
    try {
      const parsed: unknown = JSON.parse(trimmed);
      return isPlainObject(parsed) ? parsed : null;
    } catch {
      return null;
    }
  }
  return isPlainObject(args) ? args : null;
}

function isPlainObject(v: unknown): v is ToolArgs {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}
