import { describe, expect, it, vi } from "vitest";

import {
  TABLE_MAX_COLS,
  TABLE_MAX_ROWS,
  cellText,
  loadDocxHtml,
  loadWorkbookSheets,
  sheetsFromDelimited,
  trimTable,
} from "../documentPreview";

// 生产代码显式引浏览器构建（Node 版入口用 fs 打开 zip，喂 arrayBuffer 会报错），
// mock 必须跟着走同一个模块 id，否则会去跑真的 JSZip。
vi.mock("mammoth/mammoth.browser", () => ({
  convertToHtml: vi.fn(async () => ({ value: '<p>正文</p><script>alert(1)</script>' })),
}));

vi.mock("exceljs", () => {
  class FakeWorkbook {
    worksheets = [
      {
        name: "Sheet1",
        rowCount: 2,
        columnCount: 2,
        getRow: (r: number) => ({
          getCell: (c: number) => ({
            text: r === 1 ? ["名称", "数量"][c - 1] : ["苹果", "3"][c - 1],
            value: undefined,
          }),
        }),
      },
    ];
    xlsx = { load: vi.fn(async () => undefined) };
  }
  return { Workbook: FakeWorkbook };
});

describe("cellText", () => {
  it("数字/布尔/日期折成可读文本", () => {
    expect(cellText(3.5)).toBe("3.5");
    expect(cellText(true)).toBe("true");
    expect(cellText(new Date(Date.UTC(2026, 8, 16)))).toBe("2026-09-16");
    expect(cellText(null)).toBe("");
  });

  it("富文本取纯文本，公式取缓存结果（不能出现 [object Object]）", () => {
    expect(cellText({ richText: [{ text: "你好" }, { text: "世界" }] })).toBe("你好世界");
    expect(cellText({ formula: "A1+B1", result: 42 })).toBe("42");
    expect(cellText({ formula: "A1+B1" })).toBe("");
    expect(cellText({ hyperlink: "https://example.com" })).toBe("https://example.com");
  });
});

describe("trimTable", () => {
  it("超过行/列上限时裁剪并明确标记被截断", () => {
    const rows = Array.from({ length: TABLE_MAX_ROWS + 5 }, () =>
      Array.from({ length: TABLE_MAX_COLS + 3 }, () => "x"),
    );
    const trimmed = trimTable(rows);
    expect(trimmed.truncated).toBe(true);
    expect(trimmed.rows.length).toBe(TABLE_MAX_ROWS);
    expect(trimmed.rows[0].length).toBe(TABLE_MAX_COLS);
  });

  it("没超限时不标记截断", () => {
    const trimmed = trimTable([["a", "b"]]);
    expect(trimmed.truncated).toBe(false);
    expect(trimmed.rows).toEqual([["a", "b"]]);
  });
});

describe("sheetsFromDelimited", () => {
  it("用文件名当工作表名，并统计行列数", () => {
    const sheets = sheetsFromDelimited("a,b\n1,2\n3,4", "reports/sales.csv");
    expect(sheets).toHaveLength(1);
    expect(sheets[0].name).toBe("sales.csv");
    expect(sheets[0].rowCount).toBe(3);
    expect(sheets[0].columnCount).toBe(2);
    expect(sheets[0].rows[1]).toEqual(["1", "2"]);
  });
});

describe("loadDocxHtml", () => {
  it("转换结果必须过 DOMPurify（文档里的 script 不能带进来）", async () => {
    const html = await loadDocxHtml({ arrayBuffer: async () => new ArrayBuffer(4) } as unknown as Blob);
    expect(html).toContain("正文");
    expect(html).not.toContain("<script");
  });
});

describe("loadWorkbookSheets", () => {
  it("读出工作表名与单元格文本", async () => {
    const sheets = await loadWorkbookSheets({ arrayBuffer: async () => new ArrayBuffer(4) } as unknown as Blob);
    expect(sheets).toHaveLength(1);
    expect(sheets[0].name).toBe("Sheet1");
    expect(sheets[0].rows).toEqual([
      ["名称", "数量"],
      ["苹果", "3"],
    ]);
  });
});
