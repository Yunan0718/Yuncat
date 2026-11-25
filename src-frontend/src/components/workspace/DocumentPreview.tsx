import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { FileWarning } from "lucide-react";
import {
  createPptxPreviewer,
  loadDocxHtml,
  loadWorkbookSheets,
  sheetsFromDelimited,
  type SheetView,
} from "../../lib/documentPreview";
import type { DocumentPreviewKind } from "../../lib/workspaceTabs";

/**
 * 内置文档预览（docx / xlsx / pptx / CSV 表格视图）——2026-09-16 新增。
 *
 * 与 MediaPreview 的分工：那个负责浏览器原生就能渲染的媒体（图片/PDF/音频/视频/HTML），
 * 这个负责需要解析的文档。解析库全部动态 import，所以不打这些文件的人不付启动成本。
 *
 * 两条硬规则：
 *   - 解析出的 HTML 必须先过 DOMPurify（文档里的 HTML 和网页上的 HTML 一样不可信）；
 *   - 解析失败要说清楚「打不开，可以用系统默认应用打开」，不留白屏。
 */

type Status =
  | { kind: "loading" }
  | { kind: "error"; message: string }
  | { kind: "html"; html: string }
  | { kind: "sheets"; sheets: SheetView[] }
  | { kind: "slides" };

/** 幻灯片按 16:9 渲染；容器可滚动，用户能看完整页 */
const SLIDE_WIDTH = 960;
const SLIDE_HEIGHT = 540;

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function SheetTable({ sheet, truncatedLabel }: { sheet: SheetView; truncatedLabel: string }) {
  return (
    <div className="h-full overflow-auto">
      <table className="border-collapse text-xs w-max min-w-full">
        <tbody>
          {sheet.rows.map((row, rowIndex) => (
            // eslint-disable-next-line react/no-array-index-key -- 表格行列没有业务主键，索引就是身份；只读渲染不会重排
            <tr key={rowIndex} className={rowIndex === 0 ? "bg-secondary/60 font-semibold" : ""}>
              <th className="sticky left-0 z-10 bg-background border border-border px-2 py-1 text-[10px] text-muted-foreground text-right select-none">
                {rowIndex + 1}
              </th>
              {row.map((cell, cellIndex) => (
                <td
                  /* eslint-disable-next-line react/no-array-index-key -- 同上，单元格位置即身份 */
                  key={cellIndex}
                  className="border border-border px-2 py-1 align-top whitespace-pre-wrap max-w-[280px]"
                >
                  {cell}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {sheet.truncated && <div className="p-2 text-[11px] text-muted-foreground">{truncatedLabel}</div>}
    </div>
  );
}

export function DocumentPreview({
  apiBlob,
  path,
  name,
  kind,
  text,
}: {
  apiBlob?: (path: string, options?: RequestInit) => Promise<Blob>;
  path: string;
  name: string;
  kind: DocumentPreviewKind;
  /** CSV / TSV 走文本通道：内容已经是编辑器里读到的文本，不必再取二进制 */
  text?: string;
}) {
  const { t } = useTranslation();
  const [status, setStatus] = useState<Status>({ kind: "loading" });
  const [activeSheet, setActiveSheet] = useState(0);
  const slideHostRef = useRef<HTMLDivElement | null>(null);
  const slideBufferRef = useRef<ArrayBuffer | null>(null);

  const rawUrl = `/api/v1/workspace/files/raw?path=${encodeURIComponent(path)}`;

  useEffect(() => {
    let cancelled = false;
    setStatus({ kind: "loading" });
    setActiveSheet(0);
    const run = async () => {
      try {
        if (kind === "spreadsheet" && text !== undefined) {
          const sheets = sheetsFromDelimited(text, name);
          if (!cancelled) setStatus({ kind: "sheets", sheets });
          return;
        }
        if (!apiBlob) throw new Error("缺少文件读取接口");
        if (kind === "slides") {
          slideBufferRef.current = await (await apiBlob(rawUrl)).arrayBuffer();
          if (!cancelled) setStatus({ kind: "slides" });
          return;
        }
        const blob = await apiBlob(rawUrl);
        if (kind === "document") {
          const html = await loadDocxHtml(blob);
          if (!cancelled) setStatus({ kind: "html", html });
          return;
        }
        const sheets = await loadWorkbookSheets(blob);
        if (!cancelled) setStatus({ kind: "sheets", sheets });
      } catch (error) {
        if (!cancelled) setStatus({ kind: "error", message: errorText(error) });
      }
    };
    void run();
    return () => {
      cancelled = true;
    };
  }, [apiBlob, kind, name, rawUrl, text]);

  useEffect(() => {
    if (status.kind !== "slides") return;
    const host = slideHostRef.current;
    const buffer = slideBufferRef.current;
    if (!host || !buffer) return;
    let disposed = false;
    host.replaceChildren();
    createPptxPreviewer(host, SLIDE_WIDTH, SLIDE_HEIGHT)
      .then((previewer) => (disposed ? undefined : previewer.preview(buffer)))
      .catch((error) => {
        if (!disposed) setStatus({ kind: "error", message: errorText(error) });
      });
    return () => {
      disposed = true;
      host.replaceChildren();
    };
  }, [status.kind]);

  if (status.kind === "loading") {
    return (
      <div className="flex items-center justify-center h-full text-sm text-muted-foreground">
        {t("documentPreview.loading")}
      </div>
    );
  }

  if (status.kind === "error") {
    return (
      <div className="flex flex-col items-center justify-center h-full gap-1 text-sm text-muted-foreground px-6 text-center">
        <FileWarning size={28} strokeWidth={1.3} className="text-warning" />
        <span>{t("documentPreview.loadFailed", { name })}</span>
        <span className="text-[11px] text-muted-foreground/80">{status.message}</span>
        <span className="text-[11px] text-muted-foreground/80">{t("documentPreview.unsupportedHint")}</span>
      </div>
    );
  }

  if (status.kind === "html") {
    return (
      <div
        className="docx-body h-full overflow-auto px-6 py-4 text-sm leading-relaxed"
        dangerouslySetInnerHTML={{ __html: status.html }}
      />
    );
  }

  if (status.kind === "slides") {
    return (
      <div className="h-full overflow-auto bg-secondary/30 p-4">
        <p className="mb-3 text-[11px] text-muted-foreground">{t("documentPreview.slidesHint")}</p>
        <div ref={slideHostRef} data-testid="pptx-host" />
      </div>
    );
  }

  const sheets = status.sheets;
  if (sheets.length === 0) {
    return (
      <div className="flex items-center justify-center h-full text-sm text-muted-foreground">
        {t("documentPreview.empty")}
      </div>
    );
  }
  const current = sheets[Math.min(activeSheet, sheets.length - 1)];
  return (
    <div className="flex flex-col h-full">
      {sheets.length > 1 && (
        <div className="flex items-center gap-1 px-2 py-1 border-b border-border overflow-x-auto">
          {sheets.map((sheet, index) => (
            <button
              key={sheet.name}
              type="button"
              onClick={() => setActiveSheet(index)}
              className={`px-2 py-0.5 text-[11px] rounded ${
                index === activeSheet ? "bg-secondary font-semibold" : "hover:bg-secondary/60"
              }`}
            >
              {sheet.name}
            </button>
          ))}
        </div>
      )}
      <div className="flex-1 min-h-0">
        <SheetTable
          sheet={current}
          truncatedLabel={t("documentPreview.truncated", {
            rows: current.rows.length,
            total: current.rowCount,
          })}
        />
      </div>
    </div>
  );
}
