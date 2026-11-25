import { lazy, Suspense, useEffect, useId, useMemo, useRef, useState } from "react";
import DOMPurify from "dompurify";
import { marked } from "marked";
import hljs from "highlight.js";
// 高亮 token 配色统一由 src/index.css 的 .hljs 色板提供（浅/深双主题各一套），不在此硬编码 dark 主题
import {
  BarChart,
  Bar,
  LineChart,
  Line,
  PieChart,
  Pie,
  AreaChart,
  Area,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  Cell,
  Legend,
} from "recharts";
// Monaco DiffEditor 体积大，按需加载 —— 避免被 ResultRenderer 的静态导入拖入首屏 bundle
const InlineDiff = lazy(() => import("./workspace/InlineDiff").then((m) => ({ default: m.InlineDiff })));
import { useVirtualizer } from "@tanstack/react-virtual";
import { useAppStore } from "../store/appStore";
import { useThrottledValue } from "../hooks/useThrottledValue";
import { useTranslation } from "react-i18next";
import i18next from "i18next";
import { detectEntities, injectEntitySpans } from "./entity-renderer";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from "./ui/dialog";
import { Textarea } from "./ui/textarea";
import { Button } from "./ui/button";

// ---- marked 配置 ----
marked.setOptions({
  gfm: true,
  breaks: true,
});

// 代码块语法高亮
marked.use({
  renderer: {
    code({ text, lang }: { text: string; lang?: string }) {
      const language = hljs.getLanguage(lang || "") ? lang : undefined;
      const highlighted = language
        ? hljs.highlight(text, { language: language! }).value
        : hljs.highlightAuto(text).value;
      const langLabel = language || "text";
      return `<pre class="diff-block rounded-lg my-2 overflow-x-auto">
        <div class="flex items-center justify-between px-3 py-1.5 diff-block-header">
          <span class="text-[11px] diff-meta font-mono">${langLabel}</span>
          <div class="flex gap-2">
            <button class="text-[11px] diff-meta hover:text-foreground edit-btn" data-code="${encodeURIComponent(text)}" data-lang="${langLabel}">${i18next.t("resultRenderer.edit")}</button>
            <button class="text-[11px] diff-meta hover:text-foreground copy-btn" data-code="${encodeURIComponent(text)}">${i18next.t("resultRenderer.copy")}</button>
          </div>
        </div>
        <div class="px-4 py-3"><code class="text-[13px] leading-relaxed font-mono">${highlighted}</code></div>
      </pre>`;
    },
  },
});

interface ChartMetadata {
  type: "chart";
  chart_type: "bar" | "line" | "pie" | "area";
  data: Array<Record<string, unknown>>;
  title?: string;
  x_key?: string;
  y_key?: string;
}

const CHART_COLORS = [
  "var(--primary)",
  "var(--chart-2)",
  "var(--chart-3)",
  "var(--chart-4)",
  "var(--chart-5)",
  "var(--chart-6)",
];

// Task 6.11: 大内容阈值——超过则折叠 + 虚拟滚动，避免 O(n²) marked+hljs+DOMPurify 卡死浏览器
const LARGE_CONTENT_CHARS = 5000;
const LARGE_CODE_LINES = 200;
/** 流式期间富文本重解析的最小间隔（ms）——解析次数与时长成正比，而非 token 数 */
const STREAM_THROTTLE_MS = 120;
/** 流式期间超过此长度回退纯文本，防病理性长输出把主线程拖垮 */
const STREAM_RICH_MAX_CHARS = 50_000;
const VIRTUAL_CODE_ROW_HEIGHT = 20;
const VIRTUAL_CODE_MAX_HEIGHT = 400;

/** Phase 6: props now accept optional chunks array for metadata scanning */
interface ResultRendererProps {
  content?: string;
  chunks?: string[];
  resultType?: string;
  /**
   * 流式标记。旧实现（E4）在流式期间完全跳过 Markdown 走纯文本，代价是整个响应
   * 期间用户看到的都是源码级的 `**` 和裸代码块。现在改为节流富文本：解析次数与
   * 时长成正比而非 token 数，仅当超过 STREAM_RICH_MAX_CHARS 才回退纯文本。
   */
  isStreaming?: boolean;
}

