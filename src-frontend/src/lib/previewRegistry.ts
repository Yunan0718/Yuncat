/**
 * lib/previewRegistry.ts —— 文件预览注册表（2026-09-16）
 *
 * 借鉴 deepseek-harness 的 `ui-sidebar-documentpreview/document/registry.ts`：
 * 预览不再是一个 `switch (ext)`，而是一张"后缀 → 渲染器"的注册表。带来三件事：
 *
 *   1. **按最长后缀匹配**：`archive.tar.gz` 能命中 `tar.gz` 这条规则，而不是被 `.gz` 抢走；
 *   2. **外部实现优先**：第三方（插件/扩展）注册的同后缀实现排在内置之前，内置随时可被覆盖；
 *   3. **能力声明随实现走**：是不是二进制、内容要文本还是字节、要不要跟随换行偏好，
 *      都由注册项自己说清楚，而不是散在调用方的 if 里。
 *
 * 现在仍然只有一个内置实现集合（就是把原来 workspaceTabs 里的映射搬过来），
 * 但 API 已经允许追加——后面把 MediaPreview / DocumentPreview 变成注册项时不用再改这里。
 */

/** 渲染器标识：调用方按这个 id 决定挂哪个组件 */
export type PreviewRendererId =
  | "image" | "pdf" | "audio" | "video" | "html"
  | "document" | "spreadsheet" | "slides";

export interface PreviewDefinition {
  /** 唯一实现名，也是渲染器 id */
  id: PreviewRendererId;
  /** 后缀（不带点），大小写不敏感；支持复合后缀，如 "tar.gz" */
  extensions: readonly string[];
  /**
   * extensions 中哪些是二进制。命中二进制的文件不再有"按纯文本再试一次"的兜底，
   * 也就不会出现"打开 docx 看到一屏乱码"。
   */
  binaryExtensions?: readonly string[];
  /** 外部实现优先于内置实现 */
  priority?: "builtin" | "extension";
  /** 内容投递方式：文本一页页给，还是一次给完字节 */
  loading: "text" | "bytes";
  /** 是否消费"自动换行"偏好（代码块用它，表格不用） */
  wrap?: boolean;
  /** 展示名（i18n key）——目前界面还没用上，留给外部实现的标题栏 */
  titleKey?: string;
}

/** 内置实现集合：与 routes/workspace.py 的 PREVIEW_MEDIA_TYPES 一一对应 */
const BUILTIN: PreviewDefinition[] = [
  {
    id: "image",
    extensions: ["png", "jpg", "jpeg", "gif", "webp", "bmp", "ico", "svg", "avif"],
    binaryExtensions: ["png", "jpg", "jpeg", "gif", "webp", "bmp", "ico", "avif"],
    loading: "bytes",
  },
  { id: "pdf", extensions: ["pdf"], binaryExtensions: ["pdf"], loading: "bytes" },
  {
    id: "audio",
    extensions: ["mp3", "wav", "ogg", "m4a", "flac", "aac"],
    binaryExtensions: ["mp3", "wav", "ogg", "m4a", "flac", "aac"],
    loading: "bytes",
  },
  {
    id: "video",
    extensions: ["mp4", "m4v", "webm", "mov", "ogv"],
    binaryExtensions: ["mp4", "m4v", "webm", "mov", "ogv"],
    loading: "bytes",
  },
  { id: "html", extensions: ["html", "htm"], loading: "text", wrap: true },
  { id: "document", extensions: ["docx"], binaryExtensions: ["docx"], loading: "bytes" },
  {
    id: "spreadsheet",
    extensions: ["xlsx", "xlsm"],
    binaryExtensions: ["xlsx", "xlsm"],
    loading: "bytes",
  },
  { id: "slides", extensions: ["pptx"], binaryExtensions: ["pptx"], loading: "bytes" },
];

/** 外部注册（第三方扩展 / 后续把自有渲染器搬进来时用） */
const external: PreviewDefinition[] = [];

export function registerPreviewDefinition(definition: PreviewDefinition): void {
  const index = external.findIndex((item) => item.id === definition.id);
  if (index >= 0) external[index] = definition;
  else external.push(definition);
}

/** 只给测试用：清掉外部注册，避免用例之间互相影响 */
export function clearExternalPreviewDefinitions(): void {
  external.length = 0;
}

function normalizeSuffix(extension: string): string {
  return extension.toLowerCase().replace(/^\./u, "");
}

/** 取文件名（兼容 Windows 反斜杠路径） */
export function documentFileName(path: string): string {
  const normalized = (path ?? "").replace(/\\/g, "/").toLowerCase();
  return normalized.slice(normalized.lastIndexOf("/") + 1);
}

/** 命中该文件名的注册项；外部优先，其次最长后缀，都没有则 null */
export function matchPreviewDefinition(path: string): PreviewDefinition | null {
  const name = documentFileName(path);
  if (!name) return null;
  const candidates = [...external, ...BUILTIN];
  let best: PreviewDefinition | null = null;
  let bestLength = 0;
  let bestExternal = false;
  for (const definition of candidates) {
    const isExternal = definition.priority === "extension" || external.includes(definition);
    for (const raw of definition.extensions) {
      const suffix = normalizeSuffix(raw);
      if (!suffix || !name.endsWith(`.${suffix}`)) continue;
      const longer = suffix.length > bestLength;
      const sameButExternal = suffix.length === bestLength && isExternal && !bestExternal;
      if (longer || sameButExternal) {
        best = definition;
        bestLength = suffix.length;
        bestExternal = isExternal;
      }
    }
  }
  return best;
}

/** 命中的后缀本身是二进制吗（用于"要不要再按文本读一遍"的判断） */
export function isBinaryPreview(path: string): boolean {
  const definition = matchPreviewDefinition(path);
  if (!definition) return false;
  const name = documentFileName(path);
  const binary = (definition.binaryExtensions ?? []).map(normalizeSuffix);
  return binary.some((suffix) => name.endsWith(`.${suffix}`));
}

export function listPreviewDefinitions(): PreviewDefinition[] {
  return [...external, ...BUILTIN];
}
