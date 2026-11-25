/**
 * lib/documentPreview.ts —— 文档预览的解析层（不含 React）
 *
 * 三种格式各用一个成熟库，全部走动态 import：不看这类文件的人不该为它们付启动成本。
 *   docx → mammoth（BSD-2）转成语义化 HTML，再过 DOMPurify——文档里的 HTML 同样不可信
 *   xlsx → exceljs（MIT）读出工作表与单元格文本
 *   pptx → pptx-preview（ISC）按原始比例渲染到容器
 *
 * 失败一律抛错，由组件翻译成「打不开，可以用系统默认应用打开」：
 * 不返回半成品，也不静默吞掉。
 */
import DOMPurify from "dompurify";
import { detectDelimiter, parseDelimited } from "./csv";

/** 表格预览上限（人为上限，可调）：整张几十万行的表塞进 DOM 会直接把页面拖死 */
export const TABLE_MAX_ROWS = 500;
export const TABLE_MAX_COLS = 60;
export const TABLE_MAX_CELL_CHARS = 200;

export interface SheetView {
  name: string;
  rows: string[][];
  rowCount: number;
  columnCount: number;
  truncated: boolean;
}

/** 单元格取文本：数字/日期/公式/富文本都折成可读字符串，避免出现 [object Object] */
export function cellText(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  if (value instanceof Date) return value.toISOString().slice(0, 10);
  if (typeof value === "object") {
    const record = value as {
      text?: unknown;
      result?: unknown;
      formula?: unknown;
      hyperlink?: unknown;
      richText?: Array<{ text?: unknown }>;
    };
    if (Array.isArray(record.richText)) {
      return record.richText.map((part) => (typeof part?.text === "string" ? part.text : "")).join("");
    }
    if (record.hyperlink !== undefined && typeof record.hyperlink === "string") return record.hyperlink;
    if (record.text !== undefined && typeof record.text !== "object") return String(record.text);
    if (record.result !== undefined && typeof record.result !== "object") return String(record.result);
    if (record.formula !== undefined) return "";
  }
  return String(value);
}

/** 裁剪到展示上限，并如实标记「被截断了」——静默丢行会让人以为文件本身没数据 */
export function trimTable(rows: string[][]): { rows: string[][]; truncated: boolean } {
  const truncated = rows.length > TABLE_MAX_ROWS || rows.some((row) => row.length > TABLE_MAX_COLS);
  const capped = rows.slice(0, TABLE_MAX_ROWS).map((row) =>
    row.slice(0, TABLE_MAX_COLS).map((cell) => {
      const text = cellText(cell);
      return text.length > TABLE_MAX_CELL_CHARS ? `${text.slice(0, TABLE_MAX_CELL_CHARS)}…` : text;
    }),
  );
  return { rows: capped, truncated };
}

/** CSV / TSV 文本 → 单张工作表 */
export function sheetsFromDelimited(text: string, name: string): SheetView[] {
  const rows = parseDelimited(text, detectDelimiter(name, text));
  const trimmed = trimTable(rows);
  return [{
    name: name.split(/[\\/]/).pop() || name,
    rows: trimmed.rows,
    rowCount: rows.length,
    columnCount: rows.reduce((max, row) => Math.max(max, row.length), 0),
    truncated: trimmed.truncated,
  }];
}

type MammothModule = {
  convertToHtml?: (input: { arrayBuffer: ArrayBuffer }, options?: unknown) => Promise<{ value?: string }>;
  default?: MammothModule;
};

/**
 * docx → 已净化 HTML。
 *
 * 显式引浏览器构建：包默认入口是 Node 版（用 fs 打开 zip），喂 `{ arrayBuffer }` 会报
 * "Could not find file in options"。靠打包器的 browser 字段映射不稳（实测在测试环境里
 * 就解析到了 Node 版），所以这里把依赖钉死。
 */
export async function loadDocxHtml(blob: Blob): Promise<string> {
  const mod = (await import("mammoth/mammoth.browser")) as unknown as MammothModule;
  const convert = mod.convertToHtml ?? mod.default?.convertToHtml;
  if (typeof convert !== "function") throw new Error("docx 解析器不可用");
  const result = await convert({ arrayBuffer: await blob.arrayBuffer() });
  return DOMPurify.sanitize(result?.value ?? "", { ADD_ATTR: ["target"] });
}

interface ExcelCell { text?: string; value?: unknown }
interface ExcelRow { getCell(index: number): ExcelCell }
interface ExcelSheet {
  name: string;
  rowCount: number;
  columnCount: number;
  getRow(index: number): ExcelRow;
}
interface ExcelWorkbook {
  worksheets: ExcelSheet[];
  xlsx: { load(data: ArrayBuffer): Promise<unknown> };
}
interface ExcelModule { Workbook: new () => ExcelWorkbook }

/** xlsx / xlsm → 工作表列表 */
export async function loadWorkbookSheets(blob: Blob): Promise<SheetView[]> {
  const mod = (await import("exceljs")) as unknown as ExcelModule & { default?: ExcelModule };
  const factory: ExcelModule | undefined = typeof mod.Workbook === "function" ? mod : mod.default;
  if (!factory) throw new Error("xlsx 解析器不可用");
  const workbook = new factory.Workbook();
  await workbook.xlsx.load(await blob.arrayBuffer());
  return (workbook.worksheets || []).map((sheet) => {
    const rowCount = sheet.rowCount || 0;
    const columnCount = sheet.columnCount || 0;
    const rows: string[][] = [];
    for (let r = 1; r <= Math.min(rowCount, TABLE_MAX_ROWS); r += 1) {
      const row = sheet.getRow(r);
      const cells: string[] = [];
      for (let c = 1; c <= Math.min(columnCount, TABLE_MAX_COLS); c += 1) {
        const cell = row.getCell(c);
        cells.push(cell.text !== undefined && cell.text !== null ? cell.text : cellText(cell.value));
      }
      rows.push(cells);
    }
    return {
      name: sheet.name,
      rows,
      rowCount,
      columnCount,
      truncated: rowCount > TABLE_MAX_ROWS || columnCount > TABLE_MAX_COLS,
    };
  });
}

export interface PptxPreviewer { preview(data: ArrayBuffer): Promise<unknown> | unknown }

type PptxModule = {
  init?: (element: HTMLElement, options: { width: number; height: number }) => PptxPreviewer;
  default?: PptxModule;
};

/** pptx → 挂到容器上的预览器；调用方负责给容器一块可滚动的区域 */
export async function createPptxPreviewer(
  host: HTMLElement,
  width: number,
  height: number,
): Promise<PptxPreviewer> {
  const mod = (await import("pptx-preview")) as unknown as PptxModule;
  const init = mod.init ?? mod.default?.init;
  if (typeof init !== "function") throw new Error("pptx 预览器不可用");
  return init(host, { width, height });
}