// ---- helpers ----

function scanChunks(chunks: string[]): {
  textChunks: string[];
  chartMeta: ChartMetadata | null;
} {
  for (const chunk of chunks) {
    const trimmed = chunk.trim();
    if (trimmed.startsWith('{"type":"chart"')) {
      try {
        const meta = JSON.parse(trimmed);
        if (meta.chart_type && meta.data) {
          return {
            textChunks: chunks.filter((c) => c.trim() !== trimmed),
            chartMeta: meta as ChartMetadata,
          };
        }
      } catch {
        /* ignore */
      }
    }
  }
  return { textChunks: chunks, chartMeta: null };
}

function extractCodeBlocks(text: string): Array<{ language: string; code: string }> {
  const blocks: Array<{ language: string; code: string }> = [];
  const regex = /```(\w*)\n([\s\S]*?)```/g;
  let match;
  while ((match = regex.exec(text)) !== null) {
    blocks.push({ language: match[1] || "text", code: match[2].trimEnd() });
  }
  return blocks;
}

/** v66: 文件路径插件——检测反引号中的文件路径并渲染为可点击链接 */
const MARKED_FILE_LINK_EXT = {
  extensions: [
    {
      name: "file-link",
      level: "inline" as const,
      start(src: string) {
        return src.search(/`[^`]+\.(?:py|ts|tsx|js|jsx|json|yaml|yml|md|html|css|rs|go|java|sql|txt)`/);
      },
      tokenizer(src: string) {
        const match = src.match(/^`([^`]+\.(?:py|tsx?|jsx?|json|yaml|yml|md|html|css|rs|go|java|sql|txt))`/);
        if (match) {
          return { type: "file-link", raw: match[0], file: match[1], tokens: [] } as never;
        }
        return undefined;
      },
      renderer(token: { file: string }) {
        const tooltip = i18next.t("resultRenderer.openFileTooltip", "Open in Yuncat");
        return `<code class="cursor-pointer hover:underline text-primary" data-file="${token.file}" title="${tooltip}">${token.file}</code>`;
      },
    },
  ],
};
marked.use(MARKED_FILE_LINK_EXT);

/**
 * v67: CJK 友好的 `**加粗**`。
 * CommonMark 要求强调的闭合边界是空白或标点，而中文没有词间空格，导致
 * `**注意！**后面是中文`、`**注：**说明` 这类极常见写法退化为字面星号。
 * marked 的内置 strong 是行内规则，无法通过 marked.use({tokenizer}) 覆盖，
 * 因此改用行内扩展——扩展规则优先于内置规则执行，且仅在闭合 `**` 后紧跟
 * CJK 字符时接管，其余场景一律交回内置规则，保持原有行为不变。
 */
const CJK_CHAR =
  /[\u3000-\u303F\u3400-\u4DBF\u4E00-\u9FFF\uF900-\uFAFF\u3040-\u309F\u30A0-\u30FF\uAC00-\uD7AF\uFF00-\uFFEF]/;

const MARKED_CJK_STRONG_EXT = {
  extensions: [
    {
      name: "cjk-strong",
      level: "inline" as const,
      start(src: string) {
        return src.indexOf("**");
      },
      tokenizer(src: string) {
        const match = /^\*\*([\s\S]+?)\*\*/.exec(src);
        if (!match) return undefined;
        const next = src[match[0].length];
        if (!next || !CJK_CHAR.test(next)) return undefined; // 仅接管紧跟 CJK 的场景
        const inner = match[1];
        const lexer = (this as unknown as { lexer?: { inlineTokens?: (s: string) => never[] } }).lexer;
        const tokens = lexer?.inlineTokens
          ? lexer.inlineTokens(inner)
          : ([{ type: "text", raw: inner, text: inner }] as never);
        return { type: "cjk-strong", raw: match[0], text: inner, tokens } as never;
      },
      renderer(token: { tokens?: never[] }) {
        const parser = (this as unknown as { parser: { parseInline: (t: never[]) => string } }).parser;
        return `<strong>${parser.parseInline(token.tokens ?? [])}</strong>`;
      },
    },
  ],
};
marked.use(MARKED_CJK_STRONG_EXT);

/** v66: 用 marked 渲染 Markdown → 实体高亮 → DOMPurify 清理 → 安全 HTML */
function computeMarkdown(text: string, t: (k: string, opts?: Record<string, unknown>) => string): string {
  // M10 修复：运行时类型守卫替代 as string 断言
  const parsed = marked.parse(text);
  const raw = wrapTables(typeof parsed === "string" ? parsed : "");
  const entities = detectEntities(text);
  const withEntities = entities.length > 0 ? injectEntitySpans(raw, entities, t) : raw;
  return `<div class="markdown-body">${DOMPurify.sanitize(withEntities, {
    ADD_ATTR: ["target", "data-code", "data-lang", "data-file", "title", "data-entity", "data-entity-type", "class"],
  })}</div>`;
}

// Batch 4a F2: markdown 渲染模块级 LRU 缓存——marked+hljs+DOMPurify 全量计算对同一
// content 只做一次，历史消息重渲 / 会话切换回填 / 组件卸载重挂均命中缓存。
// key 为 content 本身（t 影响实体高亮文案，语言切换时清空）。
const MD_CACHE_LIMIT = 100;
const mdCache = new Map<string, string>();
i18next.on("languageChanged", () => mdCache.clear());

function renderMarkdown(text: string, t: (k: string, opts?: Record<string, unknown>) => string): string {
  const cached = mdCache.get(text);
  if (cached !== undefined) {
    // LRU 触碰：移到最新位置
    mdCache.delete(text);
    mdCache.set(text, cached);
    return cached;
  }
  const html = computeMarkdown(text, t);
  if (mdCache.size >= MD_CACHE_LIMIT) {
    // 逐最旧（Map 保持插入序，首个 key 即最旧）
    mdCache.delete(mdCache.keys().next().value as string);
  }
  mdCache.set(text, html);
  return html;
}

/** Wrap bare <table> elements in a responsive scroll container. */
function wrapTables(html: string): string {
  if (typeof window === "undefined" || !window.DOMParser) return html;
  const parser = new DOMParser();
  const doc = parser.parseFromString(html, "text/html");
  const tables = doc.querySelectorAll("table");
  tables.forEach((table) => {
    const wrapper = doc.createElement("div");
    wrapper.className = "table-wrapper";
    table.parentNode?.insertBefore(wrapper, table);
    wrapper.appendChild(table);
  });
  return doc.body.innerHTML;
}

function tryParseTable(text: string): Record<string, unknown>[] | null {
  try {
    const trimmed = text.trim();
    if (trimmed.startsWith("[")) {
      const arr = JSON.parse(trimmed);
      if (Array.isArray(arr) && arr.length > 0 && typeof arr[0] === "object") return arr;
    }
  } catch {
    /* not JSON */
  }
  return null;
}

function renderChart(meta: ChartMetadata) {
  const { chart_type, data, x_key = "name", y_key = "value", title } = meta;
  const cp = { data, margin: { top: 5, right: 20, left: 0, bottom: 5 } as const };
  const body = (() => {
    switch (chart_type) {
      case "bar":
        return (
          <BarChart {...cp}>
            <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
            <XAxis dataKey={x_key} tick={{ fontSize: 11 }} />
            <YAxis tick={{ fontSize: 11 }} />
            <Tooltip />
            <Bar dataKey={y_key} fill="var(--primary)" radius={[4, 4, 0, 0]} />
          </BarChart>
        );
      case "line":
        return (
          <LineChart {...cp}>
            <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
            <XAxis dataKey={x_key} tick={{ fontSize: 11 }} />
            <YAxis tick={{ fontSize: 11 }} />
            <Tooltip />
            <Line type="monotone" dataKey={y_key} stroke="var(--primary)" strokeWidth={2} dot={{ r: 3 }} />
          </LineChart>
        );
      case "area":
        return (
          <AreaChart {...cp}>
            <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
            <XAxis dataKey={x_key} tick={{ fontSize: 11 }} />
            <YAxis tick={{ fontSize: 11 }} />
            <Tooltip />
            <Area type="monotone" dataKey={y_key} stroke="var(--primary)" fill="var(--primary)" fillOpacity={0.15} />
          </AreaChart>
        );
      case "pie":
        return (
          <PieChart>
            <Pie
              data={data}
              cx="50%"
              cy="50%"
              innerRadius={50}
              outerRadius={100}
              dataKey={y_key}
              nameKey={x_key}
              label={({ name, value }: { name?: string; value?: number }) => `${name ?? ""}: ${value ?? 0}`}
            >
              {data.map((d: Record<string, unknown>, i: number) => (
                <Cell key={`cell-${d?.[x_key] ?? i}-${d?.[y_key] ?? i}`} fill={CHART_COLORS[i % CHART_COLORS.length]} />
              ))}
            </Pie>
            <Tooltip />
            <Legend />
          </PieChart>
        );
      default:
        return <div className="text-xs text-muted-foreground">Unsupported: {chart_type}</div>;
    }
  })();
  return (
    <div className="rounded-xl p-3 bg-surface-3 space-y-2">
      {title && <div className="text-xs font-semibold text-muted-foreground">{title}</div>}
      <ResponsiveContainer width="100%" height={280}>
        {body}
      </ResponsiveContainer>
    </div>
  );
}

/** Task 6.11: 大内容默认折叠，展开后 lazy 渲染 Markdown——对标 Claude Code 默认折叠行为 */
function CollapsibleMarkdown({
  content,
  t,
  lineCount,
}: {
  content: string;
  t: (k: string, opts?: Record<string, unknown>) => string;
  lineCount: number;
}) {
  const [expanded, setExpanded] = useState(false);
  return (
    <details open={expanded} className="rounded-xl bg-surface-3">
      <summary
        onClick={(e) => {
          e.preventDefault();
          setExpanded(!expanded);
        }}
        className="px-3 py-1.5 text-xs font-semibold text-muted-foreground cursor-pointer hover:bg-secondary"
      >
        {t("resultRenderer.largeContent")} ({lineCount} {t("resultRenderer.lines")})
      </summary>
      {expanded && (
        <div className="" dangerouslySetInnerHTML={{ __html: renderMarkdown(content, t) }} />
      )}
    </details>
  );
}

/** Task 6.11: 大代码块虚拟滚动——@tanstack/react-virtual 只渲染可见行，避免 10000 行 DOM 卡死 */
function VirtualizedCodeBlock({ code }: { code: string }) {
  const lines = code.split("\n");
  const parentRef = useRef<HTMLDivElement>(null);
  const virtualizer = useVirtualizer({
    count: lines.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => VIRTUAL_CODE_ROW_HEIGHT,
    overscan: 10,
  });
  return (
    <div
      data-testid="virtual-code-container"
      ref={parentRef}
      className="bg-secondary rounded p-3 overflow-auto text-xs font-mono"
      style={{ maxHeight: VIRTUAL_CODE_MAX_HEIGHT }}
    >
      <div
        style={{
          height: virtualizer.getTotalSize(),
          position: "relative",
          width: "100%",
        }}
      >
        {virtualizer.getVirtualItems().map((vRow) => (
          <div
            key={vRow.index}
            style={{
              position: "absolute",
              top: 0,
              left: 0,
              width: "100%",
              transform: `translateY(${vRow.start}px)`,
              whiteSpace: "pre",
            }}
          >
            {lines[vRow.index]}
          </div>
        ))}
      </div>
    </div>
  );
}

export function ResultRenderer({
  content,
  chunks,
  resultType,
  isStreaming,
  onApplyDiff,
}: ResultRendererProps & { onApplyDiff?: (fileName: string, newContent: string) => void }) {
  const { t } = useTranslation();
  const [copyLabel, setCopyLabel] = useState(t("resultRenderer.copy"));
  const [rejectedDiffs, setRejectedDiffs] = useState<Set<string>>(new Set());
  // Canvas: 代码块内联编辑——点击"编辑"按钮后弹出 Dialog 修改代码
  const [editingCode, setEditingCode] = useState<{ code: string; lang: string } | null>(null);
  const linkOpenBehavior = useAppStore((s) => s.linkOpenBehavior);

  useEffect(() => {
    (window as { __yuncat_link_behavior?: string }).__yuncat_link_behavior = linkOpenBehavior;
  }, [linkOpenBehavior]);

  // Batch 4a F2: displayContent/chartMeta 提升为 useMemo + markdown 结果组件级缓存——
  // 历史消息因父级重渲再次 render 时不重算 marked+hljs+DOMPurify（LRU 兜底跨实例命中）。
  const { displayContent, chartMeta } = useMemo(() => {
    if (chunks && chunks.length > 0) {
      const scan = scanChunks(chunks);
      return { displayContent: scan.textChunks.join(""), chartMeta: scan.chartMeta };
    }
    return { displayContent: content || "", chartMeta: null as ChartMetadata | null };
  }, [content, chunks]);
  // ── 流式渲染策略：节流富文本，而非一律纯文本 ──────────────────────
  //
  // 原实现（E4）在 isStreaming 期间完全跳过 markdown 走纯文本，代价是整个响应
  // 期间用户看到的都是**裸 markdown**（代码围栏、表格管道符、星号全部可见）。
  // 若改成每个 token 全量解析，则是 O(tokens × n) 的 marked + hljs + DOMPurify。
  // 折中：节流到 120ms 一帧——解析次数与**时长**成正比，与 token 数无关；
  // 超长内容仍回退纯文本，防病理性输出拖垮主线程。
  const streamTooLarge = isStreaming && displayContent.length > STREAM_RICH_MAX_CHARS;
  const throttledContent = useThrottledValue(displayContent, STREAM_THROTTLE_MS);
  /** 实际参与渲染的文本：流式期间为节流快照，非流式时为全量 */
  const renderContent = isStreaming ? throttledContent : displayContent;

  const markdownHtml = useMemo(() => {
    // 流式 + 超长 → 下方走纯文本快路径，这里不必解析
    if (streamTooLarge) return "";
    // 与下方 render 分支对齐：code/table 专用分支不走 markdown，避免无谓解析
    if (resultType === "code" || extractCodeBlocks(renderContent).length > 0) return "";
    if (resultType === "table" && tryParseTable(renderContent)) return "";
    return renderMarkdown(renderContent, t);
  }, [renderContent, streamTooLarge, resultType, t]);

  // FE2: 注册到模块级 document click 单一委托（见 delegatedDocClickHandler 注释）——
  // 本实例仅注册"编辑"回调 + 引用计数挂/卸共享监听
  const rrRootId = useId();
  useEffect(() => {
    editHandlers.set(rrRootId, (code, lang) => setEditingCode({ code, lang }));
    if (docClickListenerCount === 0) {
      document.addEventListener("click", delegatedDocClickHandler);
    }
    docClickListenerCount++;
    return () => {
      editHandlers.delete(rrRootId);
      docClickListenerCount--;
      if (docClickListenerCount === 0) {
        document.removeEventListener("click", delegatedDocClickHandler);
      }
    };
  }, [rrRootId]);

  if (!displayContent && !chartMeta) {
    return <span className="text-muted-foreground text-sm italic">(empty)</span>;
  }
  const handleCopy = () => {
    navigator.clipboard.writeText(displayContent || JSON.stringify(chartMeta)).then(() => {
      setCopyLabel(t("resultRenderer.copied"));
      setTimeout(() => setCopyLabel(t("resultRenderer.copy")), 1500);
    });
  };
  // 流式 + 超长内容 → 跳过所有专用渲染器走纯文本 O(1)，避免 O(tokens × n) 的
  // 全量 marked.parse + hljs + DOMPurify；stream_done 后自动切回富文本渲染。
  if (streamTooLarge) {
    return (
      <div>
        <div className="markdown-body whitespace-pre-wrap break-words text-sm leading-relaxed">
          {displayContent}
        </div>
        <button onClick={handleCopy} className="text-xs text-muted-foreground hover:underline mt-2">
          {copyLabel}
        </button>
      </div>
    );
  }
  // chart
  if (chartMeta) {
    return (
      <div className="space-y-3" data-rr-root={rrRootId}>
        {renderChart(chartMeta)}
        {renderContent && <div className="" dangerouslySetInnerHTML={{ __html: renderMarkdown(renderContent, t) }} />}
        <button onClick={handleCopy} className="text-xs text-muted-foreground hover:underline">
          {copyLabel}
        </button>
      </div>
    );
  }
  if (resultType === "chart") {
    return <div className="text-muted-foreground text-sm italic">{t("resultRenderer.chartUnavailable")}</div>;
  }
  // table
  const codeBlocks = extractCodeBlocks(renderContent);
  const table = resultType === "table" ? tryParseTable(renderContent) : null;
  if (table) {
    const cols = Object.keys(table[0]);
    return (
      <div className="space-y-2">
        <div className="overflow-x-auto rounded-xl bg-surface-1">
          <table className="w-full text-xs">
            <thead>
              <tr className="bg-secondary">
                {cols.map((c) => (
                  <th key={c} className="px-2 py-1.5 text-left font-semibold">
                    {c}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {table.map((row) => {
                const rowKey = cols.map((c) => String(row[c] ?? "")).join("|");
                return (
                  <tr key={`table-row-${rowKey}`} className="border-t">
                    {cols.map((c) => (
                      <td key={c} className="px-2 py-1.5">
                        {String(row[c] ?? "")}
                      </td>
                    ))}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        <button onClick={handleCopy} className="text-xs text-muted-foreground hover:underline">
          {copyLabel}
        </button>
      </div>
    );
  }
  // code — Task 6.11: 大代码块用 VirtualizedCodeBlock 虚拟滚动
  if (resultType === "code" || codeBlocks.length > 0) {
    return (
      <div className="space-y-2">
        {codeBlocks.length > 0 ? (
          codeBlocks.map((b) => {
            const isMermaid = b.language === "mermaid";
            const lineCount = b.code.split("\n").length;
            const isLargeCode = lineCount > LARGE_CODE_LINES;
            return (
              <div key={b.language + ":" + b.code.slice(0, 40)}>
                {b.language && (
                  <div className="text-xs text-muted-foreground mb-1">
                    {b.language}
                    {isMermaid && (
                      <a
                        href={`https://mermaid.live/view#${encodeURIComponent(JSON.stringify({ code: b.code, mermaid: { theme: "default" } }))}`}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="ml-2 text-primary hover:underline"
                      >
                        {t("resultRenderer.openInMermaid")}
                      </a>
                    )}
                  </div>
                )}
                {isLargeCode ? (
                  <VirtualizedCodeBlock code={b.code} />
                ) : (
                  <pre className="bg-secondary rounded p-3 overflow-x-auto text-xs font-mono">
                    <code>{b.code}</code>
                  </pre>
                )}
              </div>
            );
          })
        ) : (
          <pre className="bg-secondary rounded p-3 overflow-x-auto text-xs font-mono whitespace-pre-wrap">
            {renderContent}
          </pre>
        )}
        <button onClick={handleCopy} className="text-xs text-muted-foreground hover:underline">
          {copyLabel}
        </button>
      </div>
    );
  }
  // default markdown — renderMarkdown 内部已含 DOMPurify sanitize，不重复净化
  // Task 6.11: 大内容（>= LARGE_CONTENT_CHARS）默认折叠，展开后 lazy 渲染
  const diffs = detectInlineDiffs(renderContent);
  const visibleDiffs = diffs.filter((d) => !rejectedDiffs.has(d.title));
  // 流式期间**不折叠**：CollapsibleMarkdown 是为已定稿内容设计的（默认收起 + 展开后
  // lazy 渲染），流式期间内容还在增长，收起会让用户盯着一个折叠框看不到任何输出。
  const isLargeContent = !isStreaming && renderContent.length >= LARGE_CONTENT_CHARS;
  return (
    <div data-rr-root={rrRootId}>
      {isLargeContent ? (
        <CollapsibleMarkdown
          content={renderContent}
          t={t}
          lineCount={renderContent.split("\n").length}
        />
      ) : (
        <div className="" dangerouslySetInnerHTML={{ __html: markdownHtml }} />
      )}
      {/* v66: 内联 Diff — Accept 写入工作区 */}
      {visibleDiffs.length > 0 && (
        <details className="mt-2 rounded-xl bg-surface-3" open>
          <summary className="px-3 py-1.5 text-xs font-semibold text-primary cursor-pointer hover:bg-secondary">
            {t("resultRenderer.viewFileChanges", { n: visibleDiffs.length })}
          </summary>
          <div className="p-2 space-y-3">
            {visibleDiffs.map((d) => (
              <Suspense key={d.title} fallback={<div className="text-xs text-muted-foreground p-2">{t("resultRenderer.loadingDiff")}</div>}>
                <InlineDiff
                  oldContent={d.oldContent}
                  newContent={d.newContent}
                  title={d.title}
                  onAccept={() => onApplyDiff?.(d.title, d.newContent)}
                  onReject={() => setRejectedDiffs((prev) => new Set(prev).add(d.title))}
                />
              </Suspense>
            ))}
          </div>
        </details>
      )}
      <button onClick={handleCopy} className="text-xs text-muted-foreground hover:underline mt-1">
        {copyLabel}
      </button>
      {/* Canvas: 代码块内联编辑 Dialog——点击代码块"编辑"按钮后弹出 */}
      <Dialog open={!!editingCode} onOpenChange={(open) => { if (!open) setEditingCode(null); }}>
        <DialogContent className="max-w-3xl max-h-[80vh]">
          <DialogHeader>
            <DialogTitle>{t("resultRenderer.editCode")} — {editingCode?.lang}</DialogTitle>
          </DialogHeader>
          <Textarea
            className="font-mono text-sm min-h-[400px] resize-none"
            value={editingCode?.code || ""}
            onChange={(e) => setEditingCode((prev) => prev ? { ...prev, code: e.target.value } : null)}
          />
          <DialogFooter>
            <Button variant="outline" onClick={() => setEditingCode(null)}>
              {t("common.cancel")}
            </Button>
            <Button variant="outline" onClick={() => {
              if (editingCode) {
                navigator.clipboard.writeText(editingCode.code).catch(() => {});
                setEditingCode(null);
              }
            }}>
              {t("resultRenderer.copy")}
            </Button>
            <Button onClick={() => {
              if (editingCode) {
                // 发送到输入框——通过自定义事件通知 ChatView
                window.dispatchEvent(new CustomEvent("yuncat-insert-input", { detail: editingCode.code }));
                setEditingCode(null);
              }
            }}>
              {t("resultRenderer.sendToInput")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

// ── FE2: document click 单一委托 ──
// 原实现：每个 ResultRenderer 实例 useEffect 挂一个 document click 监听——
// N 条消息 N 个监听，点一次 copy 按钮所有实例 handler 全部命中（clipboard 写 N 次）。
// 现改为模块级单例监听（首个实例挂载时注册、最后一个卸载时移除）：
// 无状态行为（copy / 文件链接 / 实体）直接分发；依赖实例 state 的"编辑"按钮通过
// data-rr-root 标记 + 模块级回调注册表路由到按钮所属的目标实例。
type EditHandler = (code: string, lang: string) => void;
const editHandlers = new Map<string, EditHandler>();
let docClickListenerCount = 0;

function delegatedDocClickHandler(e: MouseEvent) {
  const target = e.target as HTMLElement;
  // Canvas: 代码块"编辑"按钮——路由到按钮所属实例（打开内联编辑 Dialog）
  const editBtn = target.closest(".edit-btn") as HTMLElement | null;
  if (editBtn) {
    const root = editBtn.closest("[data-rr-root]") as HTMLElement | null;
    const cb = root ? editHandlers.get(root.dataset.rrRoot || "") : undefined;
    if (cb) {
      cb(decodeURIComponent(editBtn.dataset.code || ""), editBtn.dataset.lang || "text");
      return;
    }
  }
  const btn = target.closest(".copy-btn") as HTMLElement | null;
  if (btn) {
    const code = decodeURIComponent(btn.dataset.code || "");
    navigator.clipboard.writeText(code).then(() => {
      btn.textContent = i18next.t("resultRenderer.copied");
      setTimeout(() => {
        btn.textContent = i18next.t("resultRenderer.copy");
      }, 1500);
    });
    return;
  }
  // v66: 文件路径点击 → 跳转到工作区打开
  const fileLink = target.closest("[data-file]") as HTMLElement | null;
  if (fileLink) {
    const fname = fileLink.dataset.file;
    if (fname) useAppStore.getState().setPendingWorkspaceFile(fname);
    return;
  }
  // 实体点击 → 跳转/复制
  const entityEl = target.closest("[data-entity]") as HTMLElement | null;
  if (entityEl) {
    const val = entityEl.dataset.entity || "";
    const etype = entityEl.dataset.entityType || "";
    if (etype === "url") {
      const behavior = (window as { __yuncat_link_behavior?: string }).__yuncat_link_behavior || "copy";
      if (behavior === "browser") {
        window.open(val, "_blank", "noopener,noreferrer");
      } else {
        navigator.clipboard.writeText(val).catch(() => {});
        const orig = entityEl.textContent || val;
        entityEl.textContent = i18next.t("resultRenderer.copied");
        entityEl.style.opacity = "0.7";
        setTimeout(() => {
          entityEl.textContent = orig;
          entityEl.style.opacity = "1";
        }, 1500);
      }
    } else if (etype === "file") {
      useAppStore.getState().setPendingWorkspaceFile(val);
    }
  }
}

/** Phase 11: 从文本中检测文件写入并提供 Diff 预览 */
function detectInlineDiffs(text: string): Array<{ oldContent: string; newContent: string; title: string }> {
  const results: Array<{ oldContent: string; newContent: string; title: string }> = [];
  const seen = new Set<string>();

  // 检测 "文件路径" 附近的代码块
  const filePattern = /`([^`]+\.(?:py|ts|js|tsx|jsx|md|json|yaml|yml|html|css|rs|go|java|sql|txt))`/g;
  let match: RegExpExecArray | null;
  filePattern.lastIndex = 0;

  while ((match = filePattern.exec(text)) !== null) {
    const fname = match[1].replace(/^.*[/\\]/, "");
    if (seen.has(fname)) continue;
    seen.add(fname);

    const idx = match.index;
    const before = text.slice(Math.max(0, idx - 800), idx);
    const after = text.slice(idx, Math.min(text.length, idx + 2000));

    const oldCode = extractCodeBlock(before);
    const newCode = extractCodeBlock(after);

    if (newCode) {
      results.push({
        title: fname,
        oldContent: oldCode || i18next.t("resultRenderer.noOriginalContent"),
        newContent: newCode,
      });
    }
  }
  return results.slice(0, 5); // 最多 5 个
}

function extractCodeBlock(text: string): string {
  const match = text.match(/```[\w]*\n?([\s\S]*?)```/);
  return match ? match[1].trim() : "";
}
